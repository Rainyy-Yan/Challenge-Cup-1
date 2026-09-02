"""Comment-backed, versioned qykw run state.

Only comments authored by the canonical bot are trusted.  Cancellation has a
separate append-only marker so a delayed status save cannot undo a stop.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import json
import logging
import re
import threading
from typing import Protocol

from tools.qykw.domain import (
    CancelRecord, CommandMode, CommandName, CommandRequest, CommentKind,
    CoverageReport, IssueComment, RunContext, RunRecord, RunStage, RunStatus,
)
from tools.qykw.github import GitHubGateway


_BOT_LOGIN = "qykw"
_MARKER_LIMIT = 12_000
_JSON_LIMIT = 10_000
_MAX_ITEMS = 100
_STATE_PATTERN = re.compile(r"<!--\s*qykw-state:v1\s+(\{[^<>]{1,10000}\})\s*-->")
_LEGACY_STATE_PATTERN = re.compile(r"<!--\s*qykw-state\s+(\{[^<>]{1,10000}\})\s*-->")
_CANCEL_PATTERN = re.compile(r"<!--\s*qykw-cancel:v1\s+(\{[^<>]{1,4000}\})\s*-->")
_TIME_PATTERN = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z$")
_STAGE_ORDER = {stage: index for index, stage in enumerate(RunStage)}
_TERMINAL = frozenset({RunStatus.COMPLETED, RunStatus.PARTIAL, RunStatus.FAILED, RunStatus.CANCELED, RunStatus.STALE})
_CREATE_LOCKS_GUARD = threading.Lock()
_CREATE_LOCKS: dict[tuple[str, int], tuple[threading.Lock, int]] = {}


class RunStateStore(Protocol):
    def find_by_idempotency_key(self, pr_number: int, key: str) -> RunRecord | None: ...
    def find_latest(self, pr_number: int) -> RunRecord | None: ...
    def find_latest_active(self, pr_number: int) -> RunRecord | None: ...
    def has_successful_initial_review(self, pr_number: int) -> bool: ...
    def create(self, record: RunRecord) -> bool: ...
    def save(self, record: RunRecord) -> None: ...
    def get(self, pr_number: int, run_id: str) -> RunRecord | None: ...
    def is_cancel_requested(self, pr_number: int, run_id: str) -> bool: ...
    def request_cancel(self, pr_number: int, run_id: str, *, stop_comment_id: int,
                       actor_login: str) -> CancelRecord: ...


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _compact_json(value: Mapping[str, object]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return encoded.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def render_state_marker(record: RunRecord) -> str:
    """Return the only state format this version writes."""
    _validate_record(record)
    encoded = _compact_json(_record_payload(record))
    if len(encoded) > _JSON_LIMIT:
        raise ValueError("state_payload_too_large")
    return f"<!-- qykw-state:v1 {encoded} -->"


def render_cancel_marker(pr_number: int, run_id: str, stop_comment_id: int,
                         actor_login: str, created_at: str) -> str:
    payload = _cancel_payload(pr_number, run_id, stop_comment_id, actor_login, created_at)
    return f"<!-- qykw-cancel:v1 {_compact_json(payload)} -->"


class GitHubCommentStateStore:
    """A small state store whose sole persistence surface is bot issue comments."""

    def __init__(self, gateway: GitHubGateway, *, now: Callable[[], str] = _utc_now,
                 logger: logging.Logger | None = None, repository: str | None = None) -> None:
        self.gateway = gateway
        self._now = now
        self._logger = logger or logging.getLogger(__name__)
        self._repository = repository if repository is not None else getattr(gateway, "repository", None)

    def find_by_idempotency_key(self, pr_number: int, key: str) -> RunRecord | None:
        _valid_id(key, "idempotency_key")
        return _best((stored for stored in self._records(pr_number)
                      if stored[0].context.idempotency_key == key))

    def find_latest(self, pr_number: int) -> RunRecord | None:
        return _best(self._records(pr_number))

    def find_latest_active(self, pr_number: int) -> RunRecord | None:
        return _best(stored for stored in self._records(pr_number) if stored[0].status not in _TERMINAL)

    def has_successful_initial_review(self, pr_number: int) -> bool:
        return any(record.initial_review is True and record.context.command.name is CommandName.REVIEW
                   and record.status is RunStatus.COMPLETED
                   for record, _ in self._records(pr_number))

    def create(self, record: RunRecord) -> bool:
        _validate_record(record)
        self._assert_repository(record.context.repository)
        key = (self._repository or record.context.repository, record.context.pr_number)
        with _create_critical_section(key):
            # GitHub workflow FIFO remains the cross-process serialization
            # boundary.  This bounded registry closes only same-process races.
            if self.find_by_idempotency_key(record.context.pr_number, record.context.idempotency_key) is not None:
                return False
            self.gateway.create_issue_comment(record.context.pr_number, _state_body(record))
            claimed = self.find_by_idempotency_key(record.context.pr_number, record.context.idempotency_key)
            if claimed is None:
                raise RuntimeError("state_claim_unconfirmed")
            self._safe_log("state_created", record.context.pr_number)
            return claimed.context == record.context and claimed.prompt_version == record.prompt_version

    def save(self, record: RunRecord) -> None:
        _validate_record(record)
        self._assert_repository(record.context.repository)
        stored = self._stored_for_run(record.context.pr_number, record.context.run_id)
        if stored is None:
            raise ValueError("unknown_run")
        previous, comment_id = stored
        _assert_immutable(previous, record)
        # The target is a state comment selected from a fresh scan, never a
        # cancellation comment.  A late save therefore cannot erase a cancel.
        self.gateway.update_issue_comment(comment_id, _state_body(record))
        self._safe_log("state_saved", record.context.pr_number)

    def get(self, pr_number: int, run_id: str) -> RunRecord | None:
        _valid_id(run_id, "run_id")
        stored = self._stored_for_run(pr_number, run_id)
        return None if stored is None else stored[0]

    def is_cancel_requested(self, pr_number: int, run_id: str) -> bool:
        _valid_id(run_id, "run_id")
        # Always scan again; control and review workflows are intentionally
        # independent and may race.
        return any(cancel.target_run_id == run_id for cancel in self._cancels(pr_number))

    def request_cancel(self, pr_number: int, run_id: str, *, stop_comment_id: int,
                       actor_login: str) -> CancelRecord:
        _valid_id(run_id, "run_id")
        if not isinstance(stop_comment_id, int) or stop_comment_id <= 0:
            raise ValueError("invalid_stop_comment_id")
        _valid_id(actor_login, "actor_login")
        if self.get(pr_number, run_id) is None:
            raise ValueError("unknown_run")
        for existing in self._cancels(pr_number):
            if existing.stop_comment_id == stop_comment_id:
                return existing
        created_at = self._checked_time(self._now())
        cancel = CancelRecord(pr_number, run_id, stop_comment_id, actor_login, created_at)
        self.gateway.create_issue_comment(pr_number, _cancel_body(cancel))
        self._safe_log("cancel_requested", pr_number)
        return cancel

    def _records(self, pr_number: int) -> list[tuple[RunRecord, int]]:
        records: list[tuple[RunRecord, int]] = []
        for comment in self._comments(pr_number):
            record = _parse_state_comment(comment)
            if record is not None and record.context.pr_number == pr_number and self._matches_repository(record.context.repository):
                records.append((record, comment.comment_id))
        return records

    def _stored_for_run(self, pr_number: int, run_id: str) -> tuple[RunRecord, int] | None:
        return _best_stored(stored for stored in self._records(pr_number)
                            if stored[0].context.run_id == run_id)

    def _cancels(self, pr_number: int) -> list[CancelRecord]:
        return [cancel for comment in self._comments(pr_number)
                if (cancel := _parse_cancel_comment(comment)) is not None and cancel.pr_number == pr_number]

    def _comments(self, pr_number: int) -> tuple[IssueComment, ...]:
        if not isinstance(pr_number, int) or pr_number <= 0:
            raise ValueError("invalid_pr_number")
        comments = self.gateway.list_issue_comments(pr_number)
        return tuple(comment for comment in comments if comment.author_login == _BOT_LOGIN)

    def _assert_repository(self, repository: str) -> None:
        if not self._matches_repository(repository):
            raise ValueError("repository_mismatch")

    def _matches_repository(self, repository: str) -> bool:
        return self._repository is None or repository == self._repository

    def _checked_time(self, value: str) -> str:
        if not isinstance(value, str) or not _TIME_PATTERN.fullmatch(value):
            raise ValueError("invalid_timestamp")
        return value

    def _safe_log(self, event: str, pr_number: int) -> None:
        # Deliberately do not log marker data, comments, prompts, code, or errors.
        self._logger.info("qykw_%s pr=%d", event, pr_number)


def _state_body(record: RunRecord) -> str:
    return "qykw 运行状态已更新。\n\n" + render_state_marker(record)


@contextmanager
def _create_critical_section(key: tuple[str, int]):
    """Serialize one process without retaining idle lock entries indefinitely."""
    with _CREATE_LOCKS_GUARD:
        lock, users = _CREATE_LOCKS.get(key, (threading.Lock(), 0))
        _CREATE_LOCKS[key] = (lock, users + 1)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
        with _CREATE_LOCKS_GUARD:
            current, users = _CREATE_LOCKS[key]
            if users == 1:
                del _CREATE_LOCKS[key]
            else:
                _CREATE_LOCKS[key] = (current, users - 1)


def _cancel_body(record: CancelRecord) -> str:
    return "qykw 已登记停止请求。\n\n" + render_cancel_marker(
        record.pr_number, record.target_run_id, record.stop_comment_id,
        record.actor_login, record.created_at,
    )


def _best(items: object) -> RunRecord | None:
    stored = _best_stored(items)  # type: ignore[arg-type]
    return None if stored is None else stored[0]


def _best_stored(items: object) -> tuple[RunRecord, int] | None:
    values = list(items)  # type: ignore[arg-type]
    if not values:
        return None
    return max(values, key=lambda item: (_STAGE_ORDER[item[0].stage], item[0].updated_at, item[1]))


def _record_payload(record: RunRecord) -> dict[str, object]:
    context = record.context
    coverage = record.coverage
    return {
        "version": 1,
        "kind": "state",
        "context": {
            "run_id": context.run_id, "idempotency_key": context.idempotency_key,
            "repository_id": context.repository_id, "repository": context.repository,
            "pr_number": context.pr_number, "event_name": context.event_name,
            "event_action": context.event_action, "source_repository": context.source_repository,
            "source_head_sha": context.source_head_sha, "target_base_sha": context.target_base_sha,
            "target_base_ref": context.target_base_ref, "command": {
                "name": context.command.name.value, "argument": context.command.argument,
                "mode": context.command.mode.value,
            }, "trigger_actor": context.trigger_actor,
            "trigger_comment_id": context.trigger_comment_id,
            "trigger_comment_kind": None if context.trigger_comment_kind is None else context.trigger_comment_kind.value,
        },
        "stage": record.stage.value, "status": record.status.value,
        "prompt_version": record.prompt_version, "summary_comment_id": record.summary_comment_id,
        "initial_review": record.initial_review, "coverage": None if coverage is None else {
            "total_files": coverage.total_files, "reviewed_files": coverage.reviewed_files,
            "total_hunks": coverage.total_hunks, "reviewed_hunks": coverage.reviewed_hunks,
            "omissions": list(coverage.omissions), "explains_every_file": coverage.explains_every_file,
        }, "warning_codes": list(record.warning_codes), "error_code": record.error_code,
        "created_at": record.created_at, "updated_at": record.updated_at,
    }


def _cancel_payload(pr_number: int, run_id: str, stop_comment_id: int, actor_login: str,
                    created_at: str) -> dict[str, object]:
    if not isinstance(pr_number, int) or pr_number <= 0:
        raise ValueError("invalid_pr_number")
    _valid_id(run_id, "run_id")
    _valid_id(actor_login, "actor_login")
    if not isinstance(stop_comment_id, int) or stop_comment_id <= 0:
        raise ValueError("invalid_stop_comment_id")
    if not isinstance(created_at, str) or not _TIME_PATTERN.fullmatch(created_at):
        raise ValueError("invalid_timestamp")
    return {"version": 1, "kind": "cancel", "pr_number": pr_number, "target_run_id": run_id,
            "stop_comment_id": stop_comment_id, "actor_login": actor_login, "created_at": created_at}


def _parse_state_comment(comment: IssueComment) -> RunRecord | None:
    if comment.author_login != _BOT_LOGIN or len(comment.body) > _MARKER_LIMIT:
        return None
    match = _STATE_PATTERN.search(comment.body)
    legacy = False
    if match is None:
        match = _LEGACY_STATE_PATTERN.search(comment.body)
        legacy = True
    if match is None:
        return None
    payload = _parse_json(match.group(1))
    if payload is None:
        return None
    if legacy and "version" not in payload:
        payload = {"version": 1, "kind": "state", **payload}
    record = _record_from_payload(payload)
    # Early records predate their API comment id.  The id is local metadata,
    # not a reason to reject a valid signed-by-identity state marker.
    if record is not None and record.summary_comment_id is None:
        return replace(record, summary_comment_id=comment.comment_id)
    return record


def _parse_cancel_comment(comment: IssueComment) -> CancelRecord | None:
    if comment.author_login != _BOT_LOGIN or len(comment.body) > _MARKER_LIMIT:
        return None
    match = _CANCEL_PATTERN.search(comment.body)
    if match is None:
        return None
    payload = _parse_json(match.group(1))
    if payload is None or set(payload) != {"version", "kind", "pr_number", "target_run_id", "stop_comment_id", "actor_login", "created_at"}:
        return None
    try:
        if payload["version"] != 1 or payload["kind"] != "cancel":
            return None
        values = _cancel_payload(payload["pr_number"], payload["target_run_id"], payload["stop_comment_id"],
                                 payload["actor_login"], payload["created_at"])
        return CancelRecord(values["pr_number"], values["target_run_id"], values["stop_comment_id"],
                            values["actor_login"], values["created_at"])  # type: ignore[arg-type]
    except (KeyError, TypeError, ValueError):
        return None


def _parse_json(value: str) -> dict[str, object] | None:
    if len(value) > _JSON_LIMIT:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return dict(parsed) if isinstance(parsed, Mapping) else None


def _record_from_payload(payload: Mapping[str, object]) -> RunRecord | None:
    expected = {"version", "kind", "context", "stage", "status", "prompt_version", "summary_comment_id",
                "initial_review", "coverage", "warning_codes", "error_code", "created_at", "updated_at"}
    if set(payload) != expected or payload.get("version") != 1 or payload.get("kind") != "state":
        return None
    try:
        context = _context_from_payload(_mapping(payload["context"]))
        coverage = _coverage_from_payload(payload["coverage"])
        warnings = _strings(payload["warning_codes"], maximum=_MAX_ITEMS)
        summary_id = payload["summary_comment_id"]
        if summary_id is not None and (not isinstance(summary_id, int) or summary_id <= 0):
            return None
        if not isinstance(payload["initial_review"], bool) or not isinstance(payload["prompt_version"], str):
            return None
        if payload["error_code"] is not None and not isinstance(payload["error_code"], str):
            return None
        record = RunRecord(context, RunStage(payload["stage"]), RunStatus(payload["status"]),
            payload["prompt_version"], summary_id, payload["initial_review"], coverage, warnings,
            payload["error_code"], payload["created_at"], payload["updated_at"])
        _validate_record(record)
        return record
    except (KeyError, TypeError, ValueError):
        return None


def _context_from_payload(value: Mapping[str, object]) -> RunContext:
    expected = {"run_id", "idempotency_key", "repository_id", "repository", "pr_number", "event_name",
                "event_action", "source_repository", "source_head_sha", "target_base_sha", "target_base_ref",
                "command", "trigger_actor", "trigger_comment_id", "trigger_comment_kind"}
    if set(value) != expected:
        raise ValueError("invalid_context")
    command = _mapping(value["command"])
    if set(command) != {"name", "argument", "mode"}:
        raise ValueError("invalid_command")
    trigger_id = value["trigger_comment_id"]
    kind = value["trigger_comment_kind"]
    if trigger_id is not None and (not isinstance(trigger_id, int) or trigger_id <= 0):
        raise ValueError("invalid_context")
    if kind is not None and not isinstance(kind, str):
        raise ValueError("invalid_context")
    return RunContext(
        _string(value["run_id"]), _string(value["idempotency_key"]), _positive(value["repository_id"]),
        _string(value["repository"]), _positive(value["pr_number"]), _string(value["event_name"]),
        _string(value["event_action"]), _string(value["source_repository"]), _string(value["source_head_sha"]),
        _string(value["target_base_sha"]), _string(value["target_base_ref"]),
        CommandRequest(CommandName(_string(command["name"])), _string(command["argument"], allow_empty=True), CommandMode(_string(command["mode"]))),
        _string(value["trigger_actor"]), trigger_id, None if kind is None else CommentKind(kind),
    )


def _coverage_from_payload(value: object) -> CoverageReport | None:
    if value is None:
        return None
    data = _mapping(value)
    expected = {"total_files", "reviewed_files", "total_hunks", "reviewed_hunks", "omissions", "explains_every_file"}
    if set(data) != expected or not isinstance(data["explains_every_file"], bool):
        raise ValueError("invalid_coverage")
    return CoverageReport(_nonnegative(data["total_files"]), _nonnegative(data["reviewed_files"]),
        _nonnegative(data["total_hunks"]), _nonnegative(data["reviewed_hunks"]),
        _strings(data["omissions"], maximum=_MAX_ITEMS), data["explains_every_file"])


def _validate_record(record: RunRecord) -> None:
    if not isinstance(record, RunRecord) or not isinstance(record.context, RunContext):
        raise ValueError("invalid_record")
    context = record.context
    for value, name in ((context.run_id, "run_id"), (context.idempotency_key, "idempotency_key"),
                        (context.repository, "repository"), (context.source_head_sha, "source_head_sha"),
                        (context.target_base_sha, "target_base_sha"), (record.prompt_version, "prompt_version")):
        _valid_id(value, name)
    if not isinstance(context.pr_number, int) or context.pr_number <= 0:
        raise ValueError("invalid_pr_number")
    if not isinstance(record.stage, RunStage) or not isinstance(record.status, RunStatus):
        raise ValueError("invalid_record")
    if not isinstance(record.initial_review, bool) or not isinstance(record.warning_codes, tuple):
        raise ValueError("invalid_record")
    if record.coverage is not None and not isinstance(record.coverage, CoverageReport):
        raise ValueError("invalid_record")
    if record.error_code is not None and not isinstance(record.error_code, str):
        raise ValueError("invalid_record")
    if not isinstance(record.created_at, str) or not _TIME_PATTERN.fullmatch(record.created_at):
        raise ValueError("invalid_timestamp")
    if not isinstance(record.updated_at, str) or not _TIME_PATTERN.fullmatch(record.updated_at):
        raise ValueError("invalid_timestamp")


def _assert_immutable(previous: RunRecord, current: RunRecord) -> None:
    if previous.context != current.context or previous.prompt_version != current.prompt_version:
        raise ValueError("immutable_run_mismatch")


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("invalid_mapping")
    return value


def _string(value: object, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value) or len(value) > 512:
        raise ValueError("invalid_string")
    return value


def _valid_id(value: object, name: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 512 or any(character.isspace() for character in value):
        raise ValueError(f"invalid_{name}")


def _positive(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("invalid_integer")
    return value


def _nonnegative(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("invalid_integer")
    return value


def _strings(value: object, *, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError("invalid_strings")
    parsed = tuple(_string(item) for item in value)
    return parsed
