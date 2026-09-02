"""Immutable contracts for qykw's authorized change channel."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from pathlib import Path
from typing import Protocol

from tools.qykw.domain import PullSnapshot, RunContext
from tools.qykw.provider import InferenceProvider, validate_provider_capabilities
from tools.qykw.state import RunStateStore


class ChangeKind(str, Enum):
    FIX = "fix"
    IMPLEMENT = "implement"


@dataclass(frozen=True)
class ChangeRequest:
    context: RunContext
    kind: ChangeKind
    instruction: str
    source_repository: str
    target_repository: str
    source_head_sha: str
    target_base_sha: str
    target_base_ref: str
    verification_profile: str


@dataclass(frozen=True)
class TextEdit:
    before: str
    after: str

    def __post_init__(self) -> None:
        if type(self.before) is not str or type(self.after) is not str:
            raise TypeError("text_edit_scalars_must_be_strings")


@dataclass(frozen=True)
class FilePatch:
    path: str
    base_sha256: str | None
    create: bool
    edits: tuple[TextEdit, ...]

    def __post_init__(self) -> None:
        if type(self.path) is not str:
            raise TypeError("patch_path_must_be_string")
        if self.base_sha256 is not None and type(self.base_sha256) is not str:
            raise TypeError("patch_base_sha256_must_be_string_or_none")
        if type(self.create) is not bool:
            raise TypeError("patch_create_must_be_bool")
        _require_tuple_items(self.edits, TextEdit, "edits")


@dataclass(frozen=True)
class PatchManifest:
    schema_version: int
    run_id: str
    source_repository: str
    target_repository: str
    source_pr_number: int
    source_head_sha: str
    target_base_sha: str
    target_base_ref: str
    verification_profile: str
    files: tuple[FilePatch, ...]
    digest: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int:
            raise TypeError("manifest_schema_version_must_be_int")
        if type(self.source_pr_number) is not int:
            raise TypeError("manifest_source_pr_number_must_be_int")
        scalar_strings = (
            self.run_id,
            self.source_repository,
            self.target_repository,
            self.source_head_sha,
            self.target_base_sha,
            self.target_base_ref,
            self.verification_profile,
            self.digest,
        )
        if any(type(value) is not str for value in scalar_strings):
            raise TypeError("manifest_scalar_must_be_string")
        _require_tuple_items(self.files, FilePatch, "files")


@dataclass(frozen=True)
class FileDigest:
    path: str
    mode: str
    sha256: str


@dataclass(frozen=True)
class PreparedWorkspace:
    root: Path
    source_head_sha: str
    source_files: tuple[FileDigest, ...]

    def __post_init__(self) -> None:
        _require_tuple_items(self.source_files, FileDigest, "source_files")


@dataclass(frozen=True)
class CommandResult:
    name: str
    argv_digest: str
    exit_code: int | None
    timed_out: bool
    duration_ms: int
    output_digest: str
    output_excerpt: str


@dataclass(frozen=True)
class VerificationAttestation:
    schema_version: int
    workflow_run_id: int
    run_id: str
    source_repository: str
    source_head_sha: str
    target_repository: str
    target_base_sha: str
    target_base_ref: str
    manifest_digest: str
    profile: str
    image_digest: str
    output_tree_digest: str
    workspace_tree_digest: str
    output_files: tuple[FileDigest, ...]
    success: bool
    canceled: bool
    results: tuple[CommandResult, ...]

    def __post_init__(self) -> None:
        _require_tuple_items(self.output_files, FileDigest, "output_files")
        _require_tuple_items(self.results, CommandResult, "results")


@dataclass(frozen=True)
class AppliedPatch:
    files: tuple[FileDigest, ...]
    output_tree_digest: str
    workspace_tree_digest: str

    def __post_init__(self) -> None:
        _require_tuple_items(self.files, FileDigest, "files")


@dataclass(frozen=True)
class CommitIdentity:
    login: str
    name: str
    email: str


@dataclass(frozen=True)
class SourceBlob:
    path: str
    mode: str
    content: bytes
    git_sha: str

    def __post_init__(self) -> None:
        if type(self.content) is not bytes:
            raise TypeError("content_must_be_bytes")


@dataclass(frozen=True)
class SourceTreeEntry:
    path: str
    mode: str
    kind: str
    git_sha: str


@dataclass(frozen=True)
class SourceTreeIndex:
    """Controller-owned, complete listing of one fixed source Head tree."""

    schema_version: int
    source_head_sha: str
    root_tree_sha: str
    complete: bool
    entries: tuple[SourceTreeEntry, ...]
    blobs: tuple[SourceBlob, ...]
    digest: str

    def __post_init__(self) -> None:
        _require_tuple_items(self.entries, SourceTreeEntry, "entries")
        _require_tuple_items(self.blobs, SourceBlob, "blobs")


@dataclass(frozen=True)
class TrustedSourceFile:
    """A controller-derived UTF-8 source view bound to the validated Head."""

    path: str
    mode: str
    content: str
    sha256: str


@dataclass(frozen=True)
class SourceOmission:
    """Path-only reason for excluding source text from inference context."""

    path: str
    reason: str


_MAX_TRUSTED_SOURCE_CONTEXT_BYTES = 650_000
_MAX_SOURCE_OMISSION_DETAILS = 100
_MAX_SOURCE_OMISSION_COUNT = 10_000
_SOURCE_OMISSION_REASONS = frozenset(
    {
        "sensitive_or_unsafe_path",
        "unsafe_source_file",
        "non_utf8",
        "invalid_source_content",
        "secret_content",
        "file_count_budget",
        "context_budget",
    }
)


class TrustedSourceTreeProvider(Protocol):
    """Controller capability that reads one fixed repository Head tree."""

    def get_complete_tree(
        self, source_repository: str, source_head_sha: str
    ) -> SourceTreeIndex: ...


def compute_source_tree_index_digest(index: SourceTreeIndex) -> str:
    """Return the canonical digest binding a complete source-tree index."""

    entries = sorted(
        (
            {
                "path": entry.path,
                "mode": entry.mode,
                "kind": entry.kind,
                "git_sha": entry.git_sha,
            }
            for entry in index.entries
        ),
        key=lambda item: item["path"],
    )
    blobs = sorted(
        (
            {
                "path": blob.path,
                "mode": blob.mode,
                "git_sha": blob.git_sha,
                "content_sha256": hashlib.sha256(blob.content).hexdigest(),
            }
            for blob in index.blobs
        ),
        key=lambda item: item["path"],
    )
    payload = {
        "schema_version": index.schema_version,
        "source_head_sha": index.source_head_sha,
        "root_tree_sha": index.root_tree_sha,
        "complete": index.complete,
        "entries": entries,
        "blobs": blobs,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(b"qykw-source-tree-index-v1\0" + encoded).hexdigest()


@dataclass(frozen=True)
class PublishedFile:
    path: str
    mode: str
    content: bytes
    sha256: str

    def __post_init__(self) -> None:
        if type(self.content) is not bytes:
            raise TypeError("content_must_be_bytes")


@dataclass(frozen=True)
class GitTreeEntry:
    path: str
    mode: str
    blob_sha: str


class WriteState(str, Enum):
    NOT_CREATED = "not_created"
    CREATED = "created"
    UNKNOWN = "unknown"


class PublicationStage(str, Enum):
    PREFLIGHT = "preflight"
    BLOBS = "blobs"
    TREE = "tree"
    COMMIT = "commit"
    REF = "ref"
    PULL = "pull"
    COMPLETED = "completed"


class WriteKind(str, Enum):
    BLOB = "blob"
    TREE = "tree"
    COMMIT = "commit"
    REF = "ref"
    PULL = "pull"


@dataclass(frozen=True)
class WriteReceipt:
    kind: WriteKind
    target: str
    object_id: str | None
    state: WriteState


@dataclass(frozen=True)
class PublishedCommit:
    commit_sha: str
    tree_sha: str


@dataclass(frozen=True)
class PublicationRequest:
    change: ChangeRequest
    manifest: PatchManifest
    attestation: VerificationAttestation
    branch_name: str
    title: str
    body: str


@dataclass(frozen=True)
class ChangePublication:
    stage: PublicationStage
    branch_name: str
    branch_state: WriteState
    pull_state: WriteState
    commit_sha: str | None
    pull_number: int | None
    receipts: tuple[WriteReceipt, ...]
    partial: bool
    error_code: str | None

    def __post_init__(self) -> None:
        _require_tuple_items(self.receipts, WriteReceipt, "receipts")


class PatchGenerator(Protocol):
    def generate(
        self,
        request: ChangeRequest,
        snapshot: PullSnapshot,
        state_store: RunStateStore,
    ) -> PatchManifest: ...


class SandboxVerifier(Protocol):
    def verify(
        self,
        request: ChangeRequest,
        manifest: PatchManifest,
        workspace: PreparedWorkspace,
    ) -> VerificationAttestation: ...


class ChangePublisher(Protocol):
    def publish(self, request: PublicationRequest) -> ChangePublication: ...


class ChangePolicy(Protocol):
    def validate_request(self, request: ChangeRequest, snapshot: PullSnapshot) -> None: ...

    def validate_manifest(
        self, request: ChangeRequest, manifest: PatchManifest
    ) -> None: ...

    def trusted_source_files(
        self, request: ChangeRequest
    ) -> tuple[TrustedSourceFile, ...]: ...

    def trusted_source_omissions(
        self, request: ChangeRequest
    ) -> tuple[SourceOmission, ...]: ...

    def trusted_source_omission_count(self, request: ChangeRequest) -> int: ...


def prepare_change(
    request: ChangeRequest,
    snapshot: PullSnapshot,
    provider: InferenceProvider,
    policy: ChangePolicy,
    state_store: RunStateStore,
) -> PatchManifest:
    """Generate and validate one deterministic patch manifest.

    The provider can propose only file paths and exact text replacements.  All
    run bindings, the verification profile, source baselines, ordering, and
    digest are supplied or recomputed by this controller.
    """

    policy.validate_request(request, snapshot)
    source_files = _validate_trusted_source_files(
        policy.trusted_source_files(request)
    )
    omissions = _validate_source_omissions(
        policy.trusted_source_omissions(request)
    )
    omission_count = policy.trusted_source_omission_count(request)
    if (
        type(omission_count) is not int
        or not len(omissions) <= omission_count <= _MAX_SOURCE_OMISSION_COUNT
    ):
        raise ValueError("invalid_source_omission_count")
    if not source_files and omissions:
        raise ValueError("no_safe_source_context")

    # Imported lazily so prompt builders can type-reference the immutable
    # change contracts without creating an import cycle.
    from tools.qykw.prompts import build_change_patch_request

    inference_request = build_change_patch_request(
        request, source_files, omissions, omission_count
    )
    _raise_if_canceled(request, state_store)
    validate_provider_capabilities(provider, inference_request)
    response = provider.complete(inference_request)
    _raise_if_canceled(request, state_store)

    files = _parse_patch_files(response.value)
    provisional = PatchManifest(
        schema_version=1,
        run_id=request.context.run_id,
        source_repository=request.source_repository,
        target_repository=request.target_repository,
        source_pr_number=request.context.pr_number,
        source_head_sha=request.source_head_sha,
        target_base_sha=request.target_base_sha,
        target_base_ref=request.target_base_ref,
        verification_profile=request.verification_profile,
        files=tuple(sorted(files, key=lambda patch: patch.path)),
        digest="",
    )
    manifest = PatchManifest(
        schema_version=provisional.schema_version,
        run_id=provisional.run_id,
        source_repository=provisional.source_repository,
        target_repository=provisional.target_repository,
        source_pr_number=provisional.source_pr_number,
        source_head_sha=provisional.source_head_sha,
        target_base_sha=provisional.target_base_sha,
        target_base_ref=provisional.target_base_ref,
        verification_profile=provisional.verification_profile,
        files=provisional.files,
        digest=compute_manifest_digest(provisional),
    )
    policy.validate_manifest(request, manifest)
    _raise_if_canceled(request, state_store)
    return manifest


def canonical_manifest_bytes(
    manifest: PatchManifest, *, include_digest: bool
) -> bytes:
    """Serialize a manifest with stable path ordering and edit ordering."""

    if type(manifest) is not PatchManifest or type(include_digest) is not bool:
        raise TypeError("invalid_manifest_serialization_input")
    if include_digest and manifest.digest != compute_manifest_digest(manifest):
        raise ValueError("manifest_digest_mismatch")
    payload: dict[str, object] = {
        "schema_version": manifest.schema_version,
        "run_id": manifest.run_id,
        "source_repository": manifest.source_repository,
        "target_repository": manifest.target_repository,
        "source_pr_number": manifest.source_pr_number,
        "source_head_sha": manifest.source_head_sha,
        "target_base_sha": manifest.target_base_sha,
        "target_base_ref": manifest.target_base_ref,
        "verification_profile": manifest.verification_profile,
        "files": [
            {
                "path": patch.path,
                "base_sha256": patch.base_sha256,
                "create": patch.create,
                "edits": [
                    {"before": edit.before, "after": edit.after}
                    for edit in patch.edits
                ],
            }
            for patch in sorted(manifest.files, key=lambda item: item.path)
        ],
    }
    if include_digest:
        payload["digest"] = manifest.digest
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, UnicodeError):
        raise ValueError("manifest_non_utf8") from None


def compute_manifest_digest(manifest: PatchManifest) -> str:
    """Bind every trusted field and exact replacement to one SHA-256."""

    encoded = canonical_manifest_bytes(manifest, include_digest=False)
    return hashlib.sha256(b"qykw-patch-manifest-v1\0" + encoded).hexdigest()


def _parse_patch_files(value: object) -> tuple[FilePatch, ...]:
    if type(value) is not dict or set(value) != {"files"}:
        raise ValueError("invalid_patch_response")
    raw_files = value["files"]
    if type(raw_files) is not list or not 1 <= len(raw_files) <= 20:
        raise ValueError("invalid_patch_file_count")
    parsed: list[FilePatch] = []
    for raw_patch in raw_files:
        if type(raw_patch) is not dict or set(raw_patch) != {
            "path", "base_sha256", "create", "edits"
        }:
            raise ValueError("invalid_patch_file")
        path = raw_patch["path"]
        base_sha256 = raw_patch["base_sha256"]
        create = raw_patch["create"]
        raw_edits = raw_patch["edits"]
        if type(path) is not str or not path:
            raise TypeError("invalid_patch_path")
        if base_sha256 is not None and type(base_sha256) is not str:
            raise TypeError("invalid_patch_base_digest")
        if type(create) is not bool:
            raise TypeError("invalid_patch_create")
        if type(raw_edits) is not list or not 1 <= len(raw_edits) <= 100:
            raise ValueError("invalid_patch_edit_count")
        edits: list[TextEdit] = []
        for raw_edit in raw_edits:
            if type(raw_edit) is not dict or set(raw_edit) != {"before", "after"}:
                raise ValueError("invalid_text_edit")
            before = raw_edit["before"]
            after = raw_edit["after"]
            if type(before) is not str or type(after) is not str:
                raise TypeError("invalid_text_edit_type")
            try:
                before.encode("utf-8")
                after.encode("utf-8")
            except UnicodeEncodeError:
                raise ValueError("non_utf8_text") from None
            edits.append(TextEdit(before, after))
        parsed.append(FilePatch(path, base_sha256, create, tuple(edits)))
    return tuple(parsed)


def _validate_trusted_source_files(
    source_files: object,
) -> tuple[TrustedSourceFile, ...]:
    if type(source_files) is not tuple or len(source_files) > 100:
        raise ValueError("invalid_trusted_source_view")
    previous_path: str | None = None
    context_bytes = 0
    for source in source_files:
        if (
            type(source) is not TrustedSourceFile
            or type(source.path) is not str
            or type(source.mode) is not str
            or type(source.content) is not str
            or type(source.sha256) is not str
            or source.mode != "100644"
        ):
            raise ValueError("invalid_trusted_source_view")
        if previous_path is not None and source.path <= previous_path:
            raise ValueError("invalid_trusted_source_order")
        previous_path = source.path
        try:
            encoded = source.content.encode("utf-8")
            path_bytes = source.path.encode("utf-8")
        except (AttributeError, UnicodeEncodeError):
            raise ValueError("invalid_trusted_source_view") from None
        if source.sha256 != hashlib.sha256(encoded).hexdigest():
            raise ValueError("invalid_trusted_source_view")
        context_bytes += len(
            json.dumps(
                {
                    "path": source.path,
                    "mode": source.mode,
                    "sha256": source.sha256,
                    "content": source.content,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        if (
            not source.path
            or len(path_bytes) > 240
            or not source.content
            or len(encoded) > 256 * 1024
            or context_bytes > _MAX_TRUSTED_SOURCE_CONTEXT_BYTES
        ):
            raise ValueError("invalid_trusted_source_view")
    return source_files


def _validate_source_omissions(
    omissions: object,
) -> tuple[SourceOmission, ...]:
    if type(omissions) is not tuple or len(omissions) > _MAX_SOURCE_OMISSION_DETAILS:
        raise ValueError("invalid_source_omissions")
    previous_path: str | None = None
    for omission in omissions:
        if (
            type(omission) is not SourceOmission
            or type(omission.path) is not str
            or type(omission.reason) is not str
        ):
            raise ValueError("invalid_source_omissions")
        try:
            encoded_path = omission.path.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("invalid_source_omissions") from None
        if (
            not omission.path
            or omission.reason not in _SOURCE_OMISSION_REASONS
            or len(encoded_path) > 240
            or (previous_path is not None and omission.path <= previous_path)
        ):
            raise ValueError("invalid_source_omissions")
        previous_path = omission.path
    return omissions


def _raise_if_canceled(
    request: ChangeRequest, state_store: RunStateStore
) -> None:
    if state_store.is_cancel_requested(
        request.context.pr_number, request.context.run_id
    ):
        raise ValueError("change_canceled")


def _require_tuple_items(value: object, item_type: type, field: str) -> None:
    if type(value) is not tuple:
        raise TypeError(f"{field}_must_be_tuple")
    if any(type(item) is not item_type for item in value):
        raise TypeError(f"{field}_contains_invalid_item")
