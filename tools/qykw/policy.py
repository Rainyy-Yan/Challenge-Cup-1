"""Deterministic authorization for qykw commands."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from pathlib import PurePosixPath

from tools.qykw.change import (
    ChangeKind,
    ChangeRequest,
    FilePatch,
    PatchManifest,
    SourceBlob,
    SourceOmission,
    SourceTreeEntry,
    SourceTreeIndex,
    TrustedSourceFile,
    TrustedSourceTreeProvider,
    compute_manifest_digest,
    compute_source_tree_index_digest,
)
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
    r"(?:"
    r"\bsecrets\s*(?:\.|\[\s*['\"])|"
    r"\bgithub\s*(?:\.\s*token|\[\s*['\"]token['\"]\s*\])|"
    r"\bgithub_token\b|"
    r"\bqykw_[a-z0-9_]*(?:token|key|secret|password)\b"
    r")",
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
        ".gitmodules",
        "requirements-dev.txt",
    }
)
_UNSAFE_MODES = frozenset({"120000", "160000"})
_REGULAR_MODE = "100644"
_TREE_MODE = "040000"
_MAX_CHANGE_FILES = 20
_MAX_FILE_BYTES = 256 * 1024
_MAX_EDITS_PER_FILE = 100
_MAX_INSTRUCTION_BYTES = 16 * 1024
_MAX_PATH_BYTES = 1024
_MAX_COMPONENT_BYTES = 255
_MAX_PATCH_TEXT_BYTES = 512 * 1024
_MAX_MANIFEST_TEXT_BYTES = 2 * 1024 * 1024
_MAX_TOTAL_OUTPUT_BYTES = 2 * 1024 * 1024
_MAX_TRUSTED_SOURCE_FILES = 100
_MAX_TRUSTED_SOURCE_CONTEXT_BYTES = 650_000
_MAX_SOURCE_OMISSION_DETAILS = 100
_HIGH_CONFIDENCE_SECRET = re.compile(
    r"(?:"
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"\bgh[pousr]_[A-Za-z0-9]{20,}\b|"
    r"\bnpm_[A-Za-z0-9]{20,}\b|"
    r"\bAKIA[A-Z0-9]{16}\b|"
    r"(?:^|[^A-Za-z0-9_])_?(?:api[_-]?key|auth[_-]?token|password|secret|token)"
    r"\s*[:=]\s*['\"]?[A-Za-z0-9_./+=~-]{16,}['\"]?|"
    r"\bauthorization\s*[:=]\s*['\"]?bearer\s+[A-Za-z0-9._~+/=-]{16,}|"
    r"^[ \t]*machine[ \t]+\S+[ \t]+login[ \t]+\S+[ \t]+password[ \t]+"
    r"[^\s#]{8,}[ \t]*(?:#.*)?$|"
    r"^[ \t]*password[ \t]+[^\s#]{16,}[ \t]*(?:#.*)?$"
    r")",
    re.IGNORECASE | re.MULTILINE,
)
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul", "clock$", "conin$", "conout$"}
)
_WINDOWS_INVALID_CHARACTERS = frozenset('<>:"|?*')
_CREDENTIAL_BASENAMES = frozenset(
    {
        ".npmrc",
        ".netrc",
        ".pypirc",
        ".envrc",
        ".git-credentials",
    }
)
_CREDENTIAL_BACKUP_SUFFIXES = frozenset(
    {".bak", ".backup", ".old", ".orig", ".save", "~"}
)
_CREDENTIAL_PATH_SUFFIXES = (
    (".docker", "config.json"),
    (".aws", "credentials"),
    (".config", "gcloud", "application_default_credentials.json"),
    (".azure", "accesstokens.json"),
    (".kube", "config"),
)


class DeterministicChangePolicy:
    """Validate change authority and paths without consulting model output.

    Instances are scoped to one controller run.  ``validate_request`` freezes
    the trusted snapshot used by ``validate_manifest`` so a manifest cannot
    substitute repository metadata after authorization.
    """

    def __init__(
        self,
        config: QykwConfig,
        *,
        tree_provider: TrustedSourceTreeProvider,
    ) -> None:
        self._config = config
        self._tree_provider = tree_provider
        self._validated_request: ChangeRequest | None = None
        self._source_files: dict[str, ChangedFile] = {}
        self._tree_entries: dict[str, SourceTreeEntry] = {}
        self._tree_blobs: dict[str, SourceBlob] = {}
        self._selected_source_files: dict[str, TrustedSourceFile] = {}
        self._source_omissions: tuple[SourceOmission, ...] = ()
        self._source_omission_count = 0

    def validate_request(self, request: ChangeRequest, snapshot: PullSnapshot) -> None:
        self._validated_request = None
        self._source_files = {}
        self._tree_entries = {}
        self._tree_blobs = {}
        self._selected_source_files = {}
        self._source_omissions = ()
        self._source_omission_count = 0
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
        if _utf8_size(request.instruction) > _MAX_INSTRUCTION_BYTES:
            raise ValueError("instruction_too_large")
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

        try:
            source_tree = self._tree_provider.get_complete_tree(
                request.source_repository,
                request.source_head_sha,
            )
        except Exception:
            raise ValueError("source_tree_unavailable") from None
        if type(source_tree) is not SourceTreeIndex:
            raise ValueError("invalid_source_tree_result")
        tree_entries, tree_blobs = self._validate_source_tree(
            source_tree,
            request.source_head_sha,
        )

        source_files: dict[str, ChangedFile] = {}
        for file in snapshot.changed_files:
            normalized = _normalize_repository_path(file.path)
            key = _collision_key(normalized)
            if key in source_files:
                raise ValueError("duplicate_snapshot_path")
            tree_entry = tree_entries.get(key)
            removed = file.status.casefold() in {"removed", "deleted"}
            if removed:
                if tree_entry is not None:
                    raise ValueError("source_snapshot_tree_mismatch")
            elif (
                tree_entry is None
                or tree_entry.path != normalized
                or tree_entry.mode != file.head_mode
                or tree_entry.git_sha != file.head_sha
            ):
                raise ValueError("source_snapshot_tree_mismatch")
            source_files[key] = file
        self._validated_request = request
        self._source_files = source_files
        self._tree_entries = tree_entries
        self._tree_blobs = tree_blobs
        selected, omissions, omission_count = self._select_trusted_source_files()
        self._selected_source_files = {
            _collision_key(source.path): source for source in selected
        }
        self._source_omissions = omissions
        self._source_omission_count = omission_count

    def _validate_source_tree(
        self, index: SourceTreeIndex, source_head_sha: str
    ) -> tuple[dict[str, SourceTreeEntry], dict[str, SourceBlob]]:
        if index.schema_version != 1 or index.complete is not True:
            raise ValueError("complete_source_tree_required")
        if index.source_head_sha != source_head_sha:
            raise ValueError("source_tree_head_mismatch")
        if not _is_git_oid(index.root_tree_sha):
            raise ValueError("invalid_root_tree_sha")
        if not _is_sha256(index.digest) or index.digest != compute_source_tree_index_digest(
            index
        ):
            raise ValueError("source_tree_digest_mismatch")

        entries: dict[str, SourceTreeEntry] = {}
        for entry in index.entries:
            path = _normalize_repository_path(entry.path)
            key = _collision_key(path)
            if key in entries:
                raise ValueError("source_tree_path_collision")
            if entry.kind == "tree":
                valid = entry.mode == _TREE_MODE
            elif entry.kind == "blob":
                valid = entry.mode in {_REGULAR_MODE, "100755", "120000"}
            elif entry.kind == "commit":
                valid = entry.mode == "160000"
            else:
                valid = False
            if not valid or not _is_git_oid(entry.git_sha):
                raise ValueError("invalid_source_tree_entry")
            entries[key] = entry

        for entry in entries.values():
            parent = PurePosixPath(entry.path).parent
            if parent == PurePosixPath("."):
                continue
            parent_entry = entries.get(_collision_key(parent.as_posix()))
            if (
                parent_entry is None
                or parent_entry.path != parent.as_posix()
                or parent_entry.kind != "tree"
                or parent_entry.mode != _TREE_MODE
            ):
                raise ValueError("incomplete_source_tree_parent")

        blobs: dict[str, SourceBlob] = {}
        for blob in index.blobs:
            path = _normalize_repository_path(blob.path)
            key = _collision_key(path)
            if key in blobs:
                raise ValueError("source_blob_path_collision")
            entry = entries.get(key)
            if (
                entry is None
                or entry.path != path
                or entry.kind != "blob"
                or entry.mode != blob.mode
                or entry.git_sha != blob.git_sha
                or _git_blob_object_sha(blob.content, entry.git_sha) != entry.git_sha
            ):
                raise ValueError("source_blob_tree_mismatch")
            blobs[key] = blob
        return entries, blobs

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
        if not hmac.compare_digest(manifest.digest, compute_manifest_digest(manifest)):
            raise ValueError("manifest_digest_mismatch")
        if not manifest.files or len(manifest.files) > _MAX_CHANGE_FILES:
            raise ValueError("change_file_limit")

        seen: set[str] = set()
        manifest_text_bytes = 0
        total_output_bytes = 0
        for patch in manifest.files:
            path = _normalize_change_path(patch.path)
            key = _collision_key(path)
            if key in seen:
                raise ValueError("duplicate_patch_path")
            seen.add(key)
            patch_text_bytes = _patch_text_size(patch)
            if patch_text_bytes > _MAX_PATCH_TEXT_BYTES:
                raise ValueError("patch_text_limit")
            manifest_text_bytes += patch_text_bytes
            if manifest_text_bytes > _MAX_MANIFEST_TEXT_BYTES:
                raise ValueError("manifest_text_limit")
            total_output_bytes += self._validate_patch(path, patch)
            if total_output_bytes > _MAX_TOTAL_OUTPUT_BYTES:
                raise ValueError("total_output_limit")

    def trusted_source_files(
        self, request: ChangeRequest
    ) -> tuple[TrustedSourceFile, ...]:
        """Return a bounded UTF-8 view derived only from the validated Head."""

        if self._validated_request != request:
            raise ValueError("request_not_validated")
        return tuple(self._selected_source_files.values())

    def trusted_source_omissions(
        self, request: ChangeRequest
    ) -> tuple[SourceOmission, ...]:
        if self._validated_request != request:
            raise ValueError("request_not_validated")
        return self._source_omissions

    def trusted_source_omission_count(self, request: ChangeRequest) -> int:
        if self._validated_request != request:
            raise ValueError("request_not_validated")
        return self._source_omission_count

    def _select_trusted_source_files(
        self,
    ) -> tuple[tuple[TrustedSourceFile, ...], tuple[SourceOmission, ...], int]:
        selected: list[TrustedSourceFile] = []
        omissions: list[SourceOmission] = []
        omission_count = 0
        context_bytes = 0

        def omit(path: str, reason: str) -> None:
            nonlocal omission_count
            omission_count += 1
            if len(omissions) < _MAX_SOURCE_OMISSION_DETAILS:
                omissions.append(SourceOmission(path, reason))

        for key, source in sorted(
            self._source_files.items(), key=lambda item: item[1].path
        ):
            entry = self._tree_entries.get(key)
            try:
                path = _normalize_change_path(source.path)
            except ValueError:
                omit(source.path, "sensitive_or_unsafe_path")
                continue
            if (
                entry is None
                or entry.path != path
                or entry.kind != "blob"
                or entry.mode != _REGULAR_MODE
                or source.binary
                or source.generated
                or source.status.casefold() in {"removed", "deleted"}
                or source.head_mode != _REGULAR_MODE
                or source.head_sha != entry.git_sha
                or source.head_content is None
            ):
                omit(path, "unsafe_source_file")
                continue
            content = source.head_content
            try:
                encoded = content.encode("utf-8")
            except UnicodeEncodeError:
                omit(path, "non_utf8")
                continue
            if (
                not content
                or len(encoded) > _MAX_FILE_BYTES
                or _git_blob_object_sha(encoded, entry.git_sha) != entry.git_sha
            ):
                omit(path, "invalid_source_content")
                continue
            if _HIGH_CONFIDENCE_SECRET.search(content):
                omit(path, "secret_content")
                continue
            source_view = TrustedSourceFile(
                path=path,
                mode=entry.mode,
                content=content,
                sha256=hashlib.sha256(encoded).hexdigest(),
            )
            serialized_bytes = len(
                json.dumps(
                    {
                        "path": source_view.path,
                        "mode": source_view.mode,
                        "sha256": source_view.sha256,
                        "content": source_view.content,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            if len(selected) >= _MAX_TRUSTED_SOURCE_FILES:
                omit(path, "file_count_budget")
                continue
            if context_bytes + serialized_bytes > _MAX_TRUSTED_SOURCE_CONTEXT_BYTES:
                omit(path, "context_budget")
                continue
            context_bytes += serialized_bytes
            selected.append(source_view)
        return tuple(selected), tuple(omissions), omission_count

    def _validate_patch(self, path: str, patch: FilePatch) -> int:
        key = _collision_key(path)
        tree_entry = self._tree_entries.get(key)
        if tree_entry is not None and tree_entry.path != path:
            raise ValueError("source_tree_path_collision")
        parent = PurePosixPath(path).parent
        while parent != PurePosixPath("."):
            parent_path = parent.as_posix()
            parent_entry = self._tree_entries.get(_collision_key(parent_path))
            if (
                parent_entry is None
                or parent_entry.path != parent_path
                or parent_entry.kind != "tree"
                or parent_entry.mode != _TREE_MODE
            ):
                raise ValueError("unsafe_source_parent")
            parent = parent.parent
        if not patch.edits or len(patch.edits) > _MAX_EDITS_PER_FILE:
            raise ValueError("invalid_edit_count")
        if patch.create:
            if tree_entry is not None:
                raise ValueError("create_path_exists")
            if patch.base_sha256 is not None:
                raise ValueError("new_file_base_digest_forbidden")
            if len(patch.edits) != 1 or patch.edits[0].before != "":
                raise ValueError("invalid_new_file_edit")
            return _validate_text(patch.edits[0].after, allow_empty=False)

        if (
            tree_entry is None
            or tree_entry.kind != "blob"
            or tree_entry.mode != _REGULAR_MODE
        ):
            raise ValueError("unknown_source_path")
        source = self._source_files.get(key)
        blob = self._tree_blobs.get(key)
        trusted_source = self._selected_source_files.get(key)
        if trusted_source is None or trusted_source.path != path:
            raise ValueError("source_not_in_generation_scope")
        if source is None and blob is None:
            raise ValueError("source_content_unavailable")
        if source is not None and source.path != path:
            raise ValueError("source_path_collision")
        if source is not None:
            if source.head_mode != tree_entry.mode or source.head_sha != tree_entry.git_sha:
                raise ValueError("source_snapshot_tree_mismatch")
            source_content = source.head_content
            if (
                source_content is not None
                and _git_blob_object_sha(source_content.encode("utf-8"), tree_entry.git_sha)
                != tree_entry.git_sha
            ):
                raise ValueError("source_content_tree_mismatch")
        else:
            assert blob is not None
            try:
                source_content = blob.content.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError("non_utf8_text") from error
        if source is not None and (
            source.binary
            or source.generated
            or source.status.casefold() in {"removed", "deleted"}
        ):
            raise ValueError("unsafe_source_file")
        if source is not None and (
            source.head_mode in _UNSAFE_MODES or source_content is None
        ):
            raise ValueError("unsafe_source_file")
        if not _is_sha256(patch.base_sha256):
            raise ValueError("invalid_base_digest")

        if source_content is None:
            raise ValueError("source_content_unavailable")
        _validate_text(source_content, allow_empty=False)
        try:
            trusted_sha256 = hashlib.sha256(source_content.encode("utf-8")).hexdigest()
        except UnicodeEncodeError:
            raise ValueError("non_utf8_text") from None
        if (
            patch.base_sha256 != trusted_sha256
            or patch.base_sha256 != trusted_source.sha256
        ):
            raise ValueError("base_digest_mismatch")
        content = source_content
        for edit in patch.edits:
            _validate_text(edit.before, allow_empty=False)
            _validate_text(edit.after, allow_empty=False)
            if edit.before == edit.after:
                raise ValueError("empty_edit")
            if content.count(edit.before) != 1:
                raise ValueError("ambiguous_edit")
            content = content.replace(edit.before, edit.after, 1)
            if _utf8_size(content) > _MAX_FILE_BYTES:
                raise ValueError("change_file_too_large")
        return _validate_text(content, allow_empty=False)


def _normalize_change_path(value: str) -> str:
    normalized = _normalize_repository_path(value)
    path = PurePosixPath(normalized)
    folded = tuple(part.casefold() for part in path.parts)
    if any(_is_sensitive_component(part) for part in folded):
        raise ValueError("sensitive_path_forbidden")
    if len(folded) >= 2 and folded[0] == "tools" and folded[1] == "qykw":
        raise ValueError("qykw_self_change_forbidden")
    if _collision_key(normalized) in _SENSITIVE_PATHS:
        raise ValueError("sensitive_path_forbidden")
    if _is_credential_path(normalized):
        raise ValueError("credential_path_forbidden")
    return normalized


def _normalize_repository_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("invalid_change_path")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("noncanonical_unicode_path")
    if any(unicodedata.category(character) in {"Cc", "Cf"} for character in value):
        raise ValueError("path_control_character")
    if any(character in _WINDOWS_INVALID_CHARACTERS for character in value):
        raise ValueError("windows_invalid_path_character")
    if _utf8_size(value) > _MAX_PATH_BYTES:
        raise ValueError("path_too_long")
    if value.startswith("/") or _WINDOWS_ABSOLUTE.match(value):
        raise ValueError("absolute_path_forbidden")
    raw_parts = value.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ValueError("path_traversal_forbidden")
    for part in raw_parts:
        if part.endswith((" ", ".")):
            raise ValueError("path_trailing_character")
        if _utf8_size(part) > _MAX_COMPONENT_BYTES:
            raise ValueError("path_component_too_long")
        reserved_stem = part.split(".", 1)[0].casefold()
        if (
            reserved_stem in _WINDOWS_RESERVED
            or re.fullmatch(r"(?:com|lpt)(?:[1-9]|[¹²³])", reserved_stem)
            is not None
        ):
            raise ValueError("windows_reserved_path")
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


def _is_credential_path(path: str) -> bool:
    parts = tuple(part.casefold() for part in PurePosixPath(path).parts)
    if any(
        _is_credential_filename(part, basename)
        for part in parts
        for basename in _CREDENTIAL_BASENAMES
    ):
        return True
    return any(
        len(parts) >= len(suffix)
        and parts[-len(suffix):-1] == suffix[:-1]
        and _is_credential_filename(parts[-1], suffix[-1])
        for suffix in _CREDENTIAL_PATH_SUFFIXES
    )


def _is_credential_filename(candidate: str, credential_name: str) -> bool:
    return candidate == credential_name or any(
        candidate == credential_name + suffix
        for suffix in _CREDENTIAL_BACKUP_SUFFIXES
    )


def _validate_text(value: str, *, allow_empty: bool) -> int:
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
    return len(encoded)


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _HEX_SHA256.fullmatch(value) is not None


def _is_git_oid(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) in {40, 64}
        and all(character in "0123456789abcdef" for character in value)
    )


def _git_blob_object_sha(content: bytes, expected_oid: str) -> str:
    framed = f"blob {len(content)}\0".encode("ascii") + content
    if len(expected_oid) == 40:
        return hashlib.sha1(framed).hexdigest()
    if len(expected_oid) == 64:
        return hashlib.sha256(framed).hexdigest()
    raise ValueError("invalid_git_oid")


def _collision_key(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def _patch_text_size(patch: FilePatch) -> int:
    size = _utf8_size(patch.path) + 32
    if patch.base_sha256 is not None:
        size += _utf8_size(patch.base_sha256)
    for edit in patch.edits:
        if not isinstance(edit.before, str) or not isinstance(edit.after, str):
            raise ValueError("invalid_edit_text")
        size += _utf8_size(edit.before) + _utf8_size(edit.after) + 32
    return size


def _utf8_size(value: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise ValueError("non_utf8_text") from error


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
