"""Behavioral tests for the isolated qykw Git Data publication controller."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from typing import Callable
import unittest

from tools.qykw import change_publish as subject
from tools.qykw.change import (
    AppliedPatch,
    ChangeKind,
    ChangeRequest,
    CommandResult,
    FileDigest,
    FilePatch,
    GitTreeEntry,
    PatchManifest,
    PublishedCommit,
    PublicationRequest,
    PublicationStage,
    SourceBlob,
    SourceTreeEntry,
    TextEdit,
    VerificationAttestation,
    WriteKind,
    WriteState,
    compute_manifest_digest,
)
from tools.qykw.domain import (
    AuthenticatedUser,
    CommandMode,
    CommandName,
    CommandRequest,
    CommentKind,
    PullSnapshot,
    RepositoryPermission,
    RunContext,
)
from tools.qykw.verification import VerificationCommand, VerificationProfile
from tools.qykw.change_publish import (
    ChangeGitHubGateway,
    PublicationJournalEntry,
    PublicationWriteDisposition,
    PublicationWriteError,
    TrustedPublicationRuntime,
    compute_verification_argv_digest,
    publish_verified_change,
    validate_attestation,
)


REPOSITORY = "owner/repo"
SOURCE_HEAD = "a" * 40
BASE_SHA = "b" * 40
ROOT_TREE = ""
RUN_ID = "QY-PR7-A1B2"
BRANCH = "qykw/qy-pr7-a1b2-fix"
IMAGE_DIGEST = "sha256:" + "d" * 64


def git_oid(content: bytes) -> str:
    payload = b"blob " + str(len(content)).encode("ascii") + b"\0" + content
    return hashlib.sha1(payload).hexdigest()


def tree_digest(files: tuple[FileDigest, ...]) -> str:
    payload = [
        {"mode": item.mode, "path": item.path, "sha256": item.sha256}
        for item in sorted(files, key=lambda item: item.path)
    ]
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(b"qykw-workspace-tree-v1\0" + encoded).hexdigest()


def source_index_digest(
    root_tree_sha: str,
    entries: tuple[SourceTreeEntry, ...],
    blobs: tuple[SourceBlob, ...],
) -> str:
    payload = {
        "blobs": [
            {
                "git_sha": item.git_sha,
                "mode": item.mode,
                "path": item.path,
                "sha256": hashlib.sha256(item.content).hexdigest(),
            }
            for item in sorted(blobs, key=lambda item: item.path)
        ],
        "entries": [
            {
                "git_sha": item.git_sha,
                "kind": item.kind,
                "mode": item.mode,
                "path": item.path,
            }
            for item in sorted(entries, key=lambda item: item.path)
        ],
        "root_tree_sha": root_tree_sha,
        "schema_version": 1,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(b"qykw-source-tree-index-v1\0" + encoded).hexdigest()


def argv_digest(command: VerificationCommand) -> str:
    encoded = json.dumps(
        {"argv": list(command.argv), "env": [list(item) for item in command.env]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(b"qykw-verification-argv-v1\0" + encoded).hexdigest()


def file_digest(path: str, mode: str, content: bytes) -> FileDigest:
    return FileDigest(path, mode, hashlib.sha256(content).hexdigest())


def context() -> RunContext:
    return RunContext(
        run_id=RUN_ID,
        idempotency_key="comment:99",
        repository_id=123,
        repository=REPOSITORY,
        pr_number=7,
        event_name="issue_comment",
        event_action="created",
        source_repository=REPOSITORY,
        source_head_sha=SOURCE_HEAD,
        target_base_sha=BASE_SHA,
        target_base_ref="main",
        command=CommandRequest(CommandName.FIX, "repair the parser", CommandMode.CHANGE),
        trigger_actor="xyh202131",
        trigger_comment_id=99,
        trigger_comment_kind=CommentKind.ISSUE,
    )


def snapshot() -> PullSnapshot:
    return PullSnapshot(
        number=7,
        state="open",
        draft=False,
        source_repository=REPOSITORY,
        source_head_sha=SOURCE_HEAD,
        target_repository=REPOSITORY,
        target_base_sha=BASE_SHA,
        target_base_ref="main",
        title="source",
        body="source body",
        changed_files=(),
        trusted_rules=(),
        related_files=(),
        checks=(),
    )


PROFILE = VerificationProfile(
    "full",
    (
        VerificationCommand("compile", ("python", "-m", "compileall", "-q", "tools")),
        VerificationCommand("tests", ("python", "-m", "unittest", "discover", "-s", "tests")),
    ),
)


BASE_FILES = {
    "src/app.py": ("100644", b"before\n"),
    "sentinel.bin": ("100644", b"\x00untouched-sentinel"),
    "nested/tool.sh": ("100755", b"#!/bin/sh\necho safe\n"),
}


def git_tree_oid(items: tuple[tuple[str, str, str, str], ...]) -> str:
    ordered = sorted(
        items,
        key=lambda item: item[0].encode("utf-8") + (b"/" if item[1] == "tree" else b""),
    )
    payload = b"".join(
        ("40000" if kind == "tree" else mode).encode("ascii")
        + b" "
        + name.encode("utf-8")
        + b"\0"
        + bytes.fromhex(oid)
        for name, kind, mode, oid in ordered
    )
    header = b"tree " + str(len(payload)).encode("ascii") + b"\0"
    return hashlib.sha1(header + payload).hexdigest()


def source_tree_entries(
    files: dict[str, tuple[str, bytes]],
) -> tuple[str, tuple[SourceTreeEntry, ...]]:
    directories = {
        "/".join(path.split("/")[:index])
        for path in files
        for index in range(1, len(path.split("/")))
    }
    tree_oids: dict[str, str] = {}
    for directory in sorted(
        directories | {""},
        key=lambda item: (item.count("/") + (1 if item else 0), item),
        reverse=True,
    ):
        prefix = f"{directory}/" if directory else ""
        items: list[tuple[str, str, str, str]] = []
        for path, (mode, content) in files.items():
            if path.startswith(prefix) and "/" not in path[len(prefix):]:
                items.append((path[len(prefix):], "blob", mode, git_oid(content)))
        for child, oid in tree_oids.items():
            if child.startswith(prefix) and "/" not in child[len(prefix):]:
                items.append((child[len(prefix):], "tree", "040000", oid))
        tree_oids[directory] = git_tree_oid(tuple(items))
    entries = [
        SourceTreeEntry(path, "040000", "tree", tree_oids[path])
        for path in sorted(directories)
    ]
    entries.extend(
        SourceTreeEntry(path, mode, "blob", git_oid(content))
        for path, (mode, content) in sorted(files.items())
    )
    return tree_oids[""], tuple(entries)


ROOT_TREE, _BASE_TREE_ENTRIES = source_tree_entries(BASE_FILES)
OUTPUT_FILES = {
    **BASE_FILES,
    "src/app.py": ("100644", b"after\n"),
    "src/new.py": ("100644", b"new file\n"),
}


def request() -> PublicationRequest:
    change = ChangeRequest(
        context=context(),
        kind=ChangeKind.FIX,
        instruction="repair the parser",
        source_repository=REPOSITORY,
        target_repository=REPOSITORY,
        source_head_sha=SOURCE_HEAD,
        target_base_sha=BASE_SHA,
        target_base_ref="main",
        verification_profile="full",
    )
    provisional = PatchManifest(
        schema_version=1,
        run_id=RUN_ID,
        source_repository=REPOSITORY,
        target_repository=REPOSITORY,
        source_pr_number=7,
        source_head_sha=SOURCE_HEAD,
        target_base_sha=BASE_SHA,
        target_base_ref="main",
        verification_profile="full",
        files=(
            FilePatch(
                "src/app.py",
                hashlib.sha256(b"before\n").hexdigest(),
                False,
                (TextEdit("before", "after"),),
            ),
            FilePatch("src/new.py", None, True, (TextEdit("", "new file\n"),)),
        ),
        digest="",
    )
    manifest = replace(provisional, digest=compute_manifest_digest(provisional))
    changed = tuple(
        file_digest(path, mode, content)
        for path, (mode, content) in sorted(OUTPUT_FILES.items())
        if path in {"src/app.py", "src/new.py"}
    )
    complete = tuple(
        file_digest(path, mode, content)
        for path, (mode, content) in sorted(OUTPUT_FILES.items())
    )
    results = tuple(
        CommandResult(
            command.name,
            argv_digest(command),
            0,
            False,
            10,
            hashlib.sha256(b"ok").hexdigest(),
            "ok",
        )
        for command in PROFILE.commands
    )
    attestation = VerificationAttestation(
        schema_version=1,
        workflow_run_id=456,
        run_id=RUN_ID,
        source_repository=REPOSITORY,
        source_head_sha=SOURCE_HEAD,
        target_repository=REPOSITORY,
        target_base_sha=BASE_SHA,
        target_base_ref="main",
        manifest_digest=manifest.digest,
        profile="full",
        image_digest=IMAGE_DIGEST,
        output_tree_digest=tree_digest(changed),
        workspace_tree_digest=tree_digest(complete),
        output_files=changed,
        success=True,
        canceled=False,
        results=results,
    )
    return PublicationRequest(
        change,
        manifest,
        attestation,
        BRANCH,
        "@attacker [image](https://evil.invalid)",
        "<img src=x> @everyone Co-Authored-By: attacker",
    )


RUNTIME = TrustedPublicationRuntime(456, IMAGE_DIGEST, PROFILE, "qykw")


class FakeStateStore:
    def __init__(self) -> None:
        self.canceled = False
        self.raise_on_read = False

    def is_cancel_requested(self, pr_number: int, run_id: str) -> bool:
        if self.raise_on_read:
            raise RuntimeError("private remote response")
        if (pr_number, run_id) != (7, RUN_ID):
            raise AssertionError("wrong cancel binding")
        return self.canceled


class FakePolicy:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.reject_request = False
        self.reject_manifest = False

    def validate_request(self, change: ChangeRequest, pull: PullSnapshot) -> None:
        self.calls.append("request")
        if self.reject_request:
            raise ValueError("denied by trusted writer policy")
        if change.context.pr_number != pull.number:
            raise ValueError("bad binding")

    def validate_manifest(self, change: ChangeRequest, manifest: PatchManifest) -> None:
        self.calls.append("manifest")
        if self.reject_manifest:
            raise ValueError("denied by trusted path policy")
        if manifest.digest != compute_manifest_digest(manifest):
            raise ValueError("bad digest")


class FakeJournal:
    def __init__(self, events: list[str]) -> None:
        self.records: list[PublicationJournalEntry] = []
        self.events = events
        self.fail_at: int | None = None

    def load(self, run_id: str) -> tuple[PublicationJournalEntry, ...]:
        return tuple(record for record in self.records if record.run_id == run_id)

    def append_synced(self, entry: PublicationJournalEntry) -> None:
        self.events.append(f"journal:{entry.kind.value}:{entry.state.value}")
        if self.fail_at == len(self.records):
            raise OSError("disk details")
        self.records.append(entry)


class FakeGateway:
    def __init__(self, events: list[str]) -> None:
        self.repository = REPOSITORY
        self.events = events
        self.write_calls: list[tuple[str, object]] = []
        self.read_calls: list[tuple[str, object]] = []
        self.permission = RepositoryPermission.WRITE
        self.pull = snapshot()
        self.user = AuthenticatedUser("qykw", 12345)
        self.base_target = BASE_SHA
        self.branch_target: str | None = None
        self.commit_available = True
        self.failures: dict[str, str] = {}
        self.after_commit: Callable[[], None] | None = None
        self.after_ref: Callable[[], None] | None = None
        self.after_pull: Callable[[], None] | None = None
        self.find_inconclusive = False
        self.published_draft = True
        self.published_author = "qykw"
        self.published_head_repository = REPOSITORY
        self.published_head: str | None = None
        self.published_base = "main"
        self.published_unavailable = False
        self._trees: dict[str, dict[str, tuple[str, bytes]]] = {
            SOURCE_HEAD: dict(BASE_FILES)
        }
        self._commit_trees: dict[str, str] = {SOURCE_HEAD: ROOT_TREE}
        self._tree_objects: dict[str, dict[str, tuple[str, bytes]]] = {}
        self._blobs: dict[str, bytes] = {}
        self._pull_number: int | None = None

    def _read(self, name: str, value: object) -> None:
        self.read_calls.append((name, value))

    def _before_write(self, name: str, payload: object) -> None:
        self.events.append(f"write:{name}")
        self.write_calls.append((name, payload))
        if self.failures.get(name) == "before":
            raise PublicationWriteError(
                f"{name}_transport", PublicationWriteDisposition.DEFINITELY_NOT_SENT
            )
        if self.failures.get(name) == "generic":
            raise RuntimeError("opaque transport failure")

    def _after_write(self, name: str) -> None:
        if self.failures.get(name) == "after":
            raise PublicationWriteError(
                f"{name}_transport", PublicationWriteDisposition.MAY_HAVE_BEEN_ACCEPTED
            )

    def get_pull_snapshot(self, pr_number: int) -> PullSnapshot:
        self._read("pull", pr_number)
        return self.pull

    def get_actor_permission(self, login: str) -> RepositoryPermission:
        self._read("permission", login)
        return self.permission

    def get_authenticated_user(self) -> AuthenticatedUser:
        self._read("user", None)
        return self.user

    def commit_exists(self, repository: str, commit_sha: str) -> bool:
        self._read("commit_exists", (repository, commit_sha))
        return self.commit_available and commit_sha in self._trees

    def get_commit_tree_sha(self, repository: str, commit_sha: str) -> str:
        self._read("tree_sha", (repository, commit_sha))
        if commit_sha not in self._commit_trees:
            raise ValueError("unknown commit")
        return self._commit_trees[commit_sha]

    def list_tree_entries(
        self, repository: str, commit_sha: str
    ) -> tuple[SourceTreeEntry, ...]:
        self._read("tree", (repository, commit_sha))
        files = self._trees[commit_sha]
        return source_tree_entries(files)[1]

    def get_source_tree_index(self, repository: str, commit_sha: str):
        entries = self.list_tree_entries(repository, commit_sha)
        blobs = tuple(
            self.get_blob_at_commit(repository, commit_sha, entry.path)
            for entry in entries
            if entry.kind == "blob"
        )
        return subject.SourceTreeIndex(
            self.get_commit_tree_sha(repository, commit_sha),
            True,
            entries,
            blobs,
            source_index_digest(
                self.get_commit_tree_sha(repository, commit_sha), entries, blobs
            ),
        )

    def get_published_pull_snapshot(self, repository: str, pr_number: int):
        self._read("published_pull", (repository, pr_number))
        if self.published_unavailable:
            raise RuntimeError("snapshot unavailable")
        snapshot = subject.PublishedPullSnapshot(
            pr_number,
            "open",
            self.published_draft,
            self.published_author,
            self.published_head_repository,
            BRANCH,
            self.published_head or self.branch_target,
            self.published_base,
            self.base_target,
        )
        return snapshot

    def get_changed_paths(
        self, repository: str, base_sha: str, head_sha: str
    ) -> tuple[str, ...]:
        self._read("changed", (repository, base_sha, head_sha))
        base = self._trees[base_sha]
        head = self._trees[head_sha]
        return tuple(
            sorted(path for path in set(base) | set(head) if base.get(path) != head.get(path))
        )

    def get_blob_at_commit(
        self, repository: str, commit_sha: str, path: str
    ) -> SourceBlob:
        self._read("blob", (repository, commit_sha, path))
        mode, content = self._trees[commit_sha][path]
        return SourceBlob(path, mode, content, git_oid(content))

    def branch_exists(self, repository: str, branch_name: str) -> bool:
        self._read("branch_exists", (repository, branch_name))
        return self.branch_target is not None

    def get_ref_target(self, repository: str, branch_name: str) -> str | None:
        self._read("ref", (repository, branch_name))
        if branch_name == "main":
            return self.base_target
        return self.branch_target

    def find_draft_pull_by_run_marker(
        self,
        repository: str,
        *,
        branch_name: str,
        base_ref: str,
        run_id: str,
    ):
        self._read("find_pull", (repository, branch_name, base_ref, run_id))
        if self.find_inconclusive:
            raise RuntimeError("read unavailable")
        if (
            self._pull_number is not None
            and branch_name == BRANCH
            and base_ref == "main"
            and run_id == RUN_ID
        ):
            return self.get_published_pull_snapshot(repository, self._pull_number)
        return None

    def create_blob(self, *, repository: str, content: bytes) -> str:
        self._before_write("blob", (repository, content))
        oid = git_oid(content)
        self._blobs[oid] = content
        self._after_write("blob")
        if self.failures.get("blob") == "invalid":
            return "f" * 40
        return oid

    def create_tree(
        self,
        *,
        repository: str,
        base_tree_sha: str,
        entries: tuple[GitTreeEntry, ...],
    ) -> str:
        self._before_write("tree", (repository, base_tree_sha, entries))
        files = dict(self._trees[SOURCE_HEAD])
        for entry in entries:
            files[entry.path] = (entry.mode, self._blobs[entry.blob_sha])
        oid = source_tree_entries(files)[0]
        self._tree_objects[oid] = files
        self._after_write("tree")
        if self.failures.get("tree") == "invalid":
            return "not-an-oid"
        return oid

    def create_commit(
        self,
        *,
        repository: str,
        parent_sha: str,
        tree_sha: str,
        message: str,
        identity: object,
    ) -> PublishedCommit:
        self._before_write(
            "commit", (repository, parent_sha, tree_sha, message, identity)
        )
        oid = hashlib.sha1((parent_sha + tree_sha + message).encode()).hexdigest()
        self._trees[oid] = dict(self._tree_objects[tree_sha])
        self._commit_trees[oid] = tree_sha
        if self.after_commit is not None:
            self.after_commit()
        self._after_write("commit")
        if self.failures.get("commit") == "invalid":
            return PublishedCommit("bad", tree_sha)
        return PublishedCommit(oid, tree_sha)

    def create_ref(self, *, repository: str, branch_name: str, commit_sha: str) -> None:
        self._before_write("ref", (repository, branch_name, commit_sha))
        self.branch_target = commit_sha
        if self.after_ref is not None:
            self.after_ref()
        self._after_write("ref")
        if self.failures.get("ref") == "invalid":
            return "unexpected"  # type: ignore[return-value]

    def create_draft_pull_request(
        self,
        *,
        repository: str,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> int:
        self._before_write("pull", (repository, head, base, title, body))
        self._pull_number = 77
        if self.after_pull is not None:
            self.after_pull()
        self._after_write("pull")
        if self.failures.get("pull") == "invalid":
            return 0
        return 77


class PublicationFixture:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.gateway = FakeGateway(self.events)
        self.state = FakeStateStore()
        self.policy = FakePolicy()
        self.journal = FakeJournal(self.events)
        self.request = request()

    def publish(self, runtime: TrustedPublicationRuntime = RUNTIME):
        return publish_verified_change(
            self.request,
            self.gateway,
            self.state,
            runtime=runtime,
            journal=self.journal,
            policy=self.policy,
        )

    def journal_entry(
        self,
        operation_id: str,
        stage: PublicationStage,
        kind: WriteKind,
        target: str,
        object_id: str | None,
        state: WriteState,
    ) -> PublicationJournalEntry:
        return PublicationJournalEntry(
            1,
            RUN_ID,
            operation_id,
            stage,
            kind,
            target,
            object_id,
            state,
            repository=REPOSITORY,
            source_head_sha=SOURCE_HEAD,
            target_base_sha=BASE_SHA,
            manifest_digest=self.request.manifest.digest,
            workflow_run_id=RUNTIME.workflow_run_id,
        )


class TestAttestationBoundary(unittest.TestCase):
    def test_canonical_argv_digest_is_stable_and_binds_environment(self) -> None:
        command = VerificationCommand("x", ("python", "-V"), (("LANG", "C"),))
        self.assertEqual(compute_verification_argv_digest(command), argv_digest(command))
        self.assertNotEqual(
            compute_verification_argv_digest(command),
            compute_verification_argv_digest(replace(command, env=(("LANG", "zh_CN"),))),
        )

    def test_invalid_runtime_journal_transport_and_command_contracts_fail_closed(self) -> None:
        invalid_runtimes = (
            (0, IMAGE_DIGEST, PROFILE, "qykw"),
            (456, "d" * 64, PROFILE, "qykw"),
            (456, IMAGE_DIGEST, object(), "qykw"),
            (456, IMAGE_DIGEST, PROFILE, "attacker"),
        )
        for values in invalid_runtimes:
            with self.subTest(runtime=values), self.assertRaises(ValueError):
                TrustedPublicationRuntime(*values)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            PublicationJournalEntry(
                2, "", "", PublicationStage.PREFLIGHT, WriteKind.BLOB, "", None,
                WriteState.UNKNOWN, "", "", "", "", 0,
            )
        for code, disposition in (("BAD", PublicationWriteDisposition.DEFINITELY_NOT_SENT), ("ok", "bad")):
            with self.subTest(code=code), self.assertRaises(ValueError):
                PublicationWriteError(code, disposition)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            compute_verification_argv_digest(object())  # type: ignore[arg-type]
        for command in (
            VerificationCommand("", ("python",)),
            VerificationCommand("x", ()),
            VerificationCommand("x", ("",)),
            VerificationCommand("x", ("python",), (("LANG", 1),)),  # type: ignore[arg-type]
        ):
            with self.subTest(command=command), self.assertRaises(ValueError):
                compute_verification_argv_digest(command)

    def test_direct_attestation_type_boundaries_reject_tampering(self) -> None:
        fixture = PublicationFixture()
        replayed = AppliedPatch(
            fixture.request.attestation.output_files,
            fixture.request.attestation.output_tree_digest,
            fixture.request.attestation.workspace_tree_digest,
        )
        for bad_request, bad_runtime, bad_patch, code in (
            (object(), RUNTIME, replayed, "attestation_output_mismatch"),
            (fixture.request, object(), replayed, "attestation_runtime_mismatch"),
            (fixture.request, RUNTIME, object(), "attestation_output_mismatch"),
        ):
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, code):
                validate_attestation(  # type: ignore[arg-type]
                    bad_request, runtime=bad_runtime, replayed_patch=bad_patch
                )
        tampered = replace(fixture.request, attestation=fixture.request.attestation)
        object.__setattr__(tampered, "attestation", object())
        with self.assertRaisesRegex(ValueError, "attestation_schema_mismatch"):
            validate_attestation(tampered, runtime=RUNTIME, replayed_patch=replayed)
        bad_result = replace(fixture.request.attestation, results=fixture.request.attestation.results)
        object.__setattr__(bad_result, "results", (object(),) + bad_result.results[1:])
        with self.assertRaisesRegex(ValueError, "attestation_command_mismatch"):
            validate_attestation(
                replace(fixture.request, attestation=bad_result),
                runtime=RUNTIME,
                replayed_patch=replayed,
            )
        bad_flags = replace(fixture.request.attestation)
        object.__setattr__(bad_flags, "success", 1)
        with self.assertRaisesRegex(ValueError, "attestation_not_successful"):
            validate_attestation(
                replace(fixture.request, attestation=bad_flags),
                runtime=RUNTIME,
                replayed_patch=replayed,
            )

    def test_attestation_cannot_self_assert_controller_runtime(self) -> None:
        fixture = PublicationFixture()
        self.assertIsNone(
            validate_attestation(
                fixture.request,
                runtime=RUNTIME,
                replayed_patch=AppliedPatch(
                    fixture.request.attestation.output_files,
                    fixture.request.attestation.output_tree_digest,
                    fixture.request.attestation.workspace_tree_digest,
                ),
            )
        )
        for runtime in (
            replace(RUNTIME, workflow_run_id=999),
            replace(RUNTIME, image_digest="sha256:" + "e" * 64),
            replace(RUNTIME, profile=replace(PROFILE, name="backend")),
        ):
            with self.subTest(runtime=runtime):
                current = PublicationFixture()
                result = current.publish(runtime)
                self.assertEqual(result.stage, PublicationStage.PREFLIGHT)
                self.assertEqual(current.gateway.write_calls, [])


class TestPublicationPreflight(unittest.TestCase):
    def test_preflight_revalidates_trusted_policy_and_repository_binding(self) -> None:
        fixture = PublicationFixture()
        fixture.gateway.repository = "other/repo"
        result = fixture.publish()
        self.assertEqual(result.error_code, "target_repository_mismatch")
        self.assertEqual(fixture.policy.calls, [])
        self.assertEqual(fixture.gateway.write_calls, [])

        fixture = PublicationFixture()
        fixture.policy.reject_manifest = True
        result = fixture.publish()
        self.assertEqual(result.error_code, "path_policy_rejected")
        self.assertEqual(fixture.policy.calls, ["request", "manifest"])
        self.assertEqual(fixture.gateway.write_calls, [])

    def test_every_static_rejection_precedes_first_blob(self) -> None:
        cases: dict[str, Callable[[PublicationFixture], None]] = {
            "manifest_digest": lambda f: setattr(
                f, "request", replace(f.request, manifest=replace(f.request.manifest, digest="0" * 64))
            ),
            "canceled": lambda f: setattr(f.state, "canceled", True),
            "cancel_read": lambda f: setattr(f.state, "raise_on_read", True),
            "head_drift": lambda f: setattr(
                f.gateway, "pull", replace(f.gateway.pull, source_head_sha="f" * 40)
            ),
            "base_drift": lambda f: setattr(f.gateway, "base_target", "f" * 40),
            "permission": lambda f: setattr(f.gateway, "permission", RepositoryPermission.READ),
            "source_missing": lambda f: setattr(f.gateway, "commit_available", False),
            "bad_branch": lambda f: setattr(
                f, "request", replace(f.request, branch_name="main")
            ),
            "branch_exists": lambda f: setattr(f.gateway, "branch_target", "f" * 40),
            "bot_identity": lambda f: setattr(
                f.gateway, "user", AuthenticatedUser("attacker", 12345)
            ),
            "policy_request": lambda f: setattr(f.policy, "reject_request", True),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                fixture = PublicationFixture()
                mutate(fixture)
                result = fixture.publish()
                self.assertEqual(result.stage, PublicationStage.PREFLIGHT)
                self.assertFalse(result.partial)
                self.assertEqual(result.receipts, ())
                self.assertEqual(result.branch_state, WriteState.NOT_CREATED)
                self.assertEqual(result.pull_state, WriteState.NOT_CREATED)
                self.assertEqual(fixture.gateway.write_calls, [])

    def test_each_attestation_field_is_fail_closed(self) -> None:
        original = request().attestation
        mutations = {
            "schema": replace(original, schema_version=2),
            "workflow": replace(original, workflow_run_id=999),
            "run": replace(original, run_id="other"),
            "source_repo": replace(original, source_repository="other/repo"),
            "source_head": replace(original, source_head_sha="f" * 40),
            "target_repo": replace(original, target_repository="other/repo"),
            "base_sha": replace(original, target_base_sha="f" * 40),
            "base_ref": replace(original, target_base_ref="dev"),
            "manifest": replace(original, manifest_digest="f" * 64),
            "profile": replace(original, profile="backend"),
            "image": replace(original, image_digest="sha256:" + "e" * 64),
            "output_tree": replace(original, output_tree_digest="f" * 64),
            "workspace_tree": replace(original, workspace_tree_digest="f" * 64),
            "outputs": replace(original, output_files=original.output_files[:-1]),
            "success": replace(original, success=False),
            "canceled": replace(original, canceled=True),
            "result_count": replace(original, results=original.results[:-1]),
            "result_name": replace(
                original, results=(replace(original.results[0], name="wrong"),) + original.results[1:]
            ),
            "argv": replace(
                original, results=(replace(original.results[0], argv_digest="f" * 64),) + original.results[1:]
            ),
            "exit": replace(
                original, results=(replace(original.results[0], exit_code=1),) + original.results[1:]
            ),
            "timeout": replace(
                original, results=(replace(original.results[0], timed_out=True),) + original.results[1:]
            ),
        }
        for name, attestation in mutations.items():
            with self.subTest(name=name):
                fixture = PublicationFixture()
                fixture.request = replace(fixture.request, attestation=attestation)
                result = fixture.publish()
                self.assertEqual(result.stage, PublicationStage.PREFLIGHT)
                self.assertEqual(fixture.gateway.write_calls, [])
                self.assertIsNotNone(result.error_code)

    def test_gateway_read_and_complete_tree_failures_are_zero_write(self) -> None:
        def raise_read(*args: object, **kwargs: object):
            raise RuntimeError("opaque read failure")

        cases: dict[str, Callable[[PublicationFixture], None]] = {
            "journal": lambda f: setattr(f.journal, "load", raise_read),
            "pull": lambda f: setattr(f.gateway, "get_pull_snapshot", raise_read),
            "permission": lambda f: setattr(f.gateway, "get_actor_permission", raise_read),
            "base_ref": lambda f: setattr(f.gateway, "get_ref_target", raise_read),
            "commit": lambda f: setattr(f.gateway, "commit_exists", raise_read),
            "branch": lambda f: setattr(f.gateway, "branch_exists", raise_read),
            "user": lambda f: setattr(f.gateway, "get_authenticated_user", raise_read),
            "tree_sha": lambda f: setattr(f.gateway, "get_commit_tree_sha", raise_read),
            "tree_entries_type": lambda f: setattr(f.gateway, "list_tree_entries", lambda *a: []),
            "tree_entry_type": lambda f: setattr(f.gateway, "list_tree_entries", lambda *a: (object(),)),
            "tree_unknown_kind": lambda f: setattr(
                f.gateway,
                "list_tree_entries",
                lambda *a: (SourceTreeEntry("src", "040000", "tree", "e" * 40), SourceTreeEntry("src/app.py", "100644", "commit", "e" * 40)),
            ),
            "tree_missing_parent": lambda f: setattr(
                f.gateway,
                "list_tree_entries",
                lambda *a: (SourceTreeEntry("src/app.py", "100644", "blob", git_oid(b"before\n")),),
            ),
            "blob_read": lambda f: setattr(f.gateway, "get_blob_at_commit", raise_read),
            "blob_mismatch": lambda f: setattr(
                f.gateway,
                "get_blob_at_commit",
                lambda repository, commit, path: SourceBlob(path, "100644", b"tampered", "f" * 40),
            ),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                fixture = PublicationFixture()
                mutate(fixture)
                result = fixture.publish()
                self.assertEqual(result.stage, PublicationStage.PREFLIGHT)
                self.assertEqual(fixture.gateway.write_calls, [])
                self.assertFalse(result.partial)

    def test_strict_request_manifest_branch_and_snapshot_bindings_fail_closed(self) -> None:
        cases: dict[str, Callable[[PublicationFixture], None]] = {
            "request_type": lambda f: setattr(f, "request", object()),
            "request_context": lambda f: object.__setattr__(f.request.change, "context", object()),
            "request_binding": lambda f: object.__setattr__(f.request.change.context, "repository", "other/repo"),
            "manifest_schema": lambda f: object.__setattr__(f.request.manifest, "schema_version", 2),
            "manifest_binding": lambda f: object.__setattr__(f.request.manifest, "run_id", "other"),
            "invalid_run_branch": lambda f: (
                object.__setattr__(f.request.change.context, "run_id", "bad/run"),
                object.__setattr__(f.request.manifest, "run_id", "bad/run"),
            ),
            "closed_pull": lambda f: setattr(f.gateway, "pull", replace(f.gateway.pull, state="closed")),
            "invalid_ref_result": lambda f: setattr(f.gateway, "base_target", "bad"),
            "invalid_user_type": lambda f: setattr(f.gateway, "user", object()),
            "invalid_cancel_type": lambda f: setattr(f.state, "canceled", 1),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                fixture = PublicationFixture()
                mutate(fixture)
                result = fixture.publish()
                self.assertEqual(result.stage, PublicationStage.PREFLIGHT)
                self.assertEqual(fixture.gateway.write_calls, [])


class TestPublicationWrites(unittest.TestCase):
    def test_success_inherits_parent_tree_and_uses_fixed_safe_outputs(self) -> None:
        fixture = PublicationFixture()
        result = fixture.publish()
        self.assertEqual(result.stage, PublicationStage.COMPLETED)
        self.assertFalse(result.partial)
        self.assertEqual(result.branch_state, WriteState.CREATED)
        self.assertEqual(result.pull_state, WriteState.CREATED)
        self.assertEqual(result.pull_number, 77)
        self.assertEqual([name for name, _ in fixture.gateway.write_calls], ["blob", "blob", "tree", "commit", "ref", "pull"])
        self.assertEqual(
            fixture.events,
            [
                "journal:blob:unknown", "write:blob", "journal:blob:created",
                "journal:blob:unknown", "write:blob", "journal:blob:created",
                "journal:tree:unknown", "write:tree", "journal:tree:created",
                "journal:commit:unknown", "write:commit", "journal:commit:created",
                "journal:ref:unknown", "write:ref", "journal:ref:created",
                "journal:pull:unknown", "write:pull", "journal:pull:created",
            ],
        )
        tree_payload = fixture.gateway.write_calls[2][1]
        self.assertEqual(tree_payload[1], ROOT_TREE)
        self.assertEqual({entry.path for entry in tree_payload[2]}, {"src/app.py", "src/new.py"})
        self.assertEqual(fixture.gateway._trees[result.commit_sha]["sentinel.bin"], BASE_FILES["sentinel.bin"])
        commit_payload = fixture.gateway.write_calls[3][1]
        identity = commit_payload[4]
        self.assertEqual(commit_payload[1], SOURCE_HEAD)
        self.assertEqual(identity.name, "qykw")
        self.assertEqual(identity.email, "12345+qykw@users.noreply.github.com")
        self.assertNotIn("Co-Authored-By", commit_payload[3])
        pull_payload = fixture.gateway.write_calls[-1][1]
        self.assertEqual(pull_payload[:3], (REPOSITORY, BRANCH, "main"))
        public_text = pull_payload[3] + pull_payload[4]
        for forbidden in ("attacker", "evil.invalid", "<img", "@everyone", "Co-Authored-By"):
            self.assertNotIn(forbidden, public_text)
        self.assertIn("等待 xyh202131 审查", pull_payload[4])

    def test_write_region_consumes_only_preflight_sealed_values(self) -> None:
        fixture = PublicationFixture()

        def mutate_original_request() -> None:
            object.__setattr__(fixture.request.change, "target_base_ref", "attacker-base")
            object.__setattr__(fixture.request.change.context, "run_id", "ATTACKER-RUN")
            object.__setattr__(fixture.request.manifest, "target_base_ref", "attacker-base")

        fixture.gateway.after_commit = mutate_original_request
        result = fixture.publish()
        self.assertEqual(result.stage, PublicationStage.COMPLETED)
        pull_payload = fixture.gateway.write_calls[-1][1]
        self.assertEqual(pull_payload[2], "main")
        find_calls = [value for name, value in fixture.gateway.read_calls if name == "find_pull"]
        self.assertTrue(all(value[-1] == RUN_ID for value in find_calls))

    def test_commit_to_ref_races_leave_unreachable_objects(self) -> None:
        cases: dict[str, Callable[[PublicationFixture], None]] = {
            "cancel": lambda f: setattr(f.state, "canceled", True),
            "permission": lambda f: setattr(f.gateway, "permission", RepositoryPermission.READ),
            "head": lambda f: setattr(f.gateway, "pull", replace(f.gateway.pull, source_head_sha="f" * 40)),
            "base": lambda f: setattr(f.gateway, "base_target", "f" * 40),
            "branch": lambda f: setattr(f.gateway, "branch_target", "f" * 40),
        }
        for name, mutation in cases.items():
            with self.subTest(name=name):
                fixture = PublicationFixture()
                fixture.gateway.after_commit = lambda f=fixture, change=mutation: change(f)
                result = fixture.publish()
                self.assertEqual(result.stage, PublicationStage.REF)
                self.assertTrue(result.partial)
                self.assertIsNotNone(result.commit_sha)
                self.assertEqual(result.branch_state, WriteState.NOT_CREATED)
                self.assertFalse(any(call[0] in {"ref", "pull"} for call in fixture.gateway.write_calls))

    def test_ref_to_pull_race_reports_orphan_without_cleanup(self) -> None:
        fixture = PublicationFixture()
        fixture.gateway.after_ref = lambda: setattr(fixture.state, "canceled", True)
        result = fixture.publish()
        self.assertEqual(result.stage, PublicationStage.PULL)
        self.assertEqual(result.error_code, "change_canceled")
        self.assertEqual(result.branch_state, WriteState.CREATED)
        self.assertEqual(result.pull_state, WriteState.NOT_CREATED)
        self.assertFalse(any(call[0] == "pull" for call in fixture.gateway.write_calls))
        for forbidden in ("delete", "update_ref", "merge", "approve", "close"):
            self.assertFalse(hasattr(fixture.gateway, forbidden))

    def test_write_failures_are_journaled_once_and_never_replayed(self) -> None:
        for stage in ("blob", "tree", "commit", "ref", "pull"):
            with self.subTest(stage=stage, disposition="before"):
                fixture = PublicationFixture()
                fixture.gateway.failures[stage] = "before"
                result = fixture.publish()
                self.assertEqual(result.partial, stage != "blob")
                self.assertEqual(sum(name == stage for name, _ in fixture.gateway.write_calls), 1)
                receipt = next(item for item in result.receipts if item.kind.value == stage)
                self.assertEqual(receipt.state, WriteState.NOT_CREATED)

        for stage in ("blob", "tree", "commit"):
            with self.subTest(stage=stage, disposition="after"):
                fixture = PublicationFixture()
                fixture.gateway.failures[stage] = "after"
                result = fixture.publish()
                self.assertTrue(result.partial)
                self.assertEqual(sum(name == stage for name, _ in fixture.gateway.write_calls), 1)
                receipt = next(item for item in result.receipts if item.kind.value == stage)
                self.assertEqual(receipt.state, WriteState.UNKNOWN)

    def test_ref_and_pull_disconnects_use_only_read_reconciliation(self) -> None:
        fixture = PublicationFixture()
        fixture.gateway.failures["ref"] = "after"
        result = fixture.publish()
        self.assertEqual(result.stage, PublicationStage.COMPLETED)
        self.assertEqual(sum(name == "ref" for name, _ in fixture.gateway.write_calls), 1)
        self.assertEqual(sum(name == "pull" for name, _ in fixture.gateway.write_calls), 1)

        fixture = PublicationFixture()
        fixture.gateway.failures["pull"] = "after"
        result = fixture.publish()
        self.assertEqual(result.stage, PublicationStage.COMPLETED)
        self.assertEqual(result.pull_number, 77)
        self.assertEqual(sum(name == "pull" for name, _ in fixture.gateway.write_calls), 1)

        fixture = PublicationFixture()
        fixture.gateway.failures["pull"] = "after"
        original_find = fixture.gateway.find_draft_pull_by_run_marker

        def fail_after_pull(*args: object, **kwargs: object):
            if fixture.gateway._pull_number is not None:
                raise RuntimeError("read unavailable")
            return original_find(*args, **kwargs)

        fixture.gateway.find_draft_pull_by_run_marker = fail_after_pull
        result = fixture.publish()
        self.assertEqual(result.pull_state, WriteState.UNKNOWN)
        self.assertEqual(result.error_code, "pull_write_unknown")

    def test_uncertain_ref_and_pull_absence_or_collision_are_not_retried(self) -> None:
        for observed_target, expected in ((None, WriteState.NOT_CREATED), ("f" * 40, WriteState.UNKNOWN)):
            with self.subTest(ref_target=observed_target):
                fixture = PublicationFixture()

                def uncertain_ref(**kwargs: object) -> None:
                    fixture.gateway._before_write("ref", tuple(kwargs.values()))
                    fixture.gateway.branch_target = observed_target
                    raise PublicationWriteError(
                        "ref_transport", PublicationWriteDisposition.MAY_HAVE_BEEN_ACCEPTED
                    )

                fixture.gateway.create_ref = uncertain_ref
                result = fixture.publish()
                self.assertEqual(result.branch_state, expected)
                self.assertEqual(sum(name == "ref" for name, _ in fixture.gateway.write_calls), 1)

        for found, expected in ((None, WriteState.NOT_CREATED), (0, WriteState.UNKNOWN)):
            with self.subTest(pull_lookup=found):
                fixture = PublicationFixture()

                def uncertain_pull(**kwargs: object) -> int:
                    fixture.gateway._before_write("pull", tuple(kwargs.values()))
                    raise PublicationWriteError(
                        "pull_transport", PublicationWriteDisposition.MAY_HAVE_BEEN_ACCEPTED
                    )

                fixture.gateway.create_draft_pull_request = uncertain_pull
                lookup_count = 0

                def find_after_write(*args: object, **kwargs: object):
                    nonlocal lookup_count
                    lookup_count += 1
                    return None if lookup_count == 1 else found

                fixture.gateway.find_draft_pull_by_run_marker = find_after_write
                result = fixture.publish()
                self.assertEqual(result.pull_state, expected)
                self.assertEqual(sum(name == "pull" for name, _ in fixture.gateway.write_calls), 1)

    def test_invalid_or_generic_write_responses_stop_as_unknown(self) -> None:
        for stage in ("blob", "tree", "commit", "ref", "pull"):
            with self.subTest(stage=stage, failure="invalid"):
                fixture = PublicationFixture()
                fixture.gateway.failures[stage] = "invalid"
                result = fixture.publish()
                self.assertTrue(result.partial)
                self.assertEqual(sum(name == stage for name, _ in fixture.gateway.write_calls), 1)
                receipt = next(item for item in result.receipts if item.kind.value == stage)
                self.assertEqual(receipt.state, WriteState.UNKNOWN)
            with self.subTest(stage=stage, failure="generic"):
                fixture = PublicationFixture()
                fixture.gateway.failures[stage] = "generic"
                result = fixture.publish()
                self.assertTrue(result.partial)
                self.assertEqual(sum(name == stage for name, _ in fixture.gateway.write_calls), 1)

    def test_final_journal_transition_failure_preserves_unknown_receipt(self) -> None:
        fixture = PublicationFixture()
        fixture.journal.fail_at = 1
        result = fixture.publish()
        self.assertEqual(result.error_code, "journal_write_unknown")
        self.assertEqual(len(fixture.gateway.write_calls), 1)
        self.assertEqual(result.receipts[0].state, WriteState.UNKNOWN)

    def test_published_tree_and_diff_revalidation_stop_before_ref(self) -> None:
        def tamper_tree(fixture: PublicationFixture) -> None:
            commit_sha = next(reversed(fixture.gateway._trees))
            fixture.gateway._trees[commit_sha]["sentinel.bin"] = ("100644", b"tampered")

        fixture = PublicationFixture()
        fixture.gateway.after_commit = lambda: tamper_tree(fixture)
        result = fixture.publish()
        self.assertEqual(result.error_code, "published_tree_incomplete")
        self.assertFalse(any(name == "ref" for name, _ in fixture.gateway.write_calls))

        fixture = PublicationFixture()
        fixture.gateway.get_changed_paths = lambda *args: ("src/app.py",)
        result = fixture.publish()
        self.assertEqual(result.error_code, "changed_paths_mismatch")

        fixture = PublicationFixture()
        fixture.gateway.get_changed_paths = lambda *args: ("src/app.py", "SRC/APP.PY")
        result = fixture.publish()
        self.assertEqual(result.error_code, "changed_paths_mismatch")

    def test_published_tree_unavailable_or_wrong_root_stops_before_ref(self) -> None:
        fixture = PublicationFixture()
        fixture.gateway.after_commit = lambda: setattr(
            fixture.gateway, "list_tree_entries", lambda *args: (_ for _ in ()).throw(RuntimeError("gone"))
        )
        result = fixture.publish()
        self.assertEqual(result.error_code, "published_tree_incomplete")

        fixture = PublicationFixture()

        def wrong_root() -> None:
            commit_sha = next(reversed(fixture.gateway._commit_trees))
            fixture.gateway._commit_trees[commit_sha] = "f" * 40

        fixture.gateway.after_commit = wrong_root
        result = fixture.publish()
        self.assertEqual(result.error_code, "published_tree_incomplete")

    def test_patch_replay_failure_and_inconsistent_branch_reads_are_preflight_failures(self) -> None:
        fixture = PublicationFixture()
        bad_patch = replace(fixture.request.manifest.files[0], base_sha256="f" * 64)
        provisional = replace(
            fixture.request.manifest,
            files=(bad_patch,) + fixture.request.manifest.files[1:],
            digest="",
        )
        fixture.request = replace(
            fixture.request,
            manifest=replace(provisional, digest=compute_manifest_digest(provisional)),
        )
        result = fixture.publish()
        self.assertEqual(result.error_code, "patch_replay_failed")
        self.assertEqual(fixture.gateway.write_calls, [])

        fixture = PublicationFixture()
        fixture.gateway.branch_exists = lambda *args: False
        fixture.gateway.branch_target = "f" * 40
        result = fixture.publish()
        self.assertEqual(result.error_code, "branch_collision")
        self.assertEqual(fixture.gateway.write_calls, [])
        self.assertFalse(any(name == "ref" for name, _ in fixture.gateway.write_calls))

        fixture = PublicationFixture()
        fixture.gateway.get_changed_paths = lambda *args: ["src/app.py", "src/new.py"]
        result = fixture.publish()
        self.assertEqual(result.error_code, "changed_paths_mismatch")

    def test_post_pull_base_drift_is_reported_without_cleanup(self) -> None:
        fixture = PublicationFixture()
        fixture.gateway.after_pull = lambda: setattr(fixture.gateway, "base_target", "f" * 40)
        result = fixture.publish()
        self.assertEqual(result.stage, PublicationStage.PULL)
        self.assertEqual(result.error_code, "pull_write_unknown")
        self.assertEqual(result.pull_state, WriteState.UNKNOWN)
        self.assertTrue(result.partial)

    def test_journal_failure_happens_before_external_write(self) -> None:
        fixture = PublicationFixture()
        fixture.journal.fail_at = 0
        result = fixture.publish()
        self.assertEqual(result.error_code, "journal_write_failed")
        self.assertEqual(fixture.gateway.write_calls, [])

    def test_rerun_with_exact_receipts_is_read_only(self) -> None:
        fixture = PublicationFixture()
        first = fixture.publish()
        self.assertEqual(first.stage, PublicationStage.COMPLETED)
        fixture.gateway.write_calls.clear()
        fixture.events.clear()
        second = fixture.publish()
        self.assertEqual(second.stage, PublicationStage.COMPLETED)
        self.assertEqual(second.pull_number, first.pull_number)
        self.assertEqual(fixture.gateway.write_calls, [])
        self.assertEqual(fixture.events, [])

    def test_incomplete_journal_and_inconsistent_recovery_never_write(self) -> None:
        fixture = PublicationFixture()
        fixture.journal.records.append(
            fixture.journal_entry(
                "blob:0:" + fixture.request.attestation.output_files[0].sha256,
                PublicationStage.BLOBS, WriteKind.BLOB,
                "src/app.py", None, WriteState.UNKNOWN,
            )
        )
        result = fixture.publish()
        self.assertEqual(result.error_code, "publication_recovery_required")
        self.assertEqual(fixture.gateway.write_calls, [])

        fixture = PublicationFixture()
        fixture.journal.records.append(
            fixture.journal_entry(
                "commit:published", PublicationStage.COMMIT, WriteKind.COMMIT,
                "tree", "e" * 40, WriteState.CREATED,
            )
        )
        fixture.gateway.branch_target = "f" * 40
        result = fixture.publish()
        self.assertEqual(result.error_code, "journal_read_failed")
        self.assertEqual(fixture.gateway.write_calls, [])

        fixture = PublicationFixture()
        fixture.journal.records.append(
            replace(
                fixture.journal_entry(
                    "blob:0:x", PublicationStage.BLOBS, WriteKind.BLOB,
                    "src/app.py", None, WriteState.UNKNOWN,
                ),
                run_id="OTHER",
            )
        )
        fixture.journal.load = lambda run_id: tuple(fixture.journal.records)
        result = fixture.publish()
        self.assertEqual(result.error_code, "journal_read_failed")
        self.assertEqual(fixture.gateway.write_calls, [])

        fixture = PublicationFixture()
        fixture.journal.records.append(
            fixture.journal_entry(
                "blob:0:" + fixture.request.attestation.output_files[0].sha256,
                PublicationStage.BLOBS, WriteKind.BLOB, "TOKEN-SHOULD-NOT-SURFACE",
                None, WriteState.UNKNOWN,
            )
        )
        result = fixture.publish()
        self.assertEqual(result.error_code, "journal_read_failed")
        self.assertNotIn("TOKEN-SHOULD-NOT-SURFACE", repr(result))
        self.assertEqual(fixture.gateway.write_calls, [])

    def test_recovery_lookup_or_integrity_failure_stays_read_only(self) -> None:
        fixture = PublicationFixture()
        first = fixture.publish()
        self.assertEqual(first.stage, PublicationStage.COMPLETED)
        fixture.gateway.write_calls.clear()
        fixture.gateway.find_inconclusive = True
        result = fixture.publish()
        self.assertEqual(result.error_code, "pull_lookup_failed")
        self.assertEqual(fixture.gateway.write_calls, [])

        fixture = PublicationFixture()
        first = fixture.publish()
        fixture.gateway.write_calls.clear()
        fixture.gateway._trees[first.commit_sha]["sentinel.bin"] = ("100644", b"tampered")
        result = fixture.publish()
        self.assertEqual(result.error_code, "published_tree_incomplete")
        self.assertEqual(fixture.gateway.write_calls, [])

    def test_race_gate_read_exception_fails_closed_after_commit(self) -> None:
        fixture = PublicationFixture()
        fixture.gateway.after_commit = lambda: setattr(
            fixture.gateway,
            "get_actor_permission",
            lambda *args: (_ for _ in ()).throw(RuntimeError("permission unavailable")),
        )
        result = fixture.publish()
        self.assertEqual(result.stage, PublicationStage.REF)
        self.assertEqual(result.error_code, "authorization_recheck_failed")
        self.assertFalse(any(name == "ref" for name, _ in fixture.gateway.write_calls))

    def test_path_and_digest_helpers_fail_closed_at_tree_boundary(self) -> None:
        for path in (
            None,
            "",
            "src\\app.py",
            "e\u0301.py",
            "/absolute.py",
            "src/../escape.py",
            ".git/config",
            "x" * 1025,
        ):
            with self.subTest(path=path), self.assertRaises(Exception):
                subject._normalize_tree_path(path)
        self.assertEqual(len(subject._git_blob_oid(b"content", 64)), 64)
        self.assertEqual(subject._positive_int("7"), 7)
        self.assertIsNone(subject._positive_int("0"))

    def test_git_tree_rebuild_rejects_invalid_object_evidence(self) -> None:
        blob = SourceBlob("file", "100644", b"x", git_oid(b"x"))
        invalid_graphs = (
            ((), (), 39),
            ((), (object(),), 40),
            ((), (blob, replace(blob, content=b"y", git_sha=git_oid(b"y"))), 40),
            ((object(),), (), 40),
            ((SourceTreeEntry("file", "100644", "blob", "e" * 39),), (blob,), 40),
            ((SourceTreeEntry("file", "100644", "blob", "g" * 40),), (blob,), 40),
            ((SourceTreeEntry("empty", "040000", "tree", "e" * 40),), (), 40),
            (
                (
                    SourceTreeEntry("dir", "040000", "tree", "e" * 40),
                    SourceTreeEntry("dir", "040000", "tree", "e" * 40),
                ),
                (),
                40,
            ),
            ((SourceTreeEntry("missing/file", "100644", "blob", blob.git_sha),), (replace(blob, path="missing/file"),), 40),
            ((SourceTreeEntry("file", "100755", "blob", blob.git_sha),), (blob,), 40),
            ((SourceTreeEntry("file", "120000", "blob", blob.git_sha),), (blob,), 40),
            ((SourceTreeEntry("file", "100644", "commit", blob.git_sha),), (blob,), 40),
            ((SourceTreeEntry("file", "100644", "blob", blob.git_sha),), (replace(blob, mode="100755"),), 40),
            ((SourceTreeEntry("file", "100644", "blob", blob.git_sha),), (replace(blob, content=b"y"),), 40),
            ((SourceTreeEntry("\ud800", "100644", "blob", blob.git_sha),), (replace(blob, path="\ud800"),), 40),
            ((SourceTreeEntry("bad\0name", "100644", "blob", blob.git_sha),), (replace(blob, path="bad\0name"),), 40),
        )
        for entries, blobs, length in invalid_graphs:
            with self.subTest(entries=entries, length=length):
                with self.assertRaises(subject._Failure) as error:
                    subject._rebuild_root_tree_oid(entries, blobs, length)
                self.assertEqual(error.exception.code, "parent_tree_incomplete")
        self.assertEqual(len(subject._git_tree_oid([], 64)), 64)

    def test_git_tree_rebuild_rejects_missing_blob_and_child_tree_evidence(self) -> None:
        blob = SourceBlob("dir/file", "100644", b"x", git_oid(b"x"))
        invalid_graphs = (
            ((SourceTreeEntry("file", "100644", "blob", git_oid(b"x")),), (), 40),
            ((SourceTreeEntry("file", "100644", "blob", "e" * 40),), (replace(blob, path="file"),), 40),
            (
                (
                    SourceTreeEntry("dir", "040000", "tree", "e" * 40),
                    SourceTreeEntry("dir/file", "100644", "blob", blob.git_sha),
                ),
                (blob,),
                40,
            ),
        )
        for entries, blobs, length in invalid_graphs:
            with self.subTest(entries=entries, length=length):
                with self.assertRaises(subject._Failure) as error:
                    subject._rebuild_root_tree_oid(entries, blobs, length)
                self.assertEqual(error.exception.code, "parent_tree_incomplete")

    def test_complete_tree_index_rejects_untrusted_tree_evidence(self) -> None:
        fixture = PublicationFixture()
        entries = fixture.gateway.list_tree_entries(REPOSITORY, SOURCE_HEAD)
        blobs = tuple(
            fixture.gateway.get_blob_at_commit(REPOSITORY, SOURCE_HEAD, entry.path)
            for entry in entries
            if entry.kind == "blob"
        )

        def load(
            candidate_entries: tuple[SourceTreeEntry, ...],
            candidate_blobs: tuple[SourceBlob, ...],
            root_tree: str = ROOT_TREE,
            digest: str | None = None,
        ) -> str:
            fixture.gateway.get_source_tree_index = lambda *args: subject.SourceTreeIndex(
                root_tree,
                True,
                candidate_entries,
                candidate_blobs,
                digest
                or source_index_digest(root_tree, candidate_entries, candidate_blobs),
            )
            with self.assertRaises(subject._Failure) as error:
                subject._load_complete_tree(fixture.gateway, REPOSITORY, SOURCE_HEAD)
            return error.exception.code

        tree = next(entry for entry in entries if entry.kind == "tree")
        blob = next(item for item in blobs if item.path == "src/app.py")
        self.assertEqual(load(entries, blobs, "e" * 40), "parent_tree_incomplete")
        self.assertEqual(
            load((replace(tree, mode="40000"),) + tuple(entry for entry in entries if entry != tree), blobs),
            "parent_tree_incomplete",
        )
        self.assertEqual(
            load((replace(blob, path="src/missing.py"),) + tuple(item for item in blobs if item != blob), blobs),
            "parent_tree_incomplete",
        )
        self.assertEqual(
            load(entries, tuple(item for item in blobs if item != blob)), "source_blob_mismatch"
        )
        self.assertEqual(
            load(entries, blobs, digest="e" * 64), "parent_tree_incomplete"
        )

    def test_existing_orphan_branch_is_not_adopted_or_modified(self) -> None:
        fixture = PublicationFixture()
        fixture.gateway.branch_target = "f" * 40
        result = fixture.publish()
        self.assertEqual(result.error_code, "branch_collision")
        self.assertEqual(fixture.gateway.write_calls, [])

    def test_preflight_always_resolves_existing_run_marker_before_any_write(self) -> None:
        for lookup, expected in ((77, "publication_collision"), (RuntimeError("down"), "pull_lookup_failed")):
            with self.subTest(lookup=lookup):
                fixture = PublicationFixture()

                def find(*args: object, **kwargs: object):
                    fixture.gateway._read("find_pull", (args, kwargs))
                    if isinstance(lookup, Exception):
                        raise lookup
                    return lookup

                fixture.gateway.find_draft_pull_by_run_marker = find
                result = fixture.publish()
                self.assertEqual(result.error_code, expected)
                self.assertEqual(fixture.gateway.write_calls, [])
                find_index = next(
                    index
                    for index, event in enumerate(fixture.gateway.read_calls)
                    if event[0] == "find_pull"
                )
                self.assertLess(find_index, len(fixture.gateway.read_calls))

        fixture = PublicationFixture()
        result = fixture.publish()
        self.assertEqual(result.stage, PublicationStage.COMPLETED)
        first_write = fixture.events.index("write:blob")
        self.assertTrue(any(event[0] == "find_pull" for event in fixture.gateway.read_calls))
        self.assertNotIn("write:blob", fixture.events[: fixture.events.index("write:blob")])
        self.assertGreater(first_write, 0)

    def test_trusted_complete_tree_index_rejects_filtered_sentinel_and_forged_attestation(self) -> None:
        fixture = PublicationFixture()
        full_entries = fixture.gateway.list_tree_entries(REPOSITORY, SOURCE_HEAD)
        full_blobs = tuple(
            fixture.gateway.get_blob_at_commit(REPOSITORY, SOURCE_HEAD, entry.path)
            for entry in full_entries
            if entry.kind == "blob"
        )
        filtered_entries = tuple(entry for entry in full_entries if entry.path != "sentinel.bin")
        filtered_blobs = tuple(blob for blob in full_blobs if blob.path != "sentinel.bin")
        fixture.gateway.list_tree_entries = lambda *args: filtered_entries
        fixture.gateway.get_source_tree_index = lambda *args: subject.SourceTreeIndex(
            ROOT_TREE,
            True,
            filtered_entries,
            filtered_blobs,
            source_index_digest(ROOT_TREE, filtered_entries, filtered_blobs),
        )
        forged_complete = tuple(
            file_digest(path, mode, content)
            for path, (mode, content) in sorted(OUTPUT_FILES.items())
            if path != "sentinel.bin"
        )
        fixture.request = replace(
            fixture.request,
            attestation=replace(
                fixture.request.attestation,
                workspace_tree_digest=tree_digest(forged_complete),
            ),
        )
        result = fixture.publish()
        self.assertEqual(result.error_code, "parent_tree_incomplete")
        self.assertEqual(fixture.gateway.write_calls, [])

    def test_journal_fsm_requires_created_before_next_operation_and_binds_context(self) -> None:
        fixture = PublicationFixture()
        result = fixture.publish()
        self.assertEqual(result.stage, PublicationStage.COMPLETED)
        for record in fixture.journal.records:
            self.assertEqual(getattr(record, "repository", None), REPOSITORY)
            self.assertEqual(getattr(record, "source_head_sha", None), SOURCE_HEAD)
            self.assertEqual(getattr(record, "target_base_sha", None), BASE_SHA)
            self.assertEqual(getattr(record, "manifest_digest", None), fixture.request.manifest.digest)
            self.assertEqual(getattr(record, "workflow_run_id", None), RUNTIME.workflow_run_id)

        malformed = PublicationFixture()
        first_operation = "blob:0:" + malformed.request.attestation.output_files[0].sha256
        second_operation = "blob:1:" + malformed.request.attestation.output_files[1].sha256
        malformed.journal.records.extend(
            (
                malformed.journal_entry(
                    first_operation, PublicationStage.BLOBS, WriteKind.BLOB,
                    "src/app.py", None, WriteState.UNKNOWN,
                ),
                malformed.journal_entry(
                    first_operation, PublicationStage.BLOBS, WriteKind.BLOB,
                    "src/app.py", None, WriteState.UNKNOWN,
                ),
                malformed.journal_entry(
                    second_operation, PublicationStage.BLOBS, WriteKind.BLOB,
                    "src/new.py", None, WriteState.UNKNOWN,
                ),
            )
        )
        result = malformed.publish()
        self.assertEqual(result.error_code, "journal_read_failed")
        self.assertEqual(malformed.gateway.write_calls, [])

    def test_recovery_reconciles_lone_unknown_ref_without_rewriting(self) -> None:
        fixture = PublicationFixture()
        fixture.journal.fail_at = 9
        first = fixture.publish()
        self.assertEqual(first.error_code, "journal_write_unknown")
        self.assertIsNotNone(fixture.gateway.branch_target)
        fixture.journal.fail_at = None
        fixture.gateway.write_calls.clear()
        before = len(fixture.journal.records)
        second = fixture.publish()
        self.assertNotEqual(second.stage, PublicationStage.COMPLETED)
        self.assertEqual(fixture.gateway.write_calls, [])
        self.assertEqual(len(fixture.journal.records), before + 1)
        self.assertEqual(fixture.journal.records[-1].kind, WriteKind.REF)
        self.assertEqual(fixture.journal.records[-1].state, WriteState.CREATED)

        fixture = PublicationFixture()
        fixture.journal.fail_at = 9
        fixture.gateway.failures["ref"] = "before"
        first = fixture.publish()
        self.assertEqual(first.error_code, "journal_write_unknown")
        fixture.journal.fail_at = None
        fixture.gateway.write_calls.clear()
        second = fixture.publish()
        self.assertEqual(second.error_code, "publication_recovery_required")
        self.assertEqual(second.branch_state, WriteState.NOT_CREATED)
        self.assertEqual(fixture.gateway.write_calls, [])

        fixture = PublicationFixture()
        fixture.journal.fail_at = 9
        first = fixture.publish()
        self.assertEqual(first.error_code, "journal_write_unknown")
        fixture.gateway.write_calls.clear()
        second = fixture.publish()
        self.assertEqual(second.error_code, "journal_write_unknown")
        self.assertEqual(fixture.gateway.write_calls, [])

    def test_recovery_reconciles_terminal_unknown_ref_when_it_becomes_visible(self) -> None:
        fixture = PublicationFixture()
        original_get_ref = fixture.gateway.get_ref_target
        fail_reconciliation_once = False

        def get_ref(repository: str, branch_name: str) -> str | None:
            nonlocal fail_reconciliation_once
            if branch_name == BRANCH and fail_reconciliation_once:
                fail_reconciliation_once = False
                raise RuntimeError("first reconciliation unavailable")
            return original_get_ref(repository, branch_name)

        def uncertain_ref(**kwargs: object) -> None:
            nonlocal fail_reconciliation_once
            fixture.gateway._before_write("ref", tuple(kwargs.values()))
            fixture.gateway.branch_target = str(kwargs["commit_sha"])
            fail_reconciliation_once = True
            raise PublicationWriteError(
                "ref_transport", PublicationWriteDisposition.MAY_HAVE_BEEN_ACCEPTED
            )

        fixture.gateway.get_ref_target = get_ref
        fixture.gateway.create_ref = uncertain_ref
        first = fixture.publish()
        self.assertEqual(first.branch_state, WriteState.UNKNOWN)
        self.assertEqual(fixture.journal.records[-1].state, WriteState.UNKNOWN)
        before = len(fixture.journal.records)
        fixture.gateway.write_calls.clear()
        second = fixture.publish()
        self.assertEqual(second.error_code, "publication_recovery_required")
        self.assertEqual(second.branch_state, WriteState.CREATED)
        self.assertEqual(fixture.gateway.write_calls, [])
        self.assertEqual(len(fixture.journal.records), before + 1)
        self.assertEqual(fixture.journal.records[-1].state, WriteState.CREATED)

    def test_recovery_reconciles_lone_unknown_pull_without_rewriting(self) -> None:
        fixture = PublicationFixture()
        fixture.journal.fail_at = 11
        first = fixture.publish()
        self.assertEqual(first.error_code, "journal_write_unknown")
        self.assertEqual(fixture.gateway._pull_number, 77)
        fixture.journal.fail_at = None
        fixture.gateway.write_calls.clear()
        second = fixture.publish()
        self.assertEqual(second.stage, PublicationStage.COMPLETED)
        self.assertEqual(second.pull_number, 77)
        self.assertEqual(fixture.gateway.write_calls, [])
        self.assertEqual(fixture.journal.records[-1].kind, WriteKind.PULL)
        self.assertEqual(fixture.journal.records[-1].state, WriteState.CREATED)

        fixture = PublicationFixture()
        fixture.journal.fail_at = 11
        fixture.gateway.failures["pull"] = "before"
        first = fixture.publish()
        self.assertEqual(first.error_code, "journal_write_unknown")
        fixture.journal.fail_at = None
        fixture.gateway.write_calls.clear()
        second = fixture.publish()
        self.assertEqual(second.error_code, "publication_recovery_required")
        self.assertEqual(second.pull_state, WriteState.NOT_CREATED)
        self.assertEqual(fixture.gateway.write_calls, [])

    def test_created_pull_requires_authoritative_draft_snapshot_and_stable_ref(self) -> None:
        mutators = {
            "branch": lambda fixture: setattr(
                fixture.gateway,
                "after_pull",
                lambda: setattr(fixture.gateway, "branch_target", "f" * 40),
            ),
            "not_draft": lambda fixture: setattr(fixture.gateway, "published_draft", False),
            "wrong_author": lambda fixture: setattr(fixture.gateway, "published_author", "attacker"),
            "fork_head_repo": lambda fixture: setattr(
                fixture.gateway, "published_head_repository", "fork/repo"
            ),
            "wrong_head": lambda fixture: setattr(fixture.gateway, "published_head", "f" * 40),
            "wrong_base": lambda fixture: setattr(fixture.gateway, "published_base", "release"),
            "unavailable": lambda fixture: setattr(fixture.gateway, "published_unavailable", True),
        }
        for name, mutate in mutators.items():
            with self.subTest(name=name):
                fixture = PublicationFixture()
                mutate(fixture)
                result = fixture.publish()
                self.assertEqual(result.stage, PublicationStage.PULL)
                self.assertEqual(result.pull_state, WriteState.UNKNOWN)
                self.assertTrue(result.partial)
                self.assertNotEqual(result.stage, PublicationStage.COMPLETED)

    def test_race_guard_rejects_non_boolean_cancel_and_not_created_is_not_partial(self) -> None:
        for value in (None, 0):
            with self.subTest(value=value):
                fixture = PublicationFixture()
                fixture.gateway.after_commit = lambda value=value: setattr(
                    fixture.state, "canceled", value
                )
                result = fixture.publish()
                self.assertEqual(result.stage, PublicationStage.REF)
                self.assertEqual(result.error_code, "authorization_recheck_failed")
                self.assertFalse(any(name in {"ref", "pull"} for name, _ in fixture.gateway.write_calls))

            with self.subTest(value=value, gate="after_ref"):
                fixture = PublicationFixture()
                fixture.gateway.after_ref = lambda value=value: setattr(
                    fixture.state, "canceled", value
                )
                result = fixture.publish()
                self.assertEqual(result.stage, PublicationStage.PULL)
                self.assertEqual(result.error_code, "authorization_recheck_failed")
                self.assertFalse(any(name == "pull" for name, _ in fixture.gateway.write_calls))

        fixture = PublicationFixture()
        fixture.gateway.failures["blob"] = "before"
        result = fixture.publish()
        self.assertEqual(result.receipts[0].state, WriteState.NOT_CREATED)
        self.assertFalse(result.partial)

    def test_gateway_protocol_exposes_no_destructive_or_general_write_capability(self) -> None:
        attributes = set(ChangeGitHubGateway.__dict__)
        for forbidden in (
            "approve", "merge", "close", "delete", "update_ref", "force_push",
            "settings", "secrets", "request", "general_request",
        ):
            self.assertNotIn(forbidden, attributes)


if __name__ == "__main__":
    unittest.main()
