"""Publish one verified qykw change through a narrow Git Data capability."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from typing import Callable, Protocol, TypeVar
import unicodedata

from tools.qykw.change import (
    AppliedPatch,
    ChangeKind,
    ChangePolicy,
    ChangePublication,
    ChangeRequest,
    CommandResult,
    CommitIdentity,
    FileDigest,
    GitTreeEntry,
    PatchManifest,
    PublishedCommit,
    PublishedFile,
    PublicationRequest,
    PublicationStage,
    SourceBlob,
    SourceTreeEntry,
    VerificationAttestation,
    WriteKind,
    WriteReceipt,
    WriteState,
    compute_manifest_digest,
)
from tools.qykw.domain import (
    AuthenticatedUser,
    CommandMode,
    CommandName,
    PullSnapshot,
    RepositoryPermission,
    RunContext,
)
from tools.qykw.patches import (
    apply_patch_manifest,
    compute_workspace_tree_digest,
    materialize_workspace,
)
from tools.qykw.state import RunStateStore
from tools.qykw.verification import VerificationCommand, VerificationProfile


_BOT_LOGIN = "qykw"
_ALLOWED_PERMISSIONS = frozenset(
    {RepositoryPermission.WRITE, RepositoryPermission.MAINTAIN, RepositoryPermission.ADMIN}
)
_HEX = frozenset("0123456789abcdef")
_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_BRANCH_PART_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?$")
_SUPPORTED_BLOB_MODES = frozenset({"100644", "100755"})
_TREE_MODE = "040000"
_MAX_RESULT_EXCERPT = 4_000
_MAX_TREE_ENTRIES = 100_000


class ChangeGitHubGateway(Protocol):
    """Repository-bound capability; deliberately omits destructive operations."""

    @property
    def repository(self) -> str: ...

    def get_pull_snapshot(self, pr_number: int) -> PullSnapshot: ...
    def get_actor_permission(self, login: str) -> RepositoryPermission: ...
    def get_authenticated_user(self) -> AuthenticatedUser: ...
    def commit_exists(self, repository: str, commit_sha: str) -> bool: ...
    def get_commit_tree_sha(self, repository: str, commit_sha: str) -> str: ...
    def list_tree_entries(
        self, repository: str, commit_sha: str
    ) -> tuple[SourceTreeEntry, ...]: ...
    def get_changed_paths(
        self, repository: str, base_sha: str, head_sha: str
    ) -> tuple[str, ...]: ...
    def get_blob_at_commit(
        self, repository: str, commit_sha: str, path: str
    ) -> SourceBlob: ...
    def branch_exists(self, repository: str, branch_name: str) -> bool: ...
    def get_ref_target(self, repository: str, branch_name: str) -> str | None: ...
    def find_draft_pull_by_run_marker(
        self,
        repository: str,
        *,
        branch_name: str,
        head_sha: str,
        base_ref: str,
        run_id: str,
    ) -> int | None: ...
    def create_blob(self, *, repository: str, content: bytes) -> str: ...
    def create_tree(
        self,
        *,
        repository: str,
        base_tree_sha: str,
        entries: tuple[GitTreeEntry, ...],
    ) -> str: ...
    def create_commit(
        self,
        *,
        repository: str,
        parent_sha: str,
        tree_sha: str,
        message: str,
        identity: CommitIdentity,
    ) -> PublishedCommit: ...
    def create_ref(
        self, *, repository: str, branch_name: str, commit_sha: str
    ) -> None: ...
    def create_draft_pull_request(
        self,
        *,
        repository: str,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> int: ...


@dataclass(frozen=True)
class TrustedPublicationRuntime:
    """Controller-only expectations that must never be read from an attestation."""

    workflow_run_id: int
    image_digest: str
    profile: VerificationProfile
    bot_login: str = _BOT_LOGIN

    def __post_init__(self) -> None:
        if (
            type(self.workflow_run_id) is not int
            or self.workflow_run_id <= 0
            or type(self.image_digest) is not str
            or not _is_image_digest(self.image_digest)
            or type(self.profile) is not VerificationProfile
            or type(self.bot_login) is not str
            or self.bot_login != _BOT_LOGIN
        ):
            raise ValueError("invalid_trusted_publication_runtime")


@dataclass(frozen=True)
class PublicationJournalEntry:
    """One append-only durable transition, containing identifiers but no content."""

    schema_version: int
    run_id: str
    operation_id: str
    stage: PublicationStage
    kind: WriteKind
    target: str
    object_id: str | None
    state: WriteState

    def __post_init__(self) -> None:
        if (
            self.schema_version != 1
            or type(self.run_id) is not str
            or not self.run_id
            or type(self.operation_id) is not str
            or not self.operation_id
            or type(self.stage) is not PublicationStage
            or type(self.kind) is not WriteKind
            or type(self.target) is not str
            or not self.target
            or (self.object_id is not None and type(self.object_id) is not str)
            or type(self.state) is not WriteState
        ):
            raise ValueError("invalid_publication_journal_entry")


class PublicationJournal(Protocol):
    """Durable journal independent from the review-token-backed state store.

    ``append_synced`` must append, flush and fsync before returning. Task 6 owns
    the runner.temp transport; this controller only consumes the capability.
    """

    def load(self, run_id: str) -> tuple[PublicationJournalEntry, ...]: ...
    def append_synced(self, entry: PublicationJournalEntry) -> None: ...


class PublicationWriteDisposition(str, Enum):
    DEFINITELY_NOT_SENT = "definitely_not_sent"
    MAY_HAVE_BEEN_ACCEPTED = "may_have_been_accepted"


class PublicationWriteError(RuntimeError):
    """Sanitized transport classification without response bodies or URLs."""

    def __init__(self, code: str, disposition: PublicationWriteDisposition) -> None:
        if (
            type(code) is not str
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code)
            or type(disposition) is not PublicationWriteDisposition
        ):
            raise ValueError("invalid_publication_write_error")
        self.code = code
        self.disposition = disposition
        super().__init__(code)


@dataclass(frozen=True)
class _CompleteTree:
    root_tree_sha: str
    entries: tuple[SourceTreeEntry, ...]
    blobs: tuple[SourceBlob, ...]
    digests: tuple[FileDigest, ...]


@dataclass(frozen=True)
class _PreparedPublication:
    change: ChangeRequest
    manifest: PatchManifest
    repository: str
    branch_name: str
    parent_sha: str
    parent_tree_sha: str
    manifest_paths: tuple[str, ...]
    published_files: tuple[PublishedFile, ...]
    replayed_patch: AppliedPatch
    identity: CommitIdentity
    commit_message: str
    pull_title: str
    pull_body: str
    existing_branch_target: str | None


class _Failure(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _WriteStopped(RuntimeError):
    def __init__(
        self,
        stage: PublicationStage,
        code: str,
        kind: WriteKind,
        state: WriteState,
    ) -> None:
        self.stage = stage
        self.code = code
        self.kind = kind
        self.state = state
        super().__init__(code)


_T = TypeVar("_T")


def compute_verification_argv_digest(command: VerificationCommand) -> str:
    """Bind one trusted literal argv and its fixed environment overrides."""

    if type(command) is not VerificationCommand:
        raise TypeError("verification_command_required")
    if (
        type(command.name) is not str
        or not command.name
        or type(command.argv) is not tuple
        or not command.argv
        or any(type(item) is not str or not item for item in command.argv)
        or type(command.env) is not tuple
        or any(
            type(item) is not tuple
            or len(item) != 2
            or any(type(value) is not str for value in item)
            for item in command.env
        )
    ):
        raise ValueError("invalid_verification_command")
    encoded = json.dumps(
        {"argv": list(command.argv), "env": [list(item) for item in command.env]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(b"qykw-verification-argv-v1\0" + encoded).hexdigest()


def validate_attestation(
    request: PublicationRequest,
    *,
    runtime: TrustedPublicationRuntime,
    replayed_patch: AppliedPatch,
) -> None:
    """Validate every proof field against independent controller inputs."""

    if type(request) is not PublicationRequest or type(replayed_patch) is not AppliedPatch:
        raise ValueError("attestation_output_mismatch")
    if type(runtime) is not TrustedPublicationRuntime:
        raise ValueError("attestation_runtime_mismatch")
    attestation = request.attestation
    change = request.change
    manifest = request.manifest
    if type(attestation) is not VerificationAttestation:
        raise ValueError("attestation_schema_mismatch")
    if type(attestation.schema_version) is not int or attestation.schema_version != 1:
        raise ValueError("attestation_schema_mismatch")
    if (
        type(attestation.workflow_run_id) is not int
        or attestation.workflow_run_id != runtime.workflow_run_id
        or attestation.run_id != change.context.run_id
    ):
        raise ValueError("attestation_run_mismatch")
    bindings = (
        (attestation.source_repository, change.source_repository),
        (attestation.source_head_sha, change.source_head_sha),
        (attestation.target_repository, change.target_repository),
        (attestation.target_base_sha, change.target_base_sha),
        (attestation.target_base_ref, change.target_base_ref),
        (attestation.manifest_digest, manifest.digest),
    )
    if any(type(actual) is not str or actual != expected for actual, expected in bindings):
        raise ValueError("attestation_run_mismatch")
    profile = runtime.profile
    if (
        type(profile) is not VerificationProfile
        or profile.name != change.verification_profile
        or manifest.verification_profile != profile.name
        or attestation.profile != profile.name
    ):
        raise ValueError("attestation_profile_mismatch")
    if attestation.image_digest != runtime.image_digest:
        raise ValueError("attestation_image_mismatch")
    if type(attestation.results) is not tuple or len(attestation.results) != len(profile.commands):
        raise ValueError("attestation_command_mismatch")
    for result, command in zip(attestation.results, profile.commands):
        if type(result) is not CommandResult or type(command) is not VerificationCommand:
            raise ValueError("attestation_command_mismatch")
        if (
            type(result.name) is not str
            or result.name != command.name
            or type(result.argv_digest) is not str
            or result.argv_digest != compute_verification_argv_digest(command)
            or type(result.exit_code) is not int
            or result.exit_code != 0
            or type(result.timed_out) is not bool
            or result.timed_out
            or type(result.duration_ms) is not int
            or result.duration_ms < 0
            or not _is_sha256(result.output_digest)
            or type(result.output_excerpt) is not str
            or len(result.output_excerpt.encode("utf-8")) > _MAX_RESULT_EXCERPT
        ):
            raise ValueError("attestation_command_mismatch")
    if (
        type(attestation.output_files) is not tuple
        or attestation.output_files != replayed_patch.files
        or attestation.output_tree_digest != replayed_patch.output_tree_digest
        or attestation.workspace_tree_digest != replayed_patch.workspace_tree_digest
        or not _is_sha256(attestation.output_tree_digest)
        or not _is_sha256(attestation.workspace_tree_digest)
    ):
        raise ValueError("attestation_output_mismatch")
    if type(attestation.success) is not bool or type(attestation.canceled) is not bool:
        raise ValueError("attestation_not_successful")
    if attestation.success is not True or attestation.canceled is not False:
        raise ValueError("attestation_not_successful")


def publish_verified_change(
    request: PublicationRequest,
    gateway: ChangeGitHubGateway,
    state_store: RunStateStore,
    *,
    runtime: TrustedPublicationRuntime,
    journal: PublicationJournal,
    policy: ChangePolicy,
) -> ChangePublication:
    """Publish exactly once, stopping safely at every non-atomic boundary."""

    branch_name = request.branch_name if type(request) is PublicationRequest else ""
    try:
        _validate_request_shape(request, runtime)
        try:
            existing_records = journal.load(request.change.context.run_id)
        except Exception:
            raise _Failure("journal_read_failed") from None
        records = _validate_journal_records(existing_records, request.change.context.run_id)
        with tempfile.TemporaryDirectory(prefix="qykw-publish-") as temporary:
            prepared = _preflight(
                request,
                gateway,
                state_store,
                runtime,
                policy,
                Path(temporary),
            )
            if records:
                records = _validate_journal_for_prepared(records, prepared)
            if records or prepared.existing_branch_target is not None:
                return _recover_existing(prepared, gateway, records)
            return _publish_prepared(
                prepared, gateway, state_store, journal, policy
            )
    except _Failure as error:
        return _result(
            stage=PublicationStage.PREFLIGHT,
            branch_name=branch_name,
            error_code=error.code,
        )
    except Exception:
        return _result(
            stage=PublicationStage.PREFLIGHT,
            branch_name=branch_name,
            error_code="invalid_publication_request",
        )


def _preflight(
    request: PublicationRequest,
    gateway: ChangeGitHubGateway,
    state_store: RunStateStore,
    runtime: TrustedPublicationRuntime,
    policy: ChangePolicy,
    temporary: Path,
) -> _PreparedPublication:
    change = request.change
    repository = change.target_repository
    if getattr(gateway, "repository", None) != repository:
        raise _Failure("target_repository_mismatch")
    _check_canceled(change, state_store)
    pull = _read_pull(gateway, change.context.pr_number)
    _validate_pull_snapshot(change, pull)
    try:
        policy.validate_request(change, pull)
    except Exception:
        raise _Failure("authorization_revoked") from None
    _check_permission(change, gateway)
    if _read_ref(gateway, repository, change.target_base_ref) != change.target_base_sha:
        raise _Failure("stale_target_base")
    try:
        publishable = gateway.commit_exists(repository, change.source_head_sha)
    except Exception:
        raise _Failure("source_head_not_publishable") from None
    if publishable is not True:
        raise _Failure("source_head_not_publishable")
    try:
        branch_exists = gateway.branch_exists(repository, request.branch_name)
        branch_target = gateway.get_ref_target(repository, request.branch_name)
    except Exception:
        raise _Failure("branch_collision") from None
    if type(branch_exists) is not bool or (branch_target is None) == branch_exists:
        raise _Failure("branch_collision")
    user = _read_user(gateway)
    if (
        user.login != runtime.bot_login
        or user.login != _BOT_LOGIN
        or type(user.database_id) is not int
        or user.database_id <= 0
    ):
        raise _Failure("bot_identity_mismatch")
    try:
        policy.validate_manifest(change, request.manifest)
    except Exception:
        raise _Failure("path_policy_rejected") from None

    try:
        parent = _load_complete_tree(gateway, repository, change.source_head_sha)
    except _Failure:
        raise
    source_root = temporary / "source"
    workspace_root = temporary / "workspace"
    try:
        _write_source_tree(source_root, parent.blobs)
        workspace = materialize_workspace(
            source_root,
            source_head_sha=change.source_head_sha,
            tracked_files=parent.digests,
            destination=workspace_root,
        )
        replayed = apply_patch_manifest(request.manifest, workspace)
        published_files = _read_published_files(workspace.root, replayed.files)
    except Exception:
        raise _Failure("patch_replay_failed") from None
    try:
        validate_attestation(request, runtime=runtime, replayed_patch=replayed)
    except ValueError as error:
        code = str(error)
        if not code.startswith("attestation_"):
            code = "attestation_output_mismatch"
        raise _Failure(code) from None
    identity = CommitIdentity(
        login=_BOT_LOGIN,
        name=_BOT_LOGIN,
        email=f"{user.database_id}+{_BOT_LOGIN}@users.noreply.github.com",
    )
    sealed_change = copy.deepcopy(change)
    sealed_manifest = copy.deepcopy(request.manifest)
    return _PreparedPublication(
        change=sealed_change,
        manifest=sealed_manifest,
        repository=repository,
        branch_name=request.branch_name,
        parent_sha=change.source_head_sha,
        parent_tree_sha=parent.root_tree_sha,
        manifest_paths=tuple(sorted(item.path for item in request.manifest.files)),
        published_files=published_files,
        replayed_patch=replayed,
        identity=identity,
        commit_message=f"qykw: {change.kind.value} {change.context.run_id.lower()}",
        pull_title=f"qykw: {change.kind.value} {change.context.run_id.lower()}",
        pull_body=_build_pull_body(request, runtime),
        existing_branch_target=branch_target,
    )


def _publish_prepared(
    prepared: _PreparedPublication,
    gateway: ChangeGitHubGateway,
    state_store: RunStateStore,
    journal: PublicationJournal,
    policy: ChangePolicy,
) -> ChangePublication:
    receipts: list[WriteReceipt] = []
    blob_entries: list[GitTreeEntry] = []

    def perform(
        *,
        operation_id: str,
        stage: PublicationStage,
        kind: WriteKind,
        target: str,
        write: Callable[[], _T],
        validate: Callable[[_T], tuple[str | None, _T]],
        reconcile: Callable[[], tuple[WriteState, str | None, _T | None]] | None = None,
    ) -> _T:
        unknown = PublicationJournalEntry(
            1,
            prepared.change.context.run_id,
            operation_id,
            stage,
            kind,
            target,
            None,
            WriteState.UNKNOWN,
        )
        try:
            journal.append_synced(unknown)
        except Exception:
            raise _WriteStopped(
                stage, "journal_write_failed", kind, WriteState.NOT_CREATED
            ) from None
        receipt_index = len(receipts)
        receipts.append(WriteReceipt(kind, target, None, WriteState.UNKNOWN))
        try:
            response = write()
            object_id, value = validate(response)
            state = WriteState.CREATED
        except PublicationWriteError as error:
            if error.disposition is PublicationWriteDisposition.DEFINITELY_NOT_SENT:
                state, object_id, value = WriteState.NOT_CREATED, None, None
            elif reconcile is None:
                state, object_id, value = WriteState.UNKNOWN, None, None
            else:
                try:
                    state, object_id, value = reconcile()
                except Exception:
                    state, object_id, value = WriteState.UNKNOWN, None, None
        except Exception:
            state, object_id, value = WriteState.UNKNOWN, None, None
        final = PublicationJournalEntry(
            1,
            prepared.change.context.run_id,
            operation_id,
            stage,
            kind,
            target,
            object_id,
            state,
        )
        try:
            journal.append_synced(final)
        except Exception:
            raise _WriteStopped(
                stage, "journal_write_unknown", kind, WriteState.UNKNOWN
            ) from None
        receipts[receipt_index] = WriteReceipt(kind, target, object_id, state)
        if state is not WriteState.CREATED:
            raise _WriteStopped(
                stage, f"{kind.value}_write_{state.value}", kind, state
            )
        return value  # type: ignore[return-value]

    try:
        for index, published in enumerate(prepared.published_files):
            expected_oid = _git_blob_oid(published.content, 40)
            blob_sha = perform(
                operation_id=f"blob:{index}:{published.sha256}",
                stage=PublicationStage.BLOBS,
                kind=WriteKind.BLOB,
                target=published.path,
                write=lambda item=published: gateway.create_blob(
                    repository=prepared.repository, content=item.content
                ),
                validate=lambda value, expected=expected_oid: (
                    _require_expected_oid(value, expected),
                    value,
                ),
            )
            blob_entries.append(GitTreeEntry(published.path, published.mode, blob_sha))
        tree_sha = perform(
            operation_id="tree:manifest",
            stage=PublicationStage.TREE,
            kind=WriteKind.TREE,
            target=prepared.parent_tree_sha,
            write=lambda: gateway.create_tree(
                repository=prepared.repository,
                base_tree_sha=prepared.parent_tree_sha,
                entries=tuple(blob_entries),
            ),
            validate=lambda value: (_require_oid(value), value),
        )
        commit = perform(
            operation_id="commit:published",
            stage=PublicationStage.COMMIT,
            kind=WriteKind.COMMIT,
            target=tree_sha,
            write=lambda: gateway.create_commit(
                repository=prepared.repository,
                parent_sha=prepared.parent_sha,
                tree_sha=tree_sha,
                message=prepared.commit_message,
                identity=prepared.identity,
            ),
            validate=lambda value: (
                _validate_published_commit(value, tree_sha),
                value,
            ),
        )
        _verify_published_commit(prepared, gateway, commit.commit_sha, tree_sha)
        race_error = _race_guard(
            prepared,
            gateway,
            state_store,
            policy,
            expected_branch_target=None,
        )
        if race_error is not None:
            return _result(
                stage=PublicationStage.REF,
                branch_name=prepared.branch_name,
                commit_sha=commit.commit_sha,
                receipts=tuple(receipts),
                partial=True,
                error_code=race_error,
            )

        def reconcile_ref() -> tuple[WriteState, str | None, None]:
            actual = gateway.get_ref_target(prepared.repository, prepared.branch_name)
            if actual == commit.commit_sha:
                return WriteState.CREATED, commit.commit_sha, None
            if actual is None:
                return WriteState.NOT_CREATED, None, None
            return WriteState.UNKNOWN, None, None

        perform(
            operation_id="ref:branch",
            stage=PublicationStage.REF,
            kind=WriteKind.REF,
            target=prepared.branch_name,
            write=lambda: gateway.create_ref(
                repository=prepared.repository,
                branch_name=prepared.branch_name,
                commit_sha=commit.commit_sha,
            ),
            validate=lambda value: (
                commit.commit_sha if value is None else _raise_invalid_ref_response(),
                value,
            ),
            reconcile=reconcile_ref,
        )
        race_error = _race_guard(
            prepared,
            gateway,
            state_store,
            policy,
            expected_branch_target=commit.commit_sha,
        )
        if race_error is not None:
            return _result(
                stage=PublicationStage.PULL,
                branch_name=prepared.branch_name,
                branch_state=WriteState.CREATED,
                commit_sha=commit.commit_sha,
                receipts=tuple(receipts),
                partial=True,
                error_code="orphan_branch",
            )

        def reconcile_pull() -> tuple[WriteState, str | None, int | None]:
            number = gateway.find_draft_pull_by_run_marker(
                prepared.repository,
                branch_name=prepared.branch_name,
                head_sha=commit.commit_sha,
                base_ref=prepared.change.target_base_ref,
                run_id=prepared.change.context.run_id,
            )
            if number is None:
                return WriteState.NOT_CREATED, None, None
            if type(number) is not int or number <= 0:
                return WriteState.UNKNOWN, None, None
            return WriteState.CREATED, str(number), number

        pull_number = perform(
            operation_id="pull:draft",
            stage=PublicationStage.PULL,
            kind=WriteKind.PULL,
            target=prepared.branch_name,
            write=lambda: gateway.create_draft_pull_request(
                repository=prepared.repository,
                head=prepared.branch_name,
                base=prepared.change.target_base_ref,
                title=prepared.pull_title,
                body=prepared.pull_body,
            ),
            validate=lambda value: (_require_pull_number(value), value),
            reconcile=reconcile_pull,
        )
        if _read_ref(gateway, prepared.repository, prepared.change.target_base_ref) != prepared.change.target_base_sha:
            return _result(
                stage=PublicationStage.PULL,
                branch_name=prepared.branch_name,
                branch_state=WriteState.CREATED,
                pull_state=WriteState.CREATED,
                commit_sha=commit.commit_sha,
                pull_number=pull_number,
                receipts=tuple(receipts),
                partial=True,
                error_code="stale_target_base_after_pull",
            )
        return _result(
            stage=PublicationStage.COMPLETED,
            branch_name=prepared.branch_name,
            branch_state=WriteState.CREATED,
            pull_state=WriteState.CREATED,
            commit_sha=commit.commit_sha,
            pull_number=pull_number,
            receipts=tuple(receipts),
        )
    except _WriteStopped as error:
        branch_state = _latest_state(receipts, WriteKind.REF)
        pull_state = _latest_state(receipts, WriteKind.PULL)
        if error.kind is WriteKind.REF:
            branch_state = error.state
        if error.kind is WriteKind.PULL:
            pull_state = error.state
        commit_sha = _latest_object(receipts, WriteKind.COMMIT)
        return _result(
            stage=error.stage,
            branch_name=prepared.branch_name,
            branch_state=branch_state,
            pull_state=pull_state,
            commit_sha=commit_sha,
            pull_number=_positive_int(_latest_object(receipts, WriteKind.PULL)),
            receipts=tuple(receipts),
            partial=bool(receipts),
            error_code=_stable_write_code(error),
        )
    except _Failure as error:
        return _result(
            stage=PublicationStage.COMMIT,
            branch_name=prepared.branch_name,
            commit_sha=_latest_object(receipts, WriteKind.COMMIT),
            receipts=tuple(receipts),
            partial=bool(receipts),
            error_code=error.code,
        )


def _recover_existing(
    prepared: _PreparedPublication,
    gateway: ChangeGitHubGateway,
    records: tuple[PublicationJournalEntry, ...],
) -> ChangePublication:
    receipts = _receipts_from_journal(records)
    commit_sha = _latest_object(receipts, WriteKind.COMMIT)
    branch_target = prepared.existing_branch_target
    if commit_sha is None and branch_target is not None:
        commit_sha = branch_target
    if branch_target is None:
        return _result(
            stage=PublicationStage.PREFLIGHT,
            branch_name=prepared.branch_name,
            receipts=receipts,
            partial=bool(receipts),
            error_code="publication_recovery_required",
        )
    if commit_sha != branch_target or not _is_oid(branch_target):
        return _result(
            stage=PublicationStage.PREFLIGHT,
            branch_name=prepared.branch_name,
            branch_state=WriteState.CREATED if records else WriteState.NOT_CREATED,
            commit_sha=commit_sha,
            receipts=receipts,
            partial=bool(records),
            error_code="branch_collision",
        )
    try:
        pull_number = gateway.find_draft_pull_by_run_marker(
            prepared.repository,
            branch_name=prepared.branch_name,
            head_sha=branch_target,
            base_ref=prepared.change.target_base_ref,
            run_id=prepared.change.context.run_id,
        )
    except Exception:
        pull_number = None
    if type(pull_number) is not int or pull_number <= 0:
        return _result(
            stage=PublicationStage.PREFLIGHT,
            branch_name=prepared.branch_name,
            branch_state=WriteState.CREATED if records else WriteState.NOT_CREATED,
            commit_sha=branch_target,
            receipts=receipts,
            partial=bool(records),
            error_code="orphan_branch" if records else "branch_collision",
        )
    try:
        _verify_published_commit(prepared, gateway, branch_target, None)
    except _Failure as error:
        return _result(
            stage=PublicationStage.PREFLIGHT,
            branch_name=prepared.branch_name,
            branch_state=WriteState.CREATED,
            pull_state=WriteState.CREATED,
            commit_sha=branch_target,
            pull_number=pull_number,
            receipts=receipts,
            partial=True,
            error_code=error.code,
        )
    return _result(
        stage=PublicationStage.COMPLETED,
        branch_name=prepared.branch_name,
        branch_state=WriteState.CREATED,
        pull_state=WriteState.CREATED,
        commit_sha=branch_target,
        pull_number=pull_number,
        receipts=receipts,
    )


def _race_guard(
    prepared: _PreparedPublication,
    gateway: ChangeGitHubGateway,
    state_store: RunStateStore,
    policy: ChangePolicy,
    *,
    expected_branch_target: str | None,
) -> str | None:
    change = prepared.change
    try:
        if state_store.is_cancel_requested(change.context.pr_number, change.context.run_id):
            return "change_canceled"
        pull = gateway.get_pull_snapshot(change.context.pr_number)
        _validate_pull_snapshot(change, pull)
        if gateway.get_actor_permission(change.context.trigger_actor) not in _ALLOWED_PERMISSIONS:
            return "authorization_revoked"
        policy.validate_request(change, pull)
        policy.validate_manifest(change, prepared.manifest)
        if gateway.get_ref_target(change.target_repository, change.target_base_ref) != change.target_base_sha:
            return "stale_target_base"
        if gateway.get_ref_target(change.target_repository, prepared.branch_name) != expected_branch_target:
            return "branch_raced"
    except _Failure as error:
        return error.code
    except Exception:
        return "authorization_recheck_failed"
    return None


def _verify_published_commit(
    prepared: _PreparedPublication,
    gateway: ChangeGitHubGateway,
    commit_sha: str,
    expected_tree_sha: str | None,
) -> None:
    try:
        published = _load_complete_tree(gateway, prepared.repository, commit_sha)
    except _Failure:
        raise _Failure("published_tree_incomplete") from None
    if expected_tree_sha is not None and published.root_tree_sha != expected_tree_sha:
        raise _Failure("published_tree_incomplete")
    if compute_workspace_tree_digest(published.digests) != prepared.replayed_patch.workspace_tree_digest:
        raise _Failure("workspace_digest_mismatch")
    try:
        paths = gateway.get_changed_paths(
            prepared.repository, prepared.parent_sha, commit_sha
        )
        normalized = _validate_changed_paths(paths)
    except Exception:
        raise _Failure("changed_paths_mismatch") from None
    if normalized != prepared.manifest_paths:
        raise _Failure("changed_paths_mismatch")


def _load_complete_tree(
    gateway: ChangeGitHubGateway, repository: str, commit_sha: str
) -> _CompleteTree:
    try:
        root_tree_sha = gateway.get_commit_tree_sha(repository, commit_sha)
        entries = gateway.list_tree_entries(repository, commit_sha)
    except Exception:
        raise _Failure("parent_tree_incomplete") from None
    if not _is_oid(root_tree_sha) or type(entries) is not tuple or len(entries) > _MAX_TREE_ENTRIES:
        raise _Failure("parent_tree_incomplete")
    seen: dict[str, SourceTreeEntry] = {}
    for entry in entries:
        if type(entry) is not SourceTreeEntry:
            raise _Failure("parent_tree_incomplete")
        path = _normalize_tree_path(entry.path)
        key = unicodedata.normalize("NFC", path).casefold()
        if key in seen or path != entry.path or not _is_oid(entry.git_sha):
            raise _Failure("parent_tree_incomplete")
        if entry.kind == "tree":
            valid = entry.mode == _TREE_MODE
        elif entry.kind == "blob":
            valid = entry.mode in _SUPPORTED_BLOB_MODES
        else:
            valid = False
        if not valid:
            raise _Failure("parent_tree_incomplete")
        seen[key] = entry
    for entry in seen.values():
        parent = PurePosixPath(entry.path).parent
        if parent == PurePosixPath("."):
            continue
        parent_entry = seen.get(unicodedata.normalize("NFC", parent.as_posix()).casefold())
        if parent_entry is None or parent_entry.kind != "tree" or parent_entry.path != parent.as_posix():
            raise _Failure("parent_tree_incomplete")
    blobs: list[SourceBlob] = []
    digests: list[FileDigest] = []
    for entry in sorted(seen.values(), key=lambda item: item.path):
        if entry.kind != "blob":
            continue
        try:
            blob = gateway.get_blob_at_commit(repository, commit_sha, entry.path)
        except Exception:
            raise _Failure("source_blob_mismatch") from None
        if (
            type(blob) is not SourceBlob
            or blob.path != entry.path
            or blob.mode != entry.mode
            or blob.git_sha != entry.git_sha
            or not _blob_matches_oid(blob.content, blob.git_sha)
        ):
            raise _Failure("source_blob_mismatch")
        blobs.append(blob)
        digests.append(
            FileDigest(blob.path, blob.mode, hashlib.sha256(blob.content).hexdigest())
        )
    return _CompleteTree(root_tree_sha, entries, tuple(blobs), tuple(digests))


def _write_source_tree(root: Path, blobs: tuple[SourceBlob, ...]) -> None:
    root.mkdir(mode=0o700)
    for blob in blobs:
        target = root.joinpath(*blob.path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as handle:
            handle.write(blob.content)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            target.chmod(0o755 if blob.mode == "100755" else 0o644)


def _read_published_files(
    root: Path, digests: tuple[FileDigest, ...]
) -> tuple[PublishedFile, ...]:
    result: list[PublishedFile] = []
    for item in digests:
        path = root.joinpath(*item.path.split("/"))
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != item.sha256:
            raise ValueError("published_file_digest_mismatch")
        result.append(PublishedFile(item.path, item.mode, content, item.sha256))
    return tuple(result)


def _validate_request_shape(
    request: PublicationRequest, runtime: TrustedPublicationRuntime
) -> None:
    if (
        type(request) is not PublicationRequest
        or type(request.change) is not ChangeRequest
        or type(request.manifest) is not PatchManifest
        or type(request.attestation) is not VerificationAttestation
        or type(runtime) is not TrustedPublicationRuntime
        or type(request.branch_name) is not str
        or type(request.title) is not str
        or type(request.body) is not str
    ):
        raise _Failure("invalid_publication_request")
    change = request.change
    context = change.context
    if type(context) is not RunContext:
        raise _Failure("invalid_publication_request")
    expected_kind = {
        CommandName.FIX: ChangeKind.FIX,
        CommandName.IMPLEMENT: ChangeKind.IMPLEMENT,
    }.get(context.command.name)
    if (
        type(change.kind) is not ChangeKind
        or change.kind is not expected_kind
        or context.command.mode is not CommandMode.CHANGE
        or change.instruction != context.command.argument
        or not _is_repository(change.target_repository)
        or not _is_repository(change.source_repository)
        or not _is_oid(change.source_head_sha)
        or not _is_oid(change.target_base_sha)
        or type(change.target_base_ref) is not str
        or not change.target_base_ref
        or type(context.pr_number) is not int
        or context.pr_number <= 0
        or type(context.run_id) is not str
        or not context.run_id
        or type(context.trigger_actor) is not str
        or not context.trigger_actor
    ):
        raise _Failure("invalid_publication_request")
    bindings = (
        (change.target_repository, context.repository),
        (change.source_repository, context.source_repository),
        (change.source_head_sha, context.source_head_sha),
        (change.target_base_sha, context.target_base_sha),
        (change.target_base_ref, context.target_base_ref),
    )
    if any(left != right for left, right in bindings):
        raise _Failure("request_binding_mismatch")
    manifest = request.manifest
    manifest_bindings = (
        (manifest.run_id, context.run_id),
        (manifest.source_repository, change.source_repository),
        (manifest.target_repository, change.target_repository),
        (manifest.source_pr_number, context.pr_number),
        (manifest.source_head_sha, change.source_head_sha),
        (manifest.target_base_sha, change.target_base_sha),
        (manifest.target_base_ref, change.target_base_ref),
        (manifest.verification_profile, change.verification_profile),
    )
    if type(manifest.schema_version) is not int or manifest.schema_version != 1:
        raise _Failure("manifest_binding_mismatch")
    if any(type(left) is not type(right) or left != right for left, right in manifest_bindings):
        raise _Failure("manifest_binding_mismatch")
    try:
        digest = compute_manifest_digest(manifest)
    except Exception:
        raise _Failure("manifest_digest_mismatch") from None
    if not _is_sha256(manifest.digest) or manifest.digest != digest:
        raise _Failure("manifest_digest_mismatch")
    expected_branch = _derive_branch_name(context.run_id, change.kind)
    if request.branch_name != expected_branch:
        raise _Failure("invalid_branch_name")


def _derive_branch_name(run_id: str, kind: ChangeKind) -> str:
    lowered = run_id.lower()
    if not _BRANCH_PART_PATTERN.fullmatch(lowered):
        raise _Failure("invalid_branch_name")
    return f"qykw/{lowered}-{kind.value}"


def _validate_pull_snapshot(change: ChangeRequest, pull: PullSnapshot) -> None:
    if type(pull) is not PullSnapshot or pull.state.casefold() != "open":
        raise _Failure("pull_snapshot_mismatch")
    expected = (
        (pull.number, change.context.pr_number),
        (pull.source_repository, change.source_repository),
        (pull.source_head_sha, change.source_head_sha),
        (pull.target_repository, change.target_repository),
        (pull.target_base_sha, change.target_base_sha),
        (pull.target_base_ref, change.target_base_ref),
    )
    if any(type(actual) is not type(trusted) or actual != trusted for actual, trusted in expected):
        raise _Failure("pull_snapshot_mismatch")


def _check_canceled(change: ChangeRequest, state_store: RunStateStore) -> None:
    try:
        canceled = state_store.is_cancel_requested(
            change.context.pr_number, change.context.run_id
        )
    except Exception:
        raise _Failure("cancel_state_unavailable") from None
    if type(canceled) is not bool or canceled:
        raise _Failure("change_canceled")


def _check_permission(change: ChangeRequest, gateway: ChangeGitHubGateway) -> None:
    try:
        permission = gateway.get_actor_permission(change.context.trigger_actor)
    except Exception:
        raise _Failure("authorization_revoked") from None
    if type(permission) is not RepositoryPermission or permission not in _ALLOWED_PERMISSIONS:
        raise _Failure("authorization_revoked")


def _read_pull(gateway: ChangeGitHubGateway, pr_number: int) -> PullSnapshot:
    try:
        return gateway.get_pull_snapshot(pr_number)
    except Exception:
        raise _Failure("pull_snapshot_mismatch") from None


def _read_user(gateway: ChangeGitHubGateway) -> AuthenticatedUser:
    try:
        user = gateway.get_authenticated_user()
    except Exception:
        raise _Failure("bot_identity_mismatch") from None
    if type(user) is not AuthenticatedUser:
        raise _Failure("bot_identity_mismatch")
    return user


def _read_ref(
    gateway: ChangeGitHubGateway, repository: str, branch: str
) -> str | None:
    try:
        value = gateway.get_ref_target(repository, branch)
    except Exception:
        raise _Failure("ref_read_failed") from None
    if value is not None and not _is_oid(value):
        raise _Failure("ref_read_failed")
    return value


def _build_pull_body(
    request: PublicationRequest, runtime: TrustedPublicationRuntime
) -> str:
    change = request.change
    marker = {
        "base_ref": change.target_base_ref,
        "branch": request.branch_name,
        "manifest": request.manifest.digest,
        "run": change.context.run_id,
        "source_head": change.source_head_sha,
        "source_pr": change.context.pr_number,
        "target_repository": change.target_repository,
    }
    encoded = json.dumps(marker, sort_keys=True, separators=(",", ":"))
    encoded = encoded.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    paths = "、".join(_sanitize_public_path(item.path) for item in request.manifest.files)
    checks = "、".join(command.name for command in runtime.profile.commands)
    return (
        "## qykw 授权修改\n\n"
        f"源 PR：#{change.context.pr_number}\n"
        f"运行：{change.context.run_id}\n"
        f"固定 Head：`{change.source_head_sha}`\n"
        f"修改：{len(request.manifest.files)} 个文件（{paths}）\n"
        f"验证 Profile：`{runtime.profile.name}`（{checks}）\n"
        "限制：仅创建独立分支与 Draft PR；未执行合并、批准、删除或覆盖。\n"
        "状态：等待 xyh202131 审查。\n\n"
        f"<!-- qykw-change:v1 {encoded} -->"
    )


def _sanitize_public_path(path: str) -> str:
    translation = str.maketrans(
        {"@": "＠", "<": "‹", ">": "›", "&": "＆", "[": "［", "]": "］", "(": "（", ")": "）", "!": "！", "`": "'"}
    )
    return path.translate(translation)


def _validate_journal_records(
    records: object, run_id: str
) -> tuple[PublicationJournalEntry, ...]:
    if type(records) is not tuple:
        raise _Failure("journal_read_failed")
    for record in records:
        if type(record) is not PublicationJournalEntry or record.run_id != run_id:
            raise _Failure("journal_read_failed")
    return records


def _receipts_from_journal(
    records: tuple[PublicationJournalEntry, ...]
) -> tuple[WriteReceipt, ...]:
    order: list[str] = []
    latest: dict[str, PublicationJournalEntry] = {}
    for record in records:
        if record.operation_id not in latest:
            order.append(record.operation_id)
        latest[record.operation_id] = record
    return tuple(
        WriteReceipt(
            latest[key].kind,
            latest[key].target,
            latest[key].object_id,
            latest[key].state,
        )
        for key in order
    )


def _validate_journal_for_prepared(
    records: tuple[PublicationJournalEntry, ...],
    prepared: _PreparedPublication,
) -> tuple[PublicationJournalEntry, ...]:
    expected = [
        (
            f"blob:{index}:{item.sha256}",
            PublicationStage.BLOBS,
            WriteKind.BLOB,
            item.path,
        )
        for index, item in enumerate(prepared.published_files)
    ]
    expected.extend(
        (
            ("tree:manifest", PublicationStage.TREE, WriteKind.TREE, prepared.parent_tree_sha),
            ("commit:published", PublicationStage.COMMIT, WriteKind.COMMIT, None),
            ("ref:branch", PublicationStage.REF, WriteKind.REF, prepared.branch_name),
            ("pull:draft", PublicationStage.PULL, WriteKind.PULL, prepared.branch_name),
        )
    )
    allowed = {item[0]: (index, *item[1:]) for index, item in enumerate(expected)}
    seen: dict[str, list[PublicationJournalEntry]] = {}
    first_indexes: list[int] = []
    for record in records:
        definition = allowed.get(record.operation_id)
        if definition is None:
            raise _Failure("journal_read_failed")
        index, stage, kind, target = definition
        if (
            record.stage is not stage
            or record.kind is not kind
            or (target is not None and record.target != target)
            or (target is None and not _is_oid(record.target))
        ):
            raise _Failure("journal_read_failed")
        transitions = seen.setdefault(record.operation_id, [])
        if not transitions:
            first_indexes.append(index)
            if record.state is not WriteState.UNKNOWN or record.object_id is not None:
                raise _Failure("journal_read_failed")
        elif len(transitions) != 1:
            raise _Failure("journal_read_failed")
        transitions.append(record)
    if first_indexes != list(range(len(first_indexes))):
        raise _Failure("journal_read_failed")
    for operation_id, transitions in seen.items():
        if len(transitions) == 1:
            continue
        final = transitions[-1]
        if final.state is WriteState.NOT_CREATED and final.object_id is not None:
            raise _Failure("journal_read_failed")
        if final.state is WriteState.CREATED:
            if final.kind is WriteKind.PULL:
                valid_object = _positive_int(final.object_id) is not None
            else:
                valid_object = _is_oid(final.object_id)
            if not valid_object:
                raise _Failure("journal_read_failed")
        if final.kind is WriteKind.BLOB and final.state is WriteState.CREATED:
            index = allowed[operation_id][0]
            expected_oid = _git_blob_oid(prepared.published_files[index].content, 40)
            if final.object_id != expected_oid:
                raise _Failure("journal_read_failed")
    return records


def _validate_changed_paths(value: object) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise ValueError("changed_paths_must_be_tuple")
    result: list[str] = []
    seen: set[str] = set()
    for path in value:
        normalized = _normalize_tree_path(path)
        key = unicodedata.normalize("NFC", normalized).casefold()
        if normalized != path or key in seen:
            raise ValueError("invalid_changed_paths")
        seen.add(key)
        result.append(normalized)
    return tuple(sorted(result))


def _normalize_tree_path(value: object) -> str:
    if type(value) is not str or not value or "\\" in value or "\x00" in value:
        raise _Failure("parent_tree_incomplete")
    if unicodedata.normalize("NFC", value) != value:
        raise _Failure("parent_tree_incomplete")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(part in {"", ".", ".."} for part in path.parts):
        raise _Failure("parent_tree_incomplete")
    if any(part.casefold() == ".git" for part in path.parts):
        raise _Failure("parent_tree_incomplete")
    if len(value.encode("utf-8")) > 1_024:
        raise _Failure("parent_tree_incomplete")
    return value


def _require_expected_oid(value: object, expected: str) -> str:
    oid = _require_oid(value)
    if oid != expected:
        raise ValueError("blob_oid_mismatch")
    return oid


def _require_oid(value: object) -> str:
    if not _is_oid(value):
        raise ValueError("invalid_git_oid")
    return value  # type: ignore[return-value]


def _validate_published_commit(value: object, tree_sha: str) -> str:
    if (
        type(value) is not PublishedCommit
        or not _is_oid(value.commit_sha)
        or value.tree_sha != tree_sha
    ):
        raise ValueError("invalid_published_commit")
    return value.commit_sha


def _require_pull_number(value: object) -> str:
    if type(value) is not int or value <= 0:
        raise ValueError("invalid_pull_number")
    return str(value)


def _raise_invalid_ref_response() -> str:
    raise ValueError("invalid_ref_response")


def _is_oid(value: object) -> bool:
    return (
        type(value) is str
        and len(value) in {40, 64}
        and all(character in _HEX for character in value)
    )


def _is_sha256(value: object) -> bool:
    return type(value) is str and len(value) == 64 and all(character in _HEX for character in value)


def _is_image_digest(value: str) -> bool:
    return value.startswith("sha256:") and _is_sha256(value[7:])


def _is_repository(value: object) -> bool:
    return type(value) is str and _REPOSITORY_PATTERN.fullmatch(value) is not None


def _git_blob_oid(content: bytes, length: int) -> str:
    payload = b"blob " + str(len(content)).encode("ascii") + b"\0" + content
    if length == 40:
        return hashlib.sha1(payload).hexdigest()
    return hashlib.sha256(payload).hexdigest()


def _blob_matches_oid(content: bytes, oid: str) -> bool:
    return _git_blob_oid(content, len(oid)) == oid


def _latest_state(receipts: list[WriteReceipt], kind: WriteKind) -> WriteState:
    for receipt in reversed(receipts):
        if receipt.kind is kind:
            return receipt.state
    return WriteState.NOT_CREATED


def _latest_object(
    receipts: tuple[WriteReceipt, ...] | list[WriteReceipt], kind: WriteKind
) -> str | None:
    for receipt in reversed(receipts):
        if receipt.kind is kind and receipt.object_id is not None:
            return receipt.object_id
    return None


def _positive_int(value: str | None) -> int | None:
    if value is None or not value.isascii() or not value.isdecimal():
        return None
    number = int(value)
    return number if number > 0 else None


def _stable_write_code(error: _WriteStopped) -> str:
    if error.code.startswith("journal_"):
        return error.code
    if error.state is WriteState.UNKNOWN:
        return f"{error.kind.value}_write_unknown"
    return f"{error.kind.value}_write_not_created"


def _result(
    *,
    stage: PublicationStage,
    branch_name: str,
    branch_state: WriteState = WriteState.NOT_CREATED,
    pull_state: WriteState = WriteState.NOT_CREATED,
    commit_sha: str | None = None,
    pull_number: int | None = None,
    receipts: tuple[WriteReceipt, ...] = (),
    partial: bool = False,
    error_code: str | None = None,
) -> ChangePublication:
    return ChangePublication(
        stage,
        branch_name,
        branch_state,
        pull_state,
        commit_sha,
        pull_number,
        receipts,
        partial,
        error_code,
    )
