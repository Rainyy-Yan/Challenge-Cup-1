"""Dependency-injected qykw command runner.

The runner deliberately contains orchestration only: gateways, state, review
and publishing are supplied by the trusted controller.  It never derives a
pull head from an Actions checkout or from untrusted comment content.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Callable, Protocol

from tools.qykw.advisory import AdvisoryService
from tools.qykw.commands import CommandRouter
from tools.qykw.config import QykwConfig
from tools.qykw.context import build_context_plan
from tools.qykw.domain import (
    Actor, CommandName, EventContext, PullSnapshot, RunContext, RunOutcome,
    RunRecord, RunStage, RunStatus,
)
from tools.qykw.github import GitHubGateway
from tools.qykw.policy import authorize_command
from tools.qykw.publish import ReviewPublisher, sanitize_public_text
from tools.qykw.review import ReviewEngine
from tools.qykw.state import RunStateStore
from tools.qykw.triggers import build_run_context, decide_trigger


_PROMPT_VERSION = "qykw-v1"
_TERMINAL = frozenset({RunStatus.COMPLETED, RunStatus.PARTIAL, RunStatus.FAILED, RunStatus.CANCELED, RunStatus.STALE})
_ContextBuilder = Callable[[PullSnapshot, RunContext], object]


class _Advisory(Protocol):
    def handle(self, run: RunContext, plan: object | None, record: RunRecord | None = None) -> object: ...


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class QykwRunner:
    """Run one already-normalized event through the fixed command state machine."""

    def __init__(
        self,
        config: QykwConfig,
        gateway: GitHubGateway,
        state: RunStateStore,
        advisory: _Advisory | None,
        engine: ReviewEngine | object,
        publisher: ReviewPublisher | object,
        *,
        context_builder: _ContextBuilder | None = None,
        now: Callable[[], str] = _utc_now,
        state_path: Path | None = None,
    ) -> None:
        self.config = config
        self.gateway = gateway
        self.state = state
        self.advisory = advisory
        self.engine = engine
        self.publisher = publisher
        self.context_builder = context_builder or self._build_context
        self.now = now
        configured_path = os.environ.get("QYKW_STATE_PATH")
        self._state_path = state_path if state_path is not None else (Path(configured_path) if configured_path else None)

    def handle(self, event: EventContext) -> RunOutcome:
        """Handle a normalized event without elevating untrusted input.

        A rejected event is intentionally a silent, side-effect-free outcome.
        """

        if not self._valid_event(event):
            return self._outcome("", RunStatus.FAILED, RunStage.ACCEPTED, "invalid_event")
        if event.command.name is CommandName.STOP:
            return self._handle_stop(event)

        actor = self._actor(event)
        if actor is None:
            return self._outcome("", RunStatus.FAILED, RunStage.ACCEPTED, "authorization_unavailable")
        authorization = authorize_command(event.command, actor, self.config)
        if not authorization.allowed:
            status = RunStatus.FAILED
            return self._outcome("", status, RunStage.ACCEPTED, authorization.reason)

        existing = self.state.find_by_idempotency_key(event.pr_number, event.idempotency_key)
        trigger = decide_trigger(event, existing_run=existing,
                                 initial_review_completed=self.state.has_successful_initial_review(event.pr_number),
                                 config=self.config)
        if not trigger.run:
            if existing is not None:
                return self._record_outcome(existing)
            return self._outcome("", RunStatus.FAILED, RunStage.ACCEPTED, trigger.reason)

        try:
            run = build_run_context(event, self.gateway.get_pull_ref(event.pr_number))
        except Exception:
            run = None
        if run is None:
            return self._outcome("", RunStatus.STALE, RunStage.ACCEPTED, "pull_ref_unavailable")

        # Identity and acknowledgement only happen after every non-writing gate.
        warnings: tuple[str, ...] = ()
        try:
            self.gateway.assert_bot_identity("qykw")
            if event.trigger_comment_id is not None and event.trigger_comment_kind is not None:
                try:
                    self.gateway.try_add_reaction(
                        self._trigger_ref(event), "laugh"
                    )
                except Exception:
                    warnings = ("reaction_failed",)
        except Exception:
            return self._outcome(run.run_id, RunStatus.FAILED, RunStage.ACCEPTED, "bot_identity_unavailable")

        record = self._record(run, RunStage.ACCEPTED, RunStatus.ACTIVE, warnings=warnings)
        if not self.state.create(record):
            duplicate = self.state.find_by_idempotency_key(event.pr_number, event.idempotency_key)
            return self._record_outcome(duplicate) if duplicate is not None else self._outcome(run.run_id, RunStatus.FAILED, RunStage.ACCEPTED, "state_claim_failed")
        record = self.state.get(run.pr_number, run.run_id) or record
        self._write_local_state(record)

        if event.command.name in {CommandName.HELP, CommandName.STATUS, CommandName.SUMMARY}:
            return self._run_deterministic(record)
        if event.command.name in {CommandName.ANALYZE, CommandName.PLAN}:
            return self._run_advisory(record)
        return self._run_review(record)

    def _handle_stop(self, event: EventContext) -> RunOutcome:
        active = self.state.find_latest_active(event.pr_number)
        if active is None:
            return self._outcome("", RunStatus.FAILED, RunStage.ACCEPTED, "no_active_run")
        actor = self._actor(event)
        if actor is None:
            return self._outcome(active.context.run_id, RunStatus.FAILED, active.stage, "authorization_unavailable")
        decision = authorize_command(event.command, actor, self.config,
                                     run_trigger_actor=active.context.trigger_actor)
        if not decision.allowed:
            return self._outcome(active.context.run_id, RunStatus.FAILED, active.stage, decision.reason)
        if event.trigger_comment_id is None:
            return self._outcome(active.context.run_id, RunStatus.FAILED, active.stage, "missing_stop_comment")
        try:
            self.gateway.assert_bot_identity("qykw")
            if event.trigger_comment_kind is not None:
                try:
                    self.gateway.try_add_reaction(self._trigger_ref(event), "laugh")
                except Exception:
                    pass
            self.state.request_cancel(event.pr_number, active.context.run_id,
                                      stop_comment_id=event.trigger_comment_id,
                                      actor_login=actor.login)
        except Exception:
            return self._outcome(active.context.run_id, RunStatus.FAILED, active.stage, "cancel_record_failed")
        return self._outcome(active.context.run_id, RunStatus.ACTIVE, active.stage, None)

    def _run_deterministic(self, record: RunRecord) -> RunOutcome:
        if self._cancelled(record):
            return self._cancel(record)
        current = record
        if self.advisory is not None:
            try:
                result = self.advisory.handle(current.context, None, self.state.find_latest(current.context.pr_number))
                if self._cancelled(current):
                    return self._cancel(current)
                publication_problem = self._publication_problem(current)
                if publication_problem is not None:
                    return self._publication_failure(current, publication_problem)
                if self._cancelled(current):
                    return self._cancel(current)
                self.gateway.create_issue_comment(current.context.pr_number, self._safe_advisory_body(result))
            except Exception:
                return self._finish(current, RunStatus.FAILED, "deterministic_response_failed")
        return self._finish(current, RunStatus.COMPLETED, None)

    def _run_advisory(self, record: RunRecord) -> RunOutcome:
        if self.advisory is None:
            return self._finish(record, RunStatus.FAILED, "advisory_unavailable")
        collected = self._collect(record)
        if isinstance(collected, RunOutcome):
            return collected
        snapshot, record = collected
        record = self._transition(record, RunStage.ANALYZING)
        if self._cancelled(record):
            return self._cancel(record)
        try:
            plan = self.context_builder(snapshot, record.context)
            result = self.advisory.handle(record.context, plan, record)
        except Exception:
            return self._finish(record, RunStatus.FAILED, "advisory_failed")
        if self._cancelled(record):
            return self._cancel(record)
        publication_problem = self._publication_problem(record)
        if publication_problem is not None:
            return self._publication_failure(record, publication_problem)
        if self._cancelled(record):
            return self._cancel(record)
        try:
            self.gateway.create_issue_comment(record.context.pr_number, self._safe_advisory_body(result))
        except Exception:
            return self._finish(record, RunStatus.FAILED, "advisory_publish_failed")
        return self._finish(record, RunStatus.COMPLETED, None)

    def _run_review(self, record: RunRecord) -> RunOutcome:
        if self._cancelled(record):
            return self._cancel(record)
        record = self._transition(record, RunStage.ACKNOWLEDGED)
        if self._cancelled(record):
            return self._cancel(record)
        collected = self._collect(record)
        if isinstance(collected, RunOutcome):
            return collected
        snapshot, record = collected
        record = self._transition(record, RunStage.ANALYZING)
        if self._cancelled(record):
            return self._cancel(record)
        try:
            plan = self.context_builder(snapshot, record.context)
            result = self.engine.review(record.context, snapshot, plan)  # type: ignore[union-attr]
        except Exception:
            return self._finish(record, RunStatus.FAILED, "review_failed")
        if self._cancelled(record):
            return self._cancel(record)
        record = self._transition(record, RunStage.VALIDATING)
        if self._cancelled(record):
            return self._cancel(record)
        record = self._transition(record, RunStage.PUBLISHING)
        if self._cancelled(record):
            return self._cancel(record)
        publication_problem = self._publication_problem(record)
        if publication_problem is not None:
            return self._publication_failure(record, publication_problem)
        if self._cancelled(record):
            return self._cancel(record)
        try:
            if isinstance(self.publisher, ReviewPublisher):
                published = self.publisher.publish_review(
                    record.context, result, write_guard=lambda: self._publish_permitted(record)
                )
            else:
                published = self.publisher.publish_review(record.context, result)  # type: ignore[union-attr]
        except Exception:
            return self._finish(record, RunStatus.FAILED, "publish_failed")
        if self._cancelled(record):
            return self._cancel(record)
        return self._finish(record, published.status, None,
                            warnings=tuple(getattr(published, "warning_codes", ())))

    def _collect(self, record: RunRecord) -> tuple[PullSnapshot, RunRecord] | RunOutcome:
        record = self._transition(record, RunStage.COLLECTING)
        if self._cancelled(record):
            return self._cancel(record)
        try:
            snapshot = self.gateway.get_pull_snapshot(record.context.pr_number, run=record.context)
        except Exception:
            return self._stale(record)
        if self._cancelled(record):
            return self._cancel(record)
        return snapshot, record

    def _publication_problem(self, record: RunRecord) -> str | None:
        try:
            permission = self.gateway.get_actor_permission(record.context.trigger_actor)
            actor = Actor(record.context.trigger_actor, permission)
            if not authorize_command(record.context.command, actor, self.config).allowed:
                return "authorization_drift"
        except Exception:
            return "authorization_drift"
        try:
            pull = self.gateway.get_pull_ref(record.context.pr_number)
        except Exception:
            return "stale_pull_ref"
        run = record.context
        if not (pull.number == run.pr_number and pull.state == "open"
                and pull.target_repository == run.repository
                and pull.source_repository == run.source_repository
                and pull.source_head_sha == run.source_head_sha
                and pull.target_base_sha == run.target_base_sha
                and pull.target_base_ref == run.target_base_ref):
            return "stale_pull_ref"
        return None

    def _publish_permitted(self, record: RunRecord) -> bool:
        return not self._cancelled(record) and self._publication_problem(record) is None

    def _actor(self, event: EventContext) -> Actor | None:
        if not event.actor_login:
            return None
        try:
            return Actor(event.actor_login, self.gateway.get_actor_permission(event.actor_login))
        except Exception:
            return None

    def _transition(self, record: RunRecord, stage: RunStage) -> RunRecord:
        allowed = {
            RunStage.ACCEPTED: {RunStage.ACKNOWLEDGED, RunStage.COLLECTING, RunStage.COMPLETED},
            RunStage.ACKNOWLEDGED: {RunStage.COLLECTING, RunStage.COMPLETED},
            RunStage.COLLECTING: {RunStage.ANALYZING, RunStage.COMPLETED},
            RunStage.ANALYZING: {RunStage.VALIDATING, RunStage.COMPLETED},
            RunStage.VALIDATING: {RunStage.PUBLISHING, RunStage.COMPLETED},
            RunStage.PUBLISHING: {RunStage.COMPLETED},
        }
        if stage not in allowed.get(record.stage, set()):
            raise ValueError("invalid_stage_transition")
        return self._save(replace(record, stage=stage, updated_at=self.now()))

    def _finish(self, record: RunRecord, status: RunStatus, error: str | None,
                *, warnings: tuple[str, ...] = ()) -> RunOutcome:
        final = replace(record, stage=RunStage.COMPLETED, status=status,
                        warning_codes=tuple(dict.fromkeys((*record.warning_codes, *warnings))),
                        error_code=error, updated_at=self.now())
        self._save(final)
        return self._record_outcome(final)

    def _stale(self, record: RunRecord) -> RunOutcome:
        return self._finish(record, RunStatus.STALE, "stale_pull_ref")

    def _publication_failure(self, record: RunRecord, problem: str) -> RunOutcome:
        if problem == "stale_pull_ref":
            return self._stale(record)
        return self._finish(record, RunStatus.FAILED, problem)

    def _cancel(self, record: RunRecord) -> RunOutcome:
        return self._finish(record, RunStatus.CANCELED, "cancel_requested")

    def _cancelled(self, record: RunRecord) -> bool:
        return self.state.is_cancel_requested(record.context.pr_number, record.context.run_id)

    def _save(self, record: RunRecord) -> RunRecord:
        self.state.save(record)
        stored = self.state.get(record.context.pr_number, record.context.run_id) or record
        self._write_local_state(stored)
        return stored

    def _record(self, run: RunContext, stage: RunStage, status: RunStatus,
                *, warnings: tuple[str, ...] = ()) -> RunRecord:
        timestamp = self.now()
        return RunRecord(run, stage, status, _PROMPT_VERSION, None,
                         run.command.name is CommandName.REVIEW and run.event_name == "pull_request",
                         None, warnings, None, timestamp, timestamp)

    def _build_context(self, snapshot: PullSnapshot, run: RunContext) -> object:
        provider = getattr(self.engine, "provider", None)
        if provider is None:
            raise ValueError("context_builder_required")
        capabilities = provider.capabilities()
        return build_context_plan(snapshot, run_id=run.run_id, repository_id=run.repository_id,
                                  repository_limit=capabilities.context_window,
                                  backend_context_window=capabilities.context_window,
                                  output_reserve=capabilities.max_output_tokens,
                                  safety_reserve_ratio=self.config.context.safety_reserve_ratio,
                                  max_chunk_ratio=self.config.context.max_chunk_ratio)

    @staticmethod
    def _valid_event(event: object) -> bool:
        return (isinstance(event, EventContext) and isinstance(event.pr_number, int)
                and event.pr_number > 0 and bool(event.repository) and bool(event.idempotency_key))

    @staticmethod
    def _trigger_ref(event: EventContext):
        from tools.qykw.domain import TriggerRef
        return TriggerRef("issue_comment" if event.trigger_comment_kind.value == "issue" else "review_comment",
                          event.trigger_comment_id)  # type: ignore[arg-type]

    @staticmethod
    def _safe_advisory_body(result: object) -> str:
        title = getattr(result, "title", "qykw")
        body = getattr(result, "body", "暂时无法生成结果。")
        evidence = getattr(result, "evidence", ())
        limitations = getattr(result, "limitations", ())
        lines = [sanitize_public_text(title), "", sanitize_public_text(body)]
        for label, values in (("证据", evidence), ("限制", limitations)):
            if isinstance(values, tuple):
                safe_values = [sanitize_public_text(item) for item in values[:20]]
                if safe_values:
                    lines.extend(("", label + "：", *safe_values))
        return "\n".join(lines)

    @staticmethod
    def _outcome(run_id: str, status: RunStatus, stage: RunStage, error: str | None) -> RunOutcome:
        return RunOutcome(run_id, status, stage, error)

    @staticmethod
    def _record_outcome(record: RunRecord) -> RunOutcome:
        return RunOutcome(record.context.run_id, record.status, record.stage, record.error_code)

    def _write_local_state(self, record: RunRecord) -> None:
        if self._state_path is None:
            return
        payload = {
            "version": 1, "run_id": record.context.run_id,
            "repository_id": record.context.repository_id, "pr_number": record.context.pr_number,
            "head_sha": record.context.source_head_sha, "base_sha": record.context.target_base_sha,
            "stage": record.stage.value, "status": record.status.value,
            "error_code": record.error_code,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 4096:
            raise ValueError("local_state_too_large")
        path = self._state_path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=".qykw-state-", delete=False) as temporary:
            temporary.write(encoded)
            temporary_path = Path(temporary.name)
        try:
            try:
                os.chmod(temporary_path, 0o600)
            except OSError:
                pass
            os.replace(temporary_path, path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink(missing_ok=True)
