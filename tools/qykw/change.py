"""Immutable contracts for qykw's authorized change channel."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from pathlib import Path
from typing import Protocol

from tools.qykw.domain import PullSnapshot, RunContext
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


@dataclass(frozen=True)
class FilePatch:
    path: str
    base_sha256: str | None
    create: bool
    edits: tuple[TextEdit, ...]

    def __post_init__(self) -> None:
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


def _require_tuple_items(value: object, item_type: type, field: str) -> None:
    if type(value) is not tuple:
        raise TypeError(f"{field}_must_be_tuple")
    if any(type(item) is not item_type for item in value):
        raise TypeError(f"{field}_contains_invalid_item")
