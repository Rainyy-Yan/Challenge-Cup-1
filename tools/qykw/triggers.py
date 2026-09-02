"""Normalize qykw events and make deterministic trigger decisions."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib

from tools.qykw.commands import parse_command
from tools.qykw.config import QykwConfig
from tools.qykw.domain import (
    CommandMode,
    CommandName,
    CommandRequest,
    CommentKind,
    EventContext,
    PullRef,
    RunContext,
    RunRecord,
    TriggerDecision,
)


_MAX_DECIMAL_DIGITS = 18


def normalize_event(
    event_name: str,
    payload: Mapping[str, object],
    *,
    repository_id: int,
    repository: str,
    workflow_run_id: int | None = None,
) -> EventContext | None:
    """Return pre-authorization event facts, or ``None`` for a no-op event."""

    if not _valid_repository(repository_id, repository, payload):
        return None

    action = _string(payload.get("action"), default="")
    actor_login = _login(payload.get("sender"))
    if event_name == "pull_request":
        return _normalize_pull_request(
            payload, repository_id, repository, action, actor_login
        )
    if event_name == "issue_comment":
        return _normalize_issue_comment(
            payload, repository_id, repository, action, actor_login
        )
    if event_name == "pull_request_review_comment":
        return _normalize_review_comment(
            payload, repository_id, repository, action, actor_login
        )
    if event_name == "workflow_dispatch":
        return _normalize_manual(
            payload,
            repository_id,
            repository,
            action,
            actor_login,
            workflow_run_id,
        )
    return None


def build_run_context(event: EventContext, pull: PullRef | None) -> RunContext | None:
    """Bind a normalized event to a complete, current trusted pull reference."""

    if not _is_current_trusted_pull(event, pull):
        return None
    return RunContext(
        run_id=make_run_id(event.pr_number, event.idempotency_key),
        idempotency_key=event.idempotency_key,
        repository_id=event.repository_id,
        repository=event.repository,
        pr_number=event.pr_number,
        event_name=event.event_name,
        event_action=event.action,
        source_repository=pull.source_repository,
        source_head_sha=pull.source_head_sha,
        target_base_sha=pull.target_base_sha,
        target_base_ref=pull.target_base_ref,
        command=event.command,
        trigger_actor=event.actor_login,
        trigger_comment_id=event.trigger_comment_id,
        trigger_comment_kind=event.trigger_comment_kind,
    )


def decide_trigger(
    event: EventContext,
    *,
    existing_run: RunRecord | None,
    initial_review_completed: bool,
    config: QykwConfig,
) -> TriggerDecision:
    """Decide whether an idempotent normalized event starts a run."""

    if existing_run is not None:
        return _decision(False, "duplicate", event)
    if event.command.name is CommandName.STOP:
        return _decision(False, "control_event", event)
    if event.event_name == "pull_request":
        return _automatic_decision(event, initial_review_completed, config)
    if event.event_name in {"issue_comment", "pull_request_review_comment"}:
        if event.command.name is CommandName.REREVIEW:
            return _decision(True, "explicit_rereview", event)
        return _decision(True, "comment_command", event)
    if event.event_name == "workflow_dispatch":
        return _decision(True, "manual", event)
    return _decision(False, "unsupported_event", event)


def make_run_id(pr_number: int, idempotency_key: str) -> str:
    """Create a stable public run identifier from a PR and idempotency key."""

    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32].upper()
    return f"QY-PR{pr_number}-{digest}"


def _normalize_pull_request(
    payload: Mapping[str, object],
    repository_id: int,
    repository: str,
    action: str,
    actor_login: str,
) -> EventContext | None:
    pull = _mapping(payload.get("pull_request"))
    if pull is None:
        return None
    pr_number = _pull_number(payload, pull)
    draft = pull.get("draft")
    head = _mapping(pull.get("head"))
    head_sha = _string(head.get("sha") if head is not None else None, default="")
    if pr_number is None or type(draft) is not bool or not head_sha:
        return None
    key = f"auto:{repository_id}:{pr_number}:{action}:{head_sha}"
    return EventContext(
        repository_id=repository_id,
        repository=repository,
        pr_number=pr_number,
        event_name="pull_request",
        action=action,
        actor_login=actor_login,
        source_head_hint=head_sha,
        idempotency_key=key,
        command=CommandRequest(
            CommandName.REVIEW,
            "draft" if draft else "",
            CommandMode.READ_ONLY,
        ),
    )


def _normalize_issue_comment(
    payload: Mapping[str, object],
    repository_id: int,
    repository: str,
    action: str,
    actor_login: str,
) -> EventContext | None:
    issue = _mapping(payload.get("issue"))
    if issue is None or _mapping(issue.get("pull_request")) is None:
        return None
    return _comment_event(
        payload,
        issue,
        repository_id,
        repository,
        action,
        actor_login,
        "issue_comment",
        CommentKind.ISSUE,
    )


def _normalize_review_comment(
    payload: Mapping[str, object],
    repository_id: int,
    repository: str,
    action: str,
    actor_login: str,
) -> EventContext | None:
    pull = _mapping(payload.get("pull_request"))
    if pull is None:
        return None
    return _comment_event(
        payload,
        pull,
        repository_id,
        repository,
        action,
        actor_login,
        "pull_request_review_comment",
        CommentKind.REVIEW,
    )


def _comment_event(
    payload: Mapping[str, object],
    pull_container: Mapping[str, object],
    repository_id: int,
    repository: str,
    action: str,
    actor_login: str,
    event_name: str,
    kind: CommentKind,
) -> EventContext | None:
    comment = _mapping(payload.get("comment"))
    pr_number = _positive_int(pull_container.get("number"))
    comment_id = _positive_int(comment.get("id") if comment is not None else None)
    body = _string(comment.get("body") if comment is not None else None, default="")
    command = parse_command(body)
    if pr_number is None or comment_id is None or command is None:
        return None
    return EventContext(
        repository_id=repository_id,
        repository=repository,
        pr_number=pr_number,
        event_name=event_name,
        action=action,
        actor_login=actor_login,
        source_head_hint=None,
        idempotency_key=f"comment:{repository_id}:{kind.value}:{comment_id}",
        command=command,
        trigger_comment_id=comment_id,
        trigger_comment_kind=kind,
    )


def _normalize_manual(
    payload: Mapping[str, object],
    repository_id: int,
    repository: str,
    action: str,
    actor_login: str,
    workflow_run_id: int | None,
) -> EventContext | None:
    inputs = _mapping(payload.get("inputs"))
    if inputs is None or _positive_int(workflow_run_id) is None:
        return None
    pr_number = _positive_int(inputs.get("pr_number"))
    command_name = _command_name(inputs.get("command"))
    if pr_number is None or command_name is None:
        return None
    command = CommandRequest(
        command_name,
        "",
        CommandMode.CHANGE
        if command_name in {CommandName.FIX, CommandName.IMPLEMENT}
        else CommandMode.READ_ONLY,
    )
    return EventContext(
        repository_id=repository_id,
        repository=repository,
        pr_number=pr_number,
        event_name="workflow_dispatch",
        action=action,
        actor_login=actor_login,
        source_head_hint=None,
        idempotency_key=f"manual:{repository_id}:{workflow_run_id}",
        command=command,
    )


def _automatic_decision(
    event: EventContext,
    initial_review_completed: bool,
    config: QykwConfig,
) -> TriggerDecision:
    if not config.review.auto_initial:
        return _decision(False, "automatic_initial_disabled", event)
    if event.action == "opened":
        if initial_review_completed:
            return _decision(False, "initial_review_completed", event)
        if event.source_head_hint is None:
            return _decision(False, "missing_source_head", event)
        return _decision(
            not _event_draft(event),
            "initial_opened" if not _event_draft(event) else "draft_opened",
            event,
        )
    if event.action == "ready_for_review":
        if initial_review_completed:
            return _decision(False, "initial_review_completed", event)
        return _decision(True, "initial_ready_for_review", event)
    if event.action == "reopened":
        if initial_review_completed:
            return _decision(False, "initial_review_completed", event)
        return _decision(True, "initial_reopened", event)
    return _decision(False, "automatic_event_ignored", event)


def _event_draft(event: EventContext) -> bool:
    """Read the normalized automatic-event draft fact without changing its key."""

    return event.command.argument == "draft"


def _decision(run: bool, reason: str, event: EventContext) -> TriggerDecision:
    return TriggerDecision(run, reason, event.idempotency_key)


def _valid_repository(
    repository_id: int,
    repository: str,
    payload: Mapping[str, object],
) -> bool:
    if _positive_int(repository_id) is None or not repository:
        return False
    payload_repository = _mapping(payload.get("repository"))
    if payload_repository is None:
        return True
    payload_id = payload_repository.get("id")
    payload_name = _string(payload_repository.get("full_name"), default="")
    return (
        (payload_id is None or payload_id == repository_id)
        and (not payload_name or payload_name == repository)
    )


def _is_current_trusted_pull(event: EventContext, pull: PullRef | None) -> bool:
    if pull is None:
        return False
    return (
        pull.number == event.pr_number
        and pull.state == "open"
        and pull.target_repository == event.repository
        and all(
            (
                pull.source_repository,
                pull.source_head_sha,
                pull.target_base_sha,
                pull.target_base_ref,
            )
        )
    )


def _pull_number(
    payload: Mapping[str, object], pull: Mapping[str, object]
) -> int | None:
    return _positive_int(payload.get("number")) or _positive_int(pull.get("number"))


def _login(value: object) -> str:
    sender = _mapping(value)
    return _string(sender.get("login") if sender is not None else None, default="")


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _string(value: object, *, default: str | None = None) -> str:
    return value if isinstance(value, str) else (default or "")


def _positive_int(value: object) -> int | None:
    if type(value) is int and value > 0:
        return value
    if (
        isinstance(value, str)
        and value
        and value.isascii()
        and value.isdecimal()
        and len(value) <= _MAX_DECIMAL_DIGITS
    ):
        try:
            parsed = int(value)
        except (ValueError, OverflowError):
            return None
        return parsed if parsed > 0 else None
    return None


def _command_name(value: object) -> CommandName | None:
    if not isinstance(value, str):
        return None
    try:
        return CommandName(value)
    except ValueError:
        return None
