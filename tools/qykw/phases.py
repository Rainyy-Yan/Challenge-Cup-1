"""Credential-separated production controllers for qykw workflow phases."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Mapping

from tools.qykw.advisory import AdvisoryService
from tools.qykw.config import QykwConfig, load_qykw_config
from tools.qykw.context import build_context_plan
from tools.qykw.domain import (
    Actor, CommandMode, CommandName, CommandRequest, CoverageReport, DiffSide,
    Finding, PullRef, ReviewResult, RunContext, RunRecord, RunStage, RunStatus,
    Severity, TriggerRef,
)
from tools.qykw.github import HttpGitHubGateway
from tools.qykw.policy import authorize_command
from tools.qykw.provider import ResponsesInferenceProvider
from tools.qykw.publish import ReviewPublisher, sanitize_public_text
from tools.qykw.review import ReviewEngine
from tools.qykw.state import GitHubCommentStateStore
from tools.qykw.triggers import build_run_context, decide_trigger, normalize_event


_PROMPT_VERSION = "qykw-v1"
_MAX_TEXT = 2_000
_MAX_ITEMS = 20
_COMMON_ENVIRONMENT = frozenset({
    "GITHUB_ACTIONS", "GITHUB_API_URL", "GITHUB_EVENT_NAME", "GITHUB_EVENT_PATH",
    "GITHUB_REPOSITORY", "GITHUB_REPOSITORY_ID", "GITHUB_RUN_ID", "QYKW_CONFIG_PATH",
})
_REVIEW_ENVIRONMENT = _COMMON_ENVIRONMENT | {"QYKW_REVIEW_TOKEN"}
_ANALYZE_ENVIRONMENT = _COMMON_ENVIRONMENT | {
    "GITHUB_TOKEN", "QYKW_INFERENCE_API_KEY", "QYKW_INFERENCE_BASE_URL", "QYKW_INFERENCE_MODEL",
    "QYKW_INFERENCE_ALLOWED_HOSTS", "QYKW_INFERENCE_CONTEXT_WINDOW",
    "QYKW_INFERENCE_MAX_OUTPUT_TOKENS", "QYKW_INFERENCE_TIMEOUT_SECONDS",
}


class ProductionPhaseController:
    """One process owns exactly one credential boundary and workflow phase."""

    def __init__(self, phase: str, environment: Mapping[str, str] | None = None) -> None:
        self.phase = phase
        source = os.environ if environment is None else environment
        allowed = _ANALYZE_ENVIRONMENT if phase == "analyze" else _REVIEW_ENVIRONMENT
        self.environment = {key: value for key, value in source.items() if key in allowed}

    def root(self) -> dict[str, object]:
        event, reason = self._event()
        if event is None:
            return _skipped(self.phase, reason)
        if self.phase == "control":
            return self._control_event(event)
        return self._authorize_event(event)

    def authorize(self, artifact: dict[str, object]) -> dict[str, object]:
        del artifact
        return self.root()

    def control(self, artifact: dict[str, object]) -> dict[str, object]:
        del artifact
        return self.root()

    def analyze(self, artifact: dict[str, object]) -> dict[str, object]:
        run = _run_from_artifact(artifact)
        if run is None:
            return _skipped("analyze", "upstream_skipped")
        gateway, state, config = self._read_services()
        record = state.get(run.pr_number, run.run_id)
        if record is None or record.context != run:
            return _skipped("analyze", "state_unavailable", run)
        if state.is_cancel_requested(run.pr_number, run.run_id):
            return _analysis_artifact(run, {"kind": "none", "status": "canceled"})
        if not _matches_run(gateway.get_pull_ref(run.pr_number), run):
            return _analysis_artifact(run, {"kind": "none", "status": "stale"})
        snapshot = gateway.get_pull_snapshot(run.pr_number, run=run)
        if state.is_cancel_requested(run.pr_number, run.run_id):
            return _analysis_artifact(run, {"kind": "none", "status": "canceled"})
        if not _matches_run(gateway.get_pull_ref(run.pr_number), run):
            return _analysis_artifact(run, {"kind": "none", "status": "stale"})
        if run.command.name in {CommandName.HELP, CommandName.STATUS, CommandName.SUMMARY}:
            result = _deterministic(run, record)
            return _analysis_artifact(run, {"kind": "advisory", "status": "completed", "advisory": _advisory_payload(result)})
        provider = ResponsesInferenceProvider.from_env(self.environment)
        capabilities = provider.capabilities()
        plan = build_context_plan(snapshot, run_id=run.run_id, repository_id=run.repository_id,
                                  repository_limit=capabilities.context_window,
                                  backend_context_window=capabilities.context_window,
                                  output_reserve=capabilities.max_output_tokens,
                                  safety_reserve_ratio=config.context.safety_reserve_ratio,
                                  max_chunk_ratio=config.context.max_chunk_ratio)
        if run.command.name in {CommandName.ANALYZE, CommandName.PLAN}:
            result = AdvisoryService(provider).handle(run, plan, record)
            return _analysis_artifact(run, {"kind": "advisory", "status": "completed", "advisory": _advisory_payload(result)})
        review = ReviewEngine(provider, max_findings=config.review.max_findings).review(run, snapshot, plan)
        return _analysis_artifact(run, {"kind": "review", "status": "completed", "review": _review_payload(review)})

    def publish(self, artifact: dict[str, object]) -> dict[str, object]:
        run = _run_from_artifact(artifact)
        if run is None:
            return _skipped("publish", "upstream_skipped")
        payload = artifact["payload"]
        if not isinstance(payload, dict) or payload.get("status") != "completed":
            return _published_artifact(run, False, str(payload.get("status", "invalid")) if isinstance(payload, dict) else "invalid")
        gateway, state, config = self._review_services()
        record = state.get(run.pr_number, run.run_id)
        if record is None or record.context != run:
            return _published_artifact(run, False, "state_unavailable")
        if not self._publication_allowed(gateway, state, config, run):
            return _published_artifact(run, False, "publication_blocked")
        try:
            gateway.assert_bot_identity("qykw")
        except Exception:
            return _published_artifact(run, False, "bot_identity_unavailable")
        record = self._advance_for_publish(gateway, state, config, run, record)
        if record is None:
            return _published_artifact(run, False, "publication_blocked")
        if payload.get("kind") == "review":
            review = _review_from_payload(payload.get("review"))
            if review is None:
                return _published_artifact(run, False, "invalid_analysis")
            if not self._publication_allowed(gateway, state, config, run):
                return _published_artifact(run, False, "publication_blocked")
            state.save(replace(record, stage=RunStage.VALIDATING, coverage=review.coverage, updated_at=_now()))
            record = state.get(run.pr_number, run.run_id) or record
            if not self._publication_allowed(gateway, state, config, run):
                return _published_artifact(run, False, "publication_blocked")
            state.save(replace(record, stage=RunStage.PUBLISHING, updated_at=_now()))
            result = ReviewPublisher(gateway, state, max_findings=config.review.max_findings).publish_review(
                run, review, write_guard=lambda: self._publication_allowed(gateway, state, config, run)
            )
            if result.status not in {RunStatus.COMPLETED, RunStatus.PARTIAL}:
                return _published_artifact(run, False, "publish_failed")
            record = state.get(run.pr_number, run.run_id) or record
            if not self._publication_allowed(gateway, state, config, run):
                return _published_artifact(run, False, "publication_blocked")
            state.save(replace(record, stage=RunStage.COMPLETED, status=result.status, updated_at=_now(),
                               warning_codes=result.warning_codes))
            return _published_artifact(run, True, result.status.value)
        advice = _advisory_from_payload(payload.get("advisory"))
        if advice is None or not self._publication_allowed(gateway, state, config, run):
            return _published_artifact(run, False, "publication_blocked")
        state.save(replace(record, stage=RunStage.PUBLISHING, updated_at=_now()))
        if not self._publication_allowed(gateway, state, config, run):
            return _published_artifact(run, False, "publication_blocked")
        gateway.create_issue_comment(run.pr_number, _render_advisory(advice))
        record = state.get(run.pr_number, run.run_id) or record
        if not self._publication_allowed(gateway, state, config, run):
            return _published_artifact(run, False, "publication_blocked")
        state.save(replace(record, stage=RunStage.COMPLETED, status=RunStatus.COMPLETED, updated_at=_now()))
        return _published_artifact(run, True, "completed")

    def _advance_for_publish(self, gateway: HttpGitHubGateway, state: GitHubCommentStateStore,
                             config: QykwConfig, run: RunContext, record: RunRecord) -> RunRecord | None:
        current = record
        for stage in (RunStage.ACKNOWLEDGED, RunStage.COLLECTING, RunStage.ANALYZING):
            if not self._publication_allowed(gateway, state, config, run):
                return None
            state.save(replace(current, stage=stage, updated_at=_now()))
            current = state.get(run.pr_number, run.run_id) or current
        return current

    def record_failure(self, artifact: dict[str, object], error_code: str) -> dict[str, object]:
        run = _run_from_artifact(artifact)
        if run is None:
            return _skipped("record-failure", "upstream_skipped")
        gateway, state, _ = self._review_services()
        record = state.get(run.pr_number, run.run_id)
        if record is not None and record.context == run:
            try:
                gateway.assert_bot_identity("qykw")
            except Exception:
                return {"version": 1, "phase": "record-failure", "run": _run_payload(run),
                        "payload": {"error_code": "bot_identity_unavailable"}}
            state.save(replace(record, stage=RunStage.COMPLETED, status=RunStatus.FAILED,
                               error_code=error_code, updated_at=_now()))
        return {"version": 1, "phase": "record-failure", "run": _run_payload(run),
                "payload": {"error_code": error_code}}

    def _authorize_event(self, event: object) -> dict[str, object]:
        from tools.qykw.domain import EventContext
        if not isinstance(event, EventContext):
            return _skipped("authorize", "invalid_event")
        if event.command.name is CommandName.STOP:
            return _skipped("authorize", "review_lane_noop")
        try:
            gateway, state, config = self._review_services()
            decision = authorize_command(event.command, Actor(event.actor_login, gateway.get_actor_permission(event.actor_login)), config)
            if not decision.allowed:
                return _skipped("authorize", decision.reason)
            existing = state.find_by_idempotency_key(event.pr_number, event.idempotency_key)
            trigger = decide_trigger(event, existing_run=existing,
                                     initial_review_completed=state.has_successful_initial_review(event.pr_number), config=config)
            if not trigger.run:
                return _skipped("authorize", trigger.reason)
            run = build_run_context(event, gateway.get_pull_ref(event.pr_number))
            if run is None:
                return _skipped("authorize", "stale_pull_ref")
            record = RunRecord(run, RunStage.ACCEPTED, RunStatus.ACTIVE, _PROMPT_VERSION, None,
                               run.command.name is CommandName.REVIEW and run.event_name == "pull_request",
                               None, (), None, _now(), _now())
            gateway.assert_bot_identity("qykw")
            if not state.create(record):
                return _skipped("authorize", "duplicate")
            if event.trigger_comment_id is not None and event.trigger_comment_kind is not None:
                try:
                    kind = "issue_comment" if event.trigger_comment_kind.value == "issue" else "review_comment"
                    gateway.try_add_reaction(TriggerRef(kind, event.trigger_comment_id), "laugh")
                except Exception:
                    pass
            return {"version": 1, "phase": "authorize", "run": _run_payload(run),
                    "payload": {"authorization": "accepted"}}
        except Exception:
            return _skipped("authorize", "authorization_unavailable")

    def _control_event(self, event: object) -> dict[str, object]:
        from tools.qykw.domain import EventContext
        if not isinstance(event, EventContext) or event.command.name is not CommandName.STOP:
            return _skipped("control", "review_lane_noop")
        try:
            gateway, state, config = self._review_services()
            active = state.find_latest_active(event.pr_number)
            if active is None or event.trigger_comment_id is None:
                return _skipped("control", "no_active_run")
            if state.is_cancel_requested(event.pr_number, active.context.run_id):
                return _skipped("control", "duplicate")
            decision = authorize_command(event.command, Actor(event.actor_login, gateway.get_actor_permission(event.actor_login)), config,
                                         run_trigger_actor=active.context.trigger_actor)
            if not decision.allowed:
                return _skipped("control", decision.reason)
            gateway.assert_bot_identity("qykw")
            state.request_cancel(event.pr_number, active.context.run_id, stop_comment_id=event.trigger_comment_id,
                                 actor_login=event.actor_login)
            if event.trigger_comment_kind is not None:
                try:
                    kind = "issue_comment" if event.trigger_comment_kind.value == "issue" else "review_comment"
                    gateway.try_add_reaction(TriggerRef(kind, event.trigger_comment_id), "laugh")
                except Exception:
                    pass
            return {"version": 1, "phase": "control", "run": _run_payload(active.context),
                    "payload": {"stop_comment_id": event.trigger_comment_id}}
        except Exception:
            return _skipped("control", "authorization_unavailable")

    def _event(self) -> tuple[object | None, str]:
        path = self.environment.get("GITHUB_EVENT_PATH")
        if not path:
            return None, "event_unavailable"
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
            repository_id = int(self.environment.get("GITHUB_REPOSITORY_ID", ""))
            repository = self.environment.get("GITHUB_REPOSITORY", "")
            event_name = self.environment.get("GITHUB_EVENT_NAME", "")
            run_id = int(self.environment.get("GITHUB_RUN_ID", ""))
        except (OSError, ValueError, json.JSONDecodeError):
            return None, "event_unavailable"
        if not isinstance(raw, dict):
            return None, "invalid_event"
        normalized_name = "pull_request" if event_name == "pull_request_target" else event_name
        normalized = normalize_event(normalized_name, raw, repository_id=repository_id, repository=repository,
                                     workflow_run_id=run_id)
        return (normalized, "not_a_pull_request") if normalized is None else (normalized, "")

    def _review_services(self) -> tuple[HttpGitHubGateway, GitHubCommentStateStore, QykwConfig]:
        token = self.environment.get("QYKW_REVIEW_TOKEN")
        if not token:
            raise ValueError("review_credentials_unavailable")
        repository = self.environment.get("GITHUB_REPOSITORY", "")
        gateway = HttpGitHubGateway(self.environment.get("GITHUB_API_URL", "https://api.github.com"), repository, token, token)
        return gateway, GitHubCommentStateStore(gateway, repository=repository), self._config()

    def _read_services(self) -> tuple["_ReadGateway", GitHubCommentStateStore, QykwConfig]:
        token = self.environment.get("GITHUB_TOKEN")
        if not token:
            raise ValueError("read_credentials_unavailable")
        repository = self.environment.get("GITHUB_REPOSITORY", "")
        gateway = _ReadGateway(HttpGitHubGateway(self.environment.get("GITHUB_API_URL", "https://api.github.com"), repository, token, ""))
        return gateway, GitHubCommentStateStore(gateway, repository=repository), self._config()

    def _config(self) -> QykwConfig:
        configured = self.environment.get("QYKW_CONFIG_PATH", ".github/qykw.toml")
        return load_qykw_config(Path(configured))

    @staticmethod
    def _publication_allowed(gateway: HttpGitHubGateway, state: GitHubCommentStateStore,
                             config: QykwConfig, run: RunContext) -> bool:
        try:
            actor = Actor(run.trigger_actor, gateway.get_actor_permission(run.trigger_actor))
            return (authorize_command(run.command, actor, config).allowed
                    and not state.is_cancel_requested(run.pr_number, run.run_id)
                    and _matches_run(gateway.get_pull_ref(run.pr_number), run))
        except Exception:
            return False


def build_production_controller(phase: str) -> ProductionPhaseController:
    if phase not in {"control", "authorize", "analyze", "publish", "record-failure"}:
        raise ValueError("invalid_phase")
    return ProductionPhaseController(phase)


class _ReadGateway:
    """The analyze boundary exposes only GET-backed operations to its services."""

    def __init__(self, gateway: HttpGitHubGateway) -> None:
        self._gateway = gateway
        self.repository = gateway.repository

    def get_pull_ref(self, pr_number: int):
        return self._gateway.get_pull_ref(pr_number)

    def get_pull_snapshot(self, pr_number: int, *, run: RunContext):
        return self._gateway.get_pull_snapshot(pr_number, run=run)

    def list_issue_comments(self, pr_number: int):
        return self._gateway.list_issue_comments(pr_number)


def _skipped(phase: str, reason: str, run: RunContext | None = None) -> dict[str, object]:
    return {"version": 1, "phase": phase, "run": None if run is None else _run_payload(run),
            "payload": {"status": "skipped", "reason": reason}}


def _run_payload(run: RunContext) -> dict[str, object]:
    return {"run_id": run.run_id, "idempotency_key": run.idempotency_key, "repository_id": run.repository_id,
            "repository": run.repository, "pr_number": run.pr_number, "event_name": run.event_name,
            "event_action": run.event_action, "source_repository": run.source_repository,
            "source_head_sha": run.source_head_sha, "target_base_sha": run.target_base_sha,
            "target_base_ref": run.target_base_ref, "actor_login": run.trigger_actor,
            "command": {"name": run.command.name.value, "argument": run.command.argument, "mode": run.command.mode.value}}


def _run_from_artifact(artifact: Mapping[str, object]) -> RunContext | None:
    value = artifact.get("run")
    if value is None:
        return None
    if not isinstance(value, Mapping):
        return None
    command = value.get("command")
    try:
        if not isinstance(command, Mapping):
            return None
        return RunContext(str(value["run_id"]), str(value["idempotency_key"]), int(value["repository_id"]),
                          str(value["repository"]), int(value["pr_number"]), str(value["event_name"]),
                          str(value["event_action"]), str(value["source_repository"]), str(value["source_head_sha"]),
                          str(value["target_base_sha"]), str(value["target_base_ref"]),
                          CommandRequest(CommandName(str(command["name"])), str(command["argument"]), CommandMode(str(command["mode"]))),
                          str(value["actor_login"]))
    except (KeyError, TypeError, ValueError):
        return None


def _matches_run(pull: PullRef, run: RunContext) -> bool:
    return (pull.number == run.pr_number and pull.state == "open" and pull.target_repository == run.repository
            and pull.source_repository == run.source_repository and pull.source_head_sha == run.source_head_sha
            and pull.target_base_sha == run.target_base_sha and pull.target_base_ref == run.target_base_ref)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _analysis_artifact(run: RunContext, payload: Mapping[str, object]) -> dict[str, object]:
    return {"version": 1, "phase": "analyze", "run": _run_payload(run), "payload": dict(payload)}


def _published_artifact(run: RunContext, published: bool, status: str) -> dict[str, object]:
    return {"version": 1, "phase": "publish", "run": _run_payload(run),
            "payload": {"published": published, "status": status}}


def _deterministic(run: RunContext, record: RunRecord):
    class _NoProvider:
        def capabilities(self): raise ValueError("not_used")
        def complete(self, request): raise ValueError("not_used")
    return AdvisoryService(_NoProvider()).handle(run, None, record)


def _advisory_payload(result: object) -> dict[str, object]:
    return {"title": _bounded(getattr(result, "title", "qykw"), 160), "body": _bounded(getattr(result, "body", "请求状态不可用。"), 6_000),
            "evidence": [_bounded(item, _MAX_TEXT) for item in tuple(getattr(result, "evidence", ()))[:_MAX_ITEMS]],
            "limitations": [_bounded(item, _MAX_TEXT) for item in tuple(getattr(result, "limitations", ()))[:_MAX_ITEMS]]}


def _advisory_from_payload(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict) or set(value) != {"title", "body", "evidence", "limitations"}:
        return None
    if not isinstance(value["title"], str) or not isinstance(value["body"], str):
        return None
    if not all(isinstance(value[key], list) and len(value[key]) <= _MAX_ITEMS and all(isinstance(item, str) and len(item) <= _MAX_TEXT for item in value[key]) for key in ("evidence", "limitations")):
        return None
    return value


def _render_advisory(value: Mapping[str, object]) -> str:
    lines = [sanitize_public_text(value["title"]), "", sanitize_public_text(value["body"])]
    for label, key in (("证据", "evidence"), ("限制", "limitations")):
        entries = value[key]
        if isinstance(entries, list) and entries:
            lines.extend(("", label + "：", *(sanitize_public_text(item) for item in entries)))
    return "\n".join(lines)


def _review_payload(result: ReviewResult) -> dict[str, object]:
    return {"conclusion": _bounded(result.conclusion, 500), "findings": [_finding_payload(item) for item in result.findings[:_MAX_ITEMS]],
            "coverage": {"total_files": result.coverage.total_files, "reviewed_files": result.coverage.reviewed_files,
                         "total_hunks": result.coverage.total_hunks, "reviewed_hunks": result.coverage.reviewed_hunks,
                         "omissions": [_bounded(item, _MAX_TEXT) for item in result.coverage.omissions[:_MAX_ITEMS]],
                         "explains_every_file": result.coverage.explains_every_file},
            "validation_notes": [_bounded(item, _MAX_TEXT) for item in result.validation_notes[:_MAX_ITEMS]],
            "limitations": [_bounded(item, _MAX_TEXT) for item in result.limitations[:_MAX_ITEMS]]}


def _finding_payload(item: Finding) -> dict[str, object]:
    return {"path": _bounded(item.path, 1_024), "line": item.line, "side": item.side.value, "severity": item.severity.value,
            "failure_path": _bounded(item.failure_path, _MAX_TEXT), "impact": _bounded(item.impact, _MAX_TEXT),
            "evidence": _bounded(item.evidence, _MAX_TEXT), "suggestion": _bounded(item.suggestion, _MAX_TEXT),
            "verification": _bounded(item.verification, _MAX_TEXT), "fingerprint": _bounded(item.fingerprint, 128)}


def _review_from_payload(value: object) -> ReviewResult | None:
    if not isinstance(value, dict) or set(value) != {"conclusion", "findings", "coverage", "validation_notes", "limitations"}:
        return None
    coverage = value.get("coverage")
    if not isinstance(coverage, dict) or set(coverage) != {"total_files", "reviewed_files", "total_hunks", "reviewed_hunks", "omissions", "explains_every_file"}:
        return None
    try:
        report = CoverageReport(*(int(coverage[key]) for key in ("total_files", "reviewed_files", "total_hunks", "reviewed_hunks")),
                                tuple(_bounded(item, _MAX_TEXT) for item in coverage["omissions"]), bool(coverage["explains_every_file"]))
        findings = tuple(_finding_from_payload(item) for item in value["findings"])
        if not isinstance(value["conclusion"], str) or len(value["conclusion"]) > 500 or not all(findings): return None
        if not all(isinstance(item, str) and len(item) <= _MAX_TEXT for item in value["validation_notes"] + value["limitations"]): return None
        return ReviewResult(value["conclusion"], findings, report, tuple(value["validation_notes"]), tuple(value["limitations"]))  # type: ignore[arg-type]
    except (KeyError, TypeError, ValueError):
        return None


def _finding_from_payload(value: object) -> Finding | None:
    if not isinstance(value, dict) or set(value) != {"path", "line", "side", "severity", "failure_path", "impact", "evidence", "suggestion", "verification", "fingerprint"}:
        return None
    try:
        if not isinstance(value["line"], int) or value["line"] < 1: return None
        strings = [value[key] for key in ("path", "failure_path", "impact", "evidence", "suggestion", "verification", "fingerprint")]
        if not all(isinstance(item, str) and item and len(item) <= _MAX_TEXT for item in strings): return None
        return Finding(value["path"], value["line"], DiffSide(value["side"]), Severity(value["severity"]), value["failure_path"], value["impact"], value["evidence"], value["suggestion"], value["verification"], value["fingerprint"])
    except (KeyError, TypeError, ValueError):
        return None


def _bounded(value: object, maximum: int) -> str:
    return value[:maximum] if isinstance(value, str) and value else "信息不可用"
