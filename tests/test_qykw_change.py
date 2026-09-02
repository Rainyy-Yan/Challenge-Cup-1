"""Tests for qykw's deterministic authorized-change boundary."""

from dataclasses import FrozenInstanceError, replace
import hashlib
from pathlib import Path
import unittest

from tools.qykw.change import (
    AppliedPatch,
    ChangePublication,
    ChangeKind,
    ChangeRequest,
    CommandResult,
    FileDigest,
    FilePatch,
    PatchManifest,
    PreparedWorkspace,
    PublicationStage,
    PublishedFile,
    SourceBlob,
    SourceTreeEntry,
    SourceTreeIndex,
    TextEdit,
    TrustedSourceTreeProvider,
    VerificationAttestation,
    WriteKind,
    WriteReceipt,
    WriteState,
    compute_source_tree_index_digest,
)
from tools.qykw.config import parse_qykw_config
from tools.qykw.domain import (
    ChangedFile,
    CommandMode,
    CommandName,
    CommandRequest,
    CommentKind,
    PullSnapshot,
    RunContext,
)
from tools.qykw.policy import DeterministicChangePolicy


def context(
    *,
    actor: str = "xyh202131",
    command: CommandName = CommandName.FIX,
    event_name: str = "issue_comment",
) -> RunContext:
    return RunContext(
        "QY-PR53-A1B2",
        "comment:77",
        8,
        "owner/repo",
        53,
        event_name,
        "created",
        "fork/repo",
        "a" * 40,
        "b" * 40,
        "main",
        CommandRequest(command, "修复问题", CommandMode.CHANGE),
        actor,
        77,
        (
            CommentKind.REVIEW
            if event_name == "pull_request_review_comment"
            else CommentKind.ISSUE
        ),
    )


def change_request(**context_changes: object) -> ChangeRequest:
    run = context(**context_changes)  # type: ignore[arg-type]
    kind = ChangeKind.IMPLEMENT if run.command.name is CommandName.IMPLEMENT else ChangeKind.FIX
    return ChangeRequest(
        run,
        kind,
        run.command.argument,
        run.source_repository,
        run.repository,
        run.source_head_sha,
        run.target_base_sha,
        run.target_base_ref,
        "full",
    )


def changed_file(
    path: str = "core/service.py",
    *,
    content: str | None = "old value\n",
    mode: str = "100644",
    binary: bool = False,
    status: str = "modified",
) -> ChangedFile:
    head_sha = (
        "d" * 40
        if mode == "160000"
        else git_blob_sha((content or "").encode("utf-8"))
    )
    return ChangedFile(
        path,
        None,
        status,
        "base-sha",
        head_sha,
        "100644",
        mode,
        content,
        content,
        "@@",
        binary,
        False,
        1,
        1,
    )


def snapshot(*files: ChangedFile) -> PullSnapshot:
    return PullSnapshot(
        53,
        "open",
        False,
        "fork/repo",
        "a" * 40,
        "owner/repo",
        "b" * 40,
        "main",
        "title",
        "body",
        files or (changed_file(),),
        (),
        (),
        (),
    )


def manifest(request: ChangeRequest, *files: FilePatch) -> PatchManifest:
    return PatchManifest(
        1,
        request.context.run_id,
        request.source_repository,
        request.target_repository,
        request.context.pr_number,
        request.source_head_sha,
        request.target_base_sha,
        request.target_base_ref,
        request.verification_profile,
        files or (FilePatch("core/service.py", "0" * 64, False, (TextEdit("old", "new"),)),),
        "1" * 64,
    )


def git_blob_sha(content: bytes) -> str:
    framed = f"blob {len(content)}\0".encode("ascii") + content
    return hashlib.sha1(framed).hexdigest()


