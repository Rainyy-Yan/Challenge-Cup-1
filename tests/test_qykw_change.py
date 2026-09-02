"""Tests for qykw's deterministic authorized-change boundary."""

from dataclasses import FrozenInstanceError, replace
import unittest

from tools.qykw.change import (
    ChangeKind,
    ChangeRequest,
    FilePatch,
    PatchManifest,
    TextEdit,
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
    return ChangedFile(
        path,
        None,
        status,
        "base-sha",
        "head-sha",
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


def policy() -> DeterministicChangePolicy:
    config = parse_qykw_config(
        {
            "version": 1,
            "authorization": {"code_writers": ["xyh202131"]},
            "verification": {"profiles": ["backend", "frontend", "full"]},
        }
    )
    return DeterministicChangePolicy(config)


class TestChangePolicy(unittest.TestCase):
    def test_change_contracts_are_immutable(self) -> None:
        request = change_request()
        with self.assertRaises(FrozenInstanceError):
            request.instruction = "override"  # type: ignore[misc]
        patch = FilePatch("core/service.py", "0" * 64, False, (TextEdit("a", "b"),))
        with self.assertRaises(FrozenInstanceError):
            patch.path = "other.py"  # type: ignore[misc]

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
                subject.validate_request(request, snapshot(file))
                with self.assertRaises(ValueError):
                    subject.validate_manifest(request, manifest(request))

        subject = policy()
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
        subject = policy()
        subject.validate_request(
            request,
            snapshot(changed_file(), changed_file(".github/workflows/ci.yml")),
        )
        subject.validate_manifest(request, manifest(request))

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
