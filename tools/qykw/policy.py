"""Deterministic authorization for qykw commands."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from tools.qykw.change import ChangeKind, ChangeRequest, FilePatch, PatchManifest
from tools.qykw.config import QykwConfig
from tools.qykw.domain import (
    Actor,
    AuthorizationDecision,
    ChangedFile,
    CommentKind,
    CommandMode,
    CommandName,
    CommandRequest,
    PullSnapshot,
    RepositoryPermission,
)


_CHANGE_COMMANDS = frozenset({CommandName.FIX, CommandName.IMPLEMENT})
_WRITE_PERMISSIONS = frozenset(
    {
        RepositoryPermission.WRITE,
        RepositoryPermission.MAINTAIN,
        RepositoryPermission.ADMIN,
    }
)
_CHANGE_EVENT_NAMES = frozenset({"issue_comment", "pull_request_review_comment"})
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:")
_SECRET_REFERENCE = re.compile(
    r"(?:\$\{\{\s*secrets\.|\bsecrets\s*[.[]|\bgithub_token\b|\bqykw_[a-z0-9_]*token\b)",
    re.IGNORECASE,
)
_SENSITIVE_COMPONENTS = frozenset(
    {
        ".git",
        ".github",
        "codeowners",
        "agents.md",
        ".env",
        ".credentials",
        "permissions",
        "permission",
        "secrets",
        "secret",
        "tokens",
        "token",
        "credentials",
        "credential",
        "rulesets",
        "ruleset",
        "collaborators",
        "collaborator",
    }
)
_SENSITIVE_PATHS = frozenset(
    {
        "tools/check_qykw_coverage.py",
        ".coveragerc",
        "requirements-dev.txt",
    }
)
_UNSAFE_MODES = frozenset({"120000", "160000"})
_MAX_CHANGE_FILES = 20
_MAX_FILE_BYTES = 256 * 1024
_MAX_EDITS_PER_FILE = 100


class DeterministicChangePolicy:
    """Validate change authority and paths without consulting model output.

    Instances are scoped to one controller run.  ``validate_request`` freezes
    the trusted snapshot used by ``validate_manifest`` so a manifest cannot
    substitute repository metadata after authorization.
    """

    def __init__(self, config: QykwConfig) -> None:
        self._config = config
        self._validated_request: ChangeRequest | None = None
        self._source_files: dict[str, ChangedFile] = {}

    def validate_request(self, request: ChangeRequest, snapshot: PullSnapshot) -> None:
        context = request.context
        expected_kind = {
            CommandName.FIX: ChangeKind.FIX,
            CommandName.IMPLEMENT: ChangeKind.IMPLEMENT,
        }.get(context.command.name)
        if expected_kind is None or request.kind is not expected_kind:
            raise ValueError("change_command_required")
        if context.command.mode is not CommandMode.CHANGE:
            raise ValueError("change_mode_required")
        if context.event_name not in _CHANGE_EVENT_NAMES:
            raise ValueError("pr_comment_required")
        if context.event_action not in {"created", "edited"}:
            raise ValueError("unsupported_comment_action")
        if context.trigger_comment_id is None or context.trigger_comment_kind not in {
            CommentKind.ISSUE,
            CommentKind.REVIEW,
        }:
            raise ValueError("pr_comment_required")
        if (
            context.event_name == "issue_comment"
            and context.trigger_comment_kind is not CommentKind.ISSUE
        ) or (
            context.event_name == "pull_request_review_comment"
            and context.trigger_comment_kind is not CommentKind.REVIEW
        ):
            raise ValueError("comment_kind_mismatch")
        if not _is_configured_writer(context.trigger_actor, self._config):
            raise ValueError("change_actor_not_allowed")
        if (
            not isinstance(request.instruction, str)
            or not request.instruction.strip()
            or request.instruction != context.command.argument
        ):
            raise ValueError("instruction_mismatch")
        if request.verification_profile != "full":
            raise ValueError("verification_profile_not_allowed")
        if "full" not in self._config.verification.profiles:
            raise ValueError("verification_profile_not_configured")
        if snapshot.state.casefold() != "open" or snapshot.number != context.pr_number:
            raise ValueError("pull_snapshot_mismatch")

        expected = (
            (request.source_repository, context.source_repository, snapshot.source_repository),
            (request.target_repository, context.repository, snapshot.target_repository),
            (request.source_head_sha, context.source_head_sha, snapshot.source_head_sha),
            (request.target_base_sha, context.target_base_sha, snapshot.target_base_sha),
            (request.target_base_ref, context.target_base_ref, snapshot.target_base_ref),
        )
        if any(left != middle or middle != right for left, middle, right in expected):
            raise ValueError("pull_snapshot_mismatch")

        source_files: dict[str, ChangedFile] = {}
        for file in snapshot.changed_files:
            normalized = _normalize_repository_path(file.path)
            if normalized in source_files:
                raise ValueError("duplicate_snapshot_path")
            source_files[normalized] = file
        self._validated_request = request
        self._source_files = source_files

    def validate_manifest(self, request: ChangeRequest, manifest: PatchManifest) -> None:
        if self._validated_request != request:
            raise ValueError("request_not_validated")
        if manifest.schema_version != 1:
            raise ValueError("unsupported_manifest_schema")
        expected = (
            (manifest.run_id, request.context.run_id),
            (manifest.source_repository, request.source_repository),
            (manifest.target_repository, request.target_repository),
            (manifest.source_pr_number, request.context.pr_number),
            (manifest.source_head_sha, request.source_head_sha),
            (manifest.target_base_sha, request.target_base_sha),
            (manifest.target_base_ref, request.target_base_ref),
            (manifest.verification_profile, request.verification_profile),
        )
        if any(actual != trusted for actual, trusted in expected):
            raise ValueError("manifest_binding_mismatch")
        if not _is_sha256(manifest.digest):
            raise ValueError("invalid_manifest_digest")
        if not manifest.files or len(manifest.files) > _MAX_CHANGE_FILES:
            raise ValueError("change_file_limit")

        seen: set[str] = set()
        for patch in manifest.files:
            path = _normalize_change_path(patch.path)
            if path in seen:
                raise ValueError("duplicate_patch_path")
            seen.add(path)
            self._validate_patch(path, patch)

    def _validate_patch(self, path: str, patch: FilePatch) -> None:
        source = self._source_files.get(path)
        parent = PurePosixPath(path).parent
        while parent != PurePosixPath("."):
            parent_source = self._source_files.get(parent.as_posix())
            if parent_source is not None and parent_source.head_mode in _UNSAFE_MODES:
                raise ValueError("unsafe_source_parent")
            parent = parent.parent
        if not patch.edits or len(patch.edits) > _MAX_EDITS_PER_FILE:
            raise ValueError("invalid_edit_count")
        if patch.create:
            if source is not None:
                raise ValueError("create_path_exists")
            if patch.base_sha256 is not None:
                raise ValueError("new_file_base_digest_forbidden")
            if len(patch.edits) != 1 or patch.edits[0].before != "":
                raise ValueError("invalid_new_file_edit")
            _validate_text(patch.edits[0].after, allow_empty=False)
            return

        if source is None:
            raise ValueError("unknown_source_path")
        if source.binary or source.generated or source.status.casefold() in {
            "removed",
            "deleted",
        }:
            raise ValueError("unsafe_source_file")
        if source.head_mode in _UNSAFE_MODES or source.head_content is None:
            raise ValueError("unsafe_source_file")
        if not _is_sha256(patch.base_sha256):
            raise ValueError("invalid_base_digest")

        _validate_text(source.head_content, allow_empty=False)
        for edit in patch.edits:
            _validate_text(edit.before, allow_empty=False)
            _validate_text(edit.after, allow_empty=False)
            if edit.before == edit.after:
                raise ValueError("empty_edit")


def _normalize_change_path(value: str) -> str:
    normalized = _normalize_repository_path(value)
    path = PurePosixPath(normalized)
    folded = tuple(part.casefold() for part in path.parts)
    if any(_is_sensitive_component(part) for part in folded):
        raise ValueError("sensitive_path_forbidden")
    if len(folded) >= 2 and folded[0] == "tools" and folded[1] == "qykw":
        raise ValueError("qykw_self_change_forbidden")
    if normalized.casefold() in _SENSITIVE_PATHS:
        raise ValueError("sensitive_path_forbidden")
    return normalized


def _normalize_repository_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("invalid_change_path")
    if value.startswith("/") or _WINDOWS_ABSOLUTE.match(value):
        raise ValueError("absolute_path_forbidden")
    raw_parts = value.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ValueError("path_traversal_forbidden")
    path = PurePosixPath(value)
    normalized = path.as_posix()
    if normalized != value:
        raise ValueError("noncanonical_path")
    return normalized


def _is_sensitive_component(part: str) -> bool:
    if part in _SENSITIVE_COMPONENTS or part.startswith((".env.", ".credentials.")):
        return True
    stem = part.split(".", 1)[0]
    return bool(
        re.match(
            r"^(?:permissions?|secrets?|tokens?|credentials?)(?:$|[_-])",
            stem,
        )
    )


def _validate_text(value: str, *, allow_empty: bool) -> None:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError("empty_file_forbidden")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("non_utf8_text") from error
    if len(encoded) > _MAX_FILE_BYTES:
        raise ValueError("change_file_too_large")
    if "\x00" in value:
        raise ValueError("binary_content_forbidden")
    if _SECRET_REFERENCE.search(value):
        raise ValueError("secret_reference_forbidden")


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _HEX_SHA256.fullmatch(value) is not None


def authorize_command(
    command: CommandRequest,
    actor: Actor,
    config: QykwConfig,
    *,
    run_trigger_actor: str | None = None,
) -> AuthorizationDecision:
    """Authorize a command using only parsed data and trusted configuration."""

    if actor.permission is RepositoryPermission.NONE:
        return AuthorizationDecision(False, "repository_member_required")
    if command.name not in config.commands.enabled_commands:
        return AuthorizationDecision(False, "command_disabled")

    if command.name in _CHANGE_COMMANDS:
        if not _is_configured_writer(actor.login, config):
            return AuthorizationDecision(False, "change_actor_not_allowed")
        if actor.permission not in _WRITE_PERMISSIONS:
            return AuthorizationDecision(False, "change_permission_denied")
        return AuthorizationDecision(False, "capability_disabled")

    if command.name is CommandName.STOP:
        if _same_login(actor.login, run_trigger_actor) or _is_configured_writer(
            actor.login, config
        ):
            return AuthorizationDecision(True, "allowed")
        return AuthorizationDecision(False, "stop_actor_not_allowed")

    return AuthorizationDecision(True, "allowed")


def _is_configured_writer(login: str, config: QykwConfig) -> bool:
    return any(_same_login(login, writer) for writer in config.authorization.code_writers)


def _same_login(left: str, right: str | None) -> bool:
    return right is not None and left.casefold() == right.casefold()