def tree_index(
    *entries: SourceTreeEntry,
    blobs: tuple[SourceBlob, ...] = (),
    complete: bool = True,
) -> SourceTreeIndex:
    default_entries = (
        SourceTreeEntry("core", "040000", "tree", "e" * 40),
        SourceTreeEntry(
            "core/service.py",
            "100644",
            "blob",
            git_blob_sha(b"old value\n"),
        ),
    )
    provisional = SourceTreeIndex(
        1,
        "a" * 40,
        "f" * 40,
        complete,
        entries or default_entries,
        blobs,
        "0" * 64,
    )
    return replace(provisional, digest=compute_source_tree_index_digest(provisional))


class FakeTreeProvider:
    def __init__(
        self,
        index: SourceTreeIndex | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.index = index or tree_index()
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def get_complete_tree(
        self, source_repository: str, source_head_sha: str
    ) -> SourceTreeIndex:
        self.calls.append((source_repository, source_head_sha))
        if self.error is not None:
            raise self.error
        return self.index


def change_config():
    return parse_qykw_config(
        {
            "version": 1,
            "authorization": {"code_writers": ["xyh202131"]},
            "verification": {"profiles": ["backend", "frontend", "full"]},
        }
    )


def policy(
    *,
    source_tree: SourceTreeIndex | None = None,
    tree_provider: TrustedSourceTreeProvider | None = None,
) -> DeterministicChangePolicy:
    return DeterministicChangePolicy(
        change_config(),
        tree_provider=(
            FakeTreeProvider(source_tree)
            if tree_provider is None
            else tree_provider
        ),
    )


class TestChangePolicy(unittest.TestCase):
    def test_change_contracts_are_immutable(self) -> None:
        request = change_request()
        with self.assertRaises(FrozenInstanceError):
            request.instruction = "override"  # type: ignore[misc]
        patch = FilePatch("core/service.py", "0" * 64, False, (TextEdit("a", "b"),))
        with self.assertRaises(FrozenInstanceError):
            patch.path = "other.py"  # type: ignore[misc]

    def test_contract_tuple_fields_reject_mutable_or_wrong_typed_values(self) -> None:
        request = change_request()
        with self.assertRaises((TypeError, ValueError)):
            FilePatch("core/service.py", "0" * 64, False, [TextEdit("a", "b")])  # type: ignore[arg-type]
        with self.assertRaises((TypeError, ValueError)):
            FilePatch("core/service.py", "0" * 64, False, ("not-an-edit",))  # type: ignore[arg-type]
        with self.assertRaises((TypeError, ValueError)):
            replace(manifest(request), files=[manifest(request).files[0]])  # type: ignore[arg-type]
        with self.assertRaises((TypeError, ValueError)):
            replace(manifest(request), files=("not-a-patch",))  # type: ignore[arg-type]

        digest = FileDigest("core/service.py", "100644", "0" * 64)
        result = CommandResult("tests", "1" * 64, 0, False, 1, "2" * 64, "OK")
        receipt = WriteReceipt(WriteKind.BLOB, "core/service.py", "sha", WriteState.CREATED)
        tuple_objects = (
            (PreparedWorkspace(Path("workspace"), "a" * 40, (digest,)), "source_files"),
            (
                VerificationAttestation(
                    1,
                    1,
                    request.context.run_id,
                    request.source_repository,
                    request.source_head_sha,
                    request.target_repository,
                    request.target_base_sha,
                    request.target_base_ref,
                    "1" * 64,
                    "full",
                    "image",
                    "2" * 64,
                    "3" * 64,
                    (digest,),
                    True,
                    False,
                    (result,),
                ),
                "output_files",
            ),
            (
                VerificationAttestation(
                    1,
                    1,
                    request.context.run_id,
                    request.source_repository,
                    request.source_head_sha,
                    request.target_repository,
                    request.target_base_sha,
                    request.target_base_ref,
                    "1" * 64,
                    "full",
                    "image",
                    "2" * 64,
                    "3" * 64,
                    (digest,),
                    True,
                    False,
                    (result,),
                ),
                "results",
            ),
            (AppliedPatch((digest,), "2" * 64, "3" * 64), "files"),
            (tree_index(), "entries"),
            (tree_index(), "blobs"),
            (
                ChangePublication(
                    PublicationStage.BLOBS,
                    "qykw/run-fix",
                    WriteState.NOT_CREATED,
                    WriteState.NOT_CREATED,
                    None,
                    None,
                    (receipt,),
                    False,
                    None,
                ),
                "receipts",
            ),
        )
        for value, field in tuple_objects:
            with self.subTest(type=type(value).__name__, field=field):
                with self.assertRaises(TypeError):
                    replace(value, **{field: list(getattr(value, field))})
                with self.assertRaises(TypeError):
                    replace(value, **{field: (object(),)})

        with self.assertRaises(TypeError):
            SourceBlob("file.py", "100644", bytearray(b"x"), "sha")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            PublishedFile("file.py", "100644", bytearray(b"x"), "0" * 64)  # type: ignore[arg-type]

    def test_only_authorized_exact_pr_comment_change_commands_are_accepted(self) -> None:
        subject = policy()
        subject.validate_request(change_request(), snapshot())
        subject.validate_request(
            change_request(command=CommandName.IMPLEMENT),
            snapshot(),
        )
        subject.validate_request(
            change_request(event_name="pull_request_review_comment"),
            snapshot(),
        )

        rejected = (
            change_request(actor="mallory"),
            change_request(command=CommandName.ANALYZE),
            change_request(event_name="pull_request"),
            replace(
                change_request(event_name="pull_request_review_comment"),
                context=replace(
                    context(event_name="pull_request_review_comment"),
                    trigger_comment_kind=CommentKind.ISSUE,
                ),
            ),
            replace(change_request(), context=replace(context(), trigger_comment_id=None)),
            replace(change_request(), instruction="different"),
            replace(change_request(), verification_profile="backend"),
        )
        for request in rejected:
            with self.subTest(request=request):
                with self.assertRaises(ValueError):
                    policy().validate_request(request, snapshot())

    def test_request_is_bound_to_the_fixed_pull_snapshot(self) -> None:
        request = change_request()
        fields = (
            "source_repository",
            "target_repository",
            "source_head_sha",
            "target_base_sha",
            "target_base_ref",
        )
        for field in fields:
            with self.subTest(field=field):
                altered = replace(request, **{field: "wrong"})
                with self.assertRaises(ValueError):
                    policy().validate_request(altered, snapshot())

    def test_failed_reauthorization_clears_the_previous_run_atomically(self) -> None:
        request = change_request()
        subject = policy()
        subject.validate_request(request, snapshot())
        subject.validate_manifest(request, manifest(request))

        with self.assertRaises(ValueError):
            subject.validate_request(request, replace(snapshot(), state="closed"))
        with self.assertRaisesRegex(ValueError, "request_not_validated"):
            subject.validate_manifest(request, manifest(request))

    def test_rejects_path_traversal_confusion_and_sensitive_paths(self) -> None:
        request = change_request()
        dangerous = (
            "/tmp/pwn.py",
            "C:/tmp/pwn.py",
            "../pwn.py",
            "core/../pwn.py",
            "core\\service.py",
            ".GIT/config",
            ".GitHub/workflows/ci.yml",
            "CODEOWNERS",
            "docs/Agents.MD",
            "TOOLS/QYKW/change.py",
            "tools/check_qykw_coverage.py",
            ".coveragerc",
            "requirements-dev.txt",
            "config/permissions.toml",
            "config/secrets.env",
            ".env.production",
            "config/credentials.json",
        )
        for path in dangerous:
            with self.subTest(path=path):
                subject = policy()
                subject.validate_request(request, snapshot())
                with self.assertRaises(ValueError):
                    subject.validate_manifest(
                        request,
                        manifest(request, FilePatch(path, None, True, (TextEdit("", "x"),))),
                    )

    def test_rejects_noncanonical_cross_platform_paths_and_collisions(self) -> None:
        request = change_request()
        dangerous = (
            "cafe\u0301.py",
            "bad\u0001.py",
            "bad\u0085.py",
            "bad\u200b.py",
            "stream.py:payload",
            "bad<name.py",
            "bad>name.py",
            'bad"name.py',
            "bad|name.py",
            "bad?name.py",
            "bad*name.py",
            "trailing./file.py",
            "trailing /file.py",
            "CON.py",
            "CONIN$.txt",
            "CONOUT$.txt",
            "dir/AUX.txt",
            "COM¹.txt",
            "COM².txt",
            "COM³.txt",
            "LPT¹.txt",
            "LPT².txt",
            "LPT³.txt",
            "a" * 256 + ".py",
            "x/" * 520 + "file.py",
            ".gitmodules",
        )
        for path in dangerous:
            with self.subTest(path=path[:40]):
                subject = policy()
                subject.validate_request(request, snapshot())
                with self.assertRaises(ValueError):
                    subject.validate_manifest(
                        request,
                        manifest(
                            request,
                            FilePatch(path, None, True, (TextEdit("", "x"),)),
                        ),
                    )

        subject = policy()
        subject.validate_request(request, snapshot())
        with self.assertRaises(ValueError):
            subject.validate_manifest(
                request,
                manifest(
                    request,
                    FilePatch("Straße.py", None, True, (TextEdit("", "x"),)),
                    FilePatch("STRASSE.py", None, True, (TextEdit("", "y"),)),
                ),
            )

        colliding_tree = tree_index(
            SourceTreeEntry("core", "040000", "tree", "e" * 40),
            SourceTreeEntry(
                "core/service.py", "100644", "blob", git_blob_sha(b"old value\n")
            ),
            SourceTreeEntry("Straße.py", "100644", "blob", "1" * 40),
            SourceTreeEntry("STRASSE.py", "100644", "blob", "2" * 40),
        )
        with self.assertRaises(ValueError):
            policy(source_tree=colliding_tree).validate_request(request, snapshot())

    def test_rejects_symlink_submodule_binary_removed_and_generated_targets(self) -> None:
        request = change_request()
        unsafe = (
            changed_file(mode="120000"),
            changed_file(mode="160000"),
            changed_file(binary=True, content=None),
            changed_file(status="removed", content=None),
            replace(changed_file(), generated=True),
        )
        for file in unsafe:
            with self.subTest(file=file):
                subject = policy()
                with self.assertRaises(ValueError):
                    subject.validate_request(request, snapshot(file))
                    subject.validate_manifest(request, manifest(request))

        subject = policy(
            source_tree=tree_index(
                SourceTreeEntry("vendor", "160000", "commit", "d" * 40),
            )
        )
        subject.validate_request(
            request,
            snapshot(changed_file("vendor", mode="160000", content="gitlink")),
        )
        with self.assertRaises(ValueError):
            subject.validate_manifest(
                request,
                manifest(
                    request,
                    FilePatch("vendor/new.py", None, True, (TextEdit("", "x"),)),
                ),
            )

    def test_unrelated_sensitive_pull_file_does_not_block_a_safe_target(self) -> None:
        request = change_request()
        subject = policy(
            source_tree=tree_index(
                SourceTreeEntry("core", "040000", "tree", "e" * 40),
                SourceTreeEntry(
                    "core/service.py",
                    "100644",
                    "blob",
                    git_blob_sha(b"old value\n"),
                ),
                SourceTreeEntry(".github", "040000", "tree", "b" * 40),
                SourceTreeEntry(
                    ".github/workflows", "040000", "tree", "c" * 40
                ),
                SourceTreeEntry(
                    ".github/workflows/ci.yml",
                    "100644",
                    "blob",
                    git_blob_sha(b"old value\n"),
                ),
            )
        )
        subject.validate_request(
            request,
            snapshot(changed_file(), changed_file(".github/workflows/ci.yml")),
        )
        subject.validate_manifest(request, manifest(request))

    def test_create_requires_a_complete_tree_proof_of_absence(self) -> None:
        request = change_request()
        with self.assertRaises(TypeError):
            DeterministicChangePolicy(change_config())  # type: ignore[call-arg]
        with self.assertRaises(ValueError):
            policy(tree_provider=FakeTreeProvider(error=RuntimeError("offline"))).validate_request(
                request, snapshot()
            )
        with self.assertRaises(ValueError):
            policy(source_tree=tree_index(complete=False)).validate_request(
                request, snapshot()
            )

        subject = policy(
            source_tree=tree_index(
                SourceTreeEntry("core", "040000", "tree", "e" * 40),
                SourceTreeEntry(
                    "core/service.py",
                    "100644",
                    "blob",
                    git_blob_sha(b"old value\n"),
                ),
                SourceTreeEntry("existing.py", "100644", "blob", "3" * 40),
            )
        )
        subject.validate_request(request, snapshot())

        # A changed-files listing cannot prove that this unchanged path is
        # absent from the fixed Head tree.
        with self.assertRaises(ValueError):
            subject.validate_manifest(
                request,
                manifest(
                    request,
                    FilePatch("existing.py", None, True, (TextEdit("", "new"),)),
                ),
            )

    def test_complete_tree_allows_only_proven_regular_blob_and_tree_parents(self) -> None:
        request = change_request()
        source_tree = tree_index(
            SourceTreeEntry("core", "040000", "tree", "e" * 40),
            SourceTreeEntry(
                "core/service.py", "100644", "blob", git_blob_sha(b"old value\n")
            ),
            SourceTreeEntry(
                "unchanged.py", "100644", "blob", git_blob_sha(b"before\n")
            ),
            SourceTreeEntry("link", "120000", "blob", git_blob_sha(b"target")),
            blobs=(
                SourceBlob(
                    "unchanged.py",
                    "100644",
                    b"before\n",
                    git_blob_sha(b"before\n"),
                ),
            ),
        )
        subject = policy(source_tree=source_tree)
        subject.validate_request(request, snapshot())
        subject.validate_manifest(
            request,
            manifest(
                request,
                FilePatch(
                    "unchanged.py",
                    "0" * 64,
                    False,
                    (TextEdit("before", "after"),),
                ),
            ),
        )
        with self.assertRaises(ValueError):
            subject.validate_manifest(
                request,
                manifest(
                    request,
                    FilePatch("link", "0" * 64, False, (TextEdit("a", "b"),)),
                ),
            )
        with self.assertRaises(ValueError):
            subject.validate_manifest(
                request,
                manifest(
                    request,
                    FilePatch("missing/new.py", None, True, (TextEdit("", "x"),)),
                ),
            )

        invalid_tree = tree_index(
            SourceTreeEntry(
                "core/service.py", "100644", "blob", git_blob_sha(b"old value\n")
            ),
        )
        with self.assertRaises(ValueError):
            policy(source_tree=invalid_tree).validate_request(request, snapshot())

    def test_tree_provider_is_fixed_to_repository_head_and_index_provenance(self) -> None:
        request = change_request()
        provider = FakeTreeProvider()
        subject = policy(tree_provider=provider)
        subject.validate_request(request, snapshot())
        self.assertEqual(
            provider.calls,
            [(request.source_repository, request.source_head_sha)],
        )
        self.assertFalse(hasattr(SourceTreeIndex, "from_mapping"))

        bad_digest = replace(tree_index(), digest="0" * 64)
        with self.assertRaises(ValueError):
            policy(source_tree=bad_digest).validate_request(request, snapshot())
        bad_root = replace(tree_index(), root_tree_sha="not-an-oid")
        bad_root = replace(
            bad_root,
            digest=compute_source_tree_index_digest(bad_root),
        )
        with self.assertRaises(ValueError):
            policy(source_tree=bad_root).validate_request(request, snapshot())

        expected_sha = git_blob_sha(b"before\n")
        bad_blob = tree_index(
            SourceTreeEntry("core", "040000", "tree", "e" * 40),
            SourceTreeEntry(
                "core/service.py", "100644", "blob", git_blob_sha(b"old value\n")
            ),
            SourceTreeEntry("unchanged.py", "100644", "blob", expected_sha),
            blobs=(
                SourceBlob("unchanged.py", "100644", b"tampered\n", expected_sha),
            ),
        )
        with self.assertRaises(ValueError):
            policy(source_tree=bad_blob).validate_request(request, snapshot())

    def test_additional_policy_boundaries_fail_closed(self) -> None:
        request = change_request()
        bad_mode = replace(
            request,
            context=replace(
                request.context,
                command=replace(request.context.command, mode=CommandMode.READ_ONLY),
            ),
        )
        with self.assertRaises(ValueError):
            policy().validate_request(bad_mode, snapshot())
        with self.assertRaises(ValueError):
            policy().validate_request(
                replace(
                    request,
                    context=replace(request.context, event_action="deleted"),
                ),
                snapshot(),
            )

        no_full = parse_qykw_config(
            {
                "version": 1,
                "authorization": {"code_writers": ["xyh202131"]},
                "verification": {"profiles": ["backend"]},
            }
        )
        with self.assertRaises(ValueError):
            DeterministicChangePolicy(
                no_full,
                tree_provider=FakeTreeProvider(),
            ).validate_request(request, snapshot())

        invalid_result = FakeTreeProvider()
        invalid_result.index = object()  # type: ignore[assignment]
        with self.assertRaises(ValueError):
            policy(tree_provider=invalid_result).validate_request(request, snapshot())
        with self.assertRaises(ValueError):
            policy().validate_request(
                request,
                snapshot(changed_file(), changed_file()),
            )

        wrong_head = replace(tree_index(), source_head_sha="b" * 40)
        wrong_head = replace(
            wrong_head,
            digest=compute_source_tree_index_digest(wrong_head),
        )
        with self.assertRaises(ValueError):
            policy(source_tree=wrong_head).validate_request(request, snapshot())

        invalid_kind = tree_index(
            SourceTreeEntry("core", "040000", "tree", "e" * 40),
            SourceTreeEntry(
                "core/service.py", "100644", "unknown", git_blob_sha(b"old value\n")
            ),
        )
        with self.assertRaises(ValueError):
            policy(source_tree=invalid_kind).validate_request(request, snapshot())

        unchanged_sha = git_blob_sha(b"before\n")
        duplicate_blobs = tree_index(
            SourceTreeEntry("core", "040000", "tree", "e" * 40),
            SourceTreeEntry(
                "core/service.py", "100644", "blob", git_blob_sha(b"old value\n")
            ),
            SourceTreeEntry("unchanged.py", "100644", "blob", unchanged_sha),
            blobs=(
                SourceBlob("unchanged.py", "100644", b"before\n", unchanged_sha),
                SourceBlob("unchanged.py", "100644", b"before\n", unchanged_sha),
            ),
        )
        with self.assertRaises(ValueError):
            policy(source_tree=duplicate_blobs).validate_request(request, snapshot())

        subject = policy()
        subject.validate_request(request, snapshot())
        for invalid_manifest in (
            replace(manifest(request), schema_version=2),
            replace(manifest(request), digest="not-a-digest"),
            replace(manifest(request), files=()),
        ):
            with self.subTest(manifest=invalid_manifest):
                with self.assertRaises(ValueError):
                    subject.validate_manifest(request, invalid_manifest)

    def test_rejects_delete_empty_duplicate_and_oversized_changes(self) -> None:
        request = change_request()
        cases = (
            FilePatch("core/service.py", "0" * 64, False, ()),
            FilePatch("core/service.py", "0" * 64, False, (TextEdit("old", ""),)),
            FilePatch("new.py", None, True, (TextEdit("", ""),)),
            FilePatch("new.py", None, True, (TextEdit("", "x"), TextEdit("", "y"))),
            FilePatch("new.py", None, True, (TextEdit("", "x" * (256 * 1024 + 1)),)),
        )
        for patch in cases:
            with self.subTest(patch=patch.path):
                subject = policy()
                subject.validate_request(request, snapshot())
                with self.assertRaises(ValueError):
                    subject.validate_manifest(request, manifest(request, patch))

        subject = policy()
        subject.validate_request(request, snapshot())
        duplicate = manifest(
            request,
            FilePatch("new.py", None, True, (TextEdit("", "x"),)),
            FilePatch("new.py", None, True, (TextEdit("", "y"),)),
        )
        with self.assertRaises(ValueError):
            subject.validate_manifest(request, duplicate)

        subject = policy()
        subject.validate_request(request, snapshot())
        secret_reference = manifest(
            request,
            FilePatch(
                "new.py",
                None,
                True,
                (TextEdit("", "token = '${{ secrets.DEPLOY_TOKEN }}'\n"),),
            ),
        )
        with self.assertRaises(ValueError):
            subject.validate_manifest(request, secret_reference)

    def test_rejects_instruction_replay_and_manifest_capacity_overflow(self) -> None:
        request = replace(change_request(), instruction="x" * (16 * 1024 + 1))
        request = replace(
            request,
            context=replace(
                request.context,
                command=replace(request.context.command, argument=request.instruction),
            ),
        )
        with self.assertRaises(ValueError):
            policy().validate_request(request, snapshot())

        request = change_request()
        subject = policy(
            source_tree=tree_index(
                SourceTreeEntry("core", "040000", "tree", "e" * 40),
                SourceTreeEntry(
                    "core/service.py",
                    "100644",
                    "blob",
                    git_blob_sha(b"LEFT RIGHT\n"),
                ),
            )
        )
        subject.validate_request(
            request,
            snapshot(changed_file(content="LEFT RIGHT\n")),
        )
        with self.assertRaises(ValueError):
            subject.validate_manifest(
                request,
                manifest(
                    request,
                    FilePatch(
                        "core/service.py",
                        "0" * 64,
                        False,
                        (
                            TextEdit("LEFT", "L" * 140_000),
                            TextEdit("RIGHT", "R" * 140_000),
                        ),
                    ),
                ),
            )

        subject = policy()
        subject.validate_request(request, snapshot())
        wide = tuple(
            FilePatch(
                f"generated-{index}.py",
                None,
                True,
                (TextEdit("", "x" * 200_000),),
            )
            for index in range(20)
        )
        with self.assertRaises(ValueError):
            subject.validate_manifest(request, manifest(request, *wide))

    def test_rejects_secret_reference_variants_without_blocking_plain_text(self) -> None:
        request = change_request()
        unsafe = (
            "value = '${{ github.token }}'\n",
            'value = "${{ github[\'token\'] }}"\n',
            'value = "${{ secrets[\'DEPLOY\'] }}"\n',
            'value = os.environ["GITHUB_TOKEN"]\n',
            "value = process.env.QYKW_PUBLISH_TOKEN\n",
            'value = getenv("QYKW_REVIEW_TOKEN")\n',
        )
        for index, content in enumerate(unsafe):
            with self.subTest(index=index):
                subject = policy()
                subject.validate_request(request, snapshot())
                with self.assertRaises(ValueError):
                    subject.validate_manifest(
                        request,
                        manifest(
                            request,
                            FilePatch("new.py", None, True, (TextEdit("", content),)),
                        ),
                    )

        subject = policy()
        subject.validate_request(request, snapshot())
        subject.validate_manifest(
            request,
            manifest(
                request,
                FilePatch(
                    "new.py",
                    None,
                    True,
                    (TextEdit("", "tokenization keeps ordinary prose readable\n"),),
                ),
            ),
        )

    def test_manifest_binding_and_profile_cannot_be_overridden(self) -> None:
        request = change_request()
        subject = policy()
        subject.validate_request(request, snapshot())
        valid = manifest(request)
        subject.validate_manifest(request, valid)

        for field in (
            "run_id",
            "source_repository",
            "target_repository",
            "source_head_sha",
            "target_base_sha",
            "target_base_ref",
            "verification_profile",
        ):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    subject.validate_manifest(request, replace(valid, **{field: "wrong"}))


if __name__ == "__main__":
    unittest.main()
