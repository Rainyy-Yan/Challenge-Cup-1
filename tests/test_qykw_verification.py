"""Security and determinism tests for qykw patch verification."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from tools.qykw.change import (
    ChangeKind,
    ChangeRequest,
    CommandResult,
    FileDigest,
    FilePatch,
    PatchManifest,
    PreparedWorkspace,
    TextEdit,
    compute_manifest_digest,
)
from tools.qykw.domain import CommandMode, CommandName, CommandRequest, RunContext
from tools.qykw.patches import (
    apply_patch_manifest,
    compute_workspace_tree_digest,
    materialize_workspace,
)
from tools.qykw.sandbox import SandboxError
from tools.qykw.verification import (
    VerificationRuntimeMetadata,
    get_verification_profile,
    verify_change,
)
import tools.qykw.verification as verification_module


def digest(path: str, content: bytes, mode: str = "100644") -> FileDigest:
    return FileDigest(path, mode, hashlib.sha256(content).hexdigest())


def manifest(
    *patches: FilePatch, head: str = "a" * 40, profile: str = "full"
) -> PatchManifest:
    provisional = PatchManifest(
        schema_version=1,
        run_id="QY-PR3-ABCD",
        source_repository="owner/source",
        target_repository="owner/target",
        source_pr_number=3,
        source_head_sha=head,
        target_base_sha="b" * 40,
        target_base_ref="main",
        verification_profile=profile,
        files=tuple(patches),
        digest="",
    )
    return replace(provisional, digest=compute_manifest_digest(provisional))


class WorkspaceFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.destination = self.root / "workspace"

    def write(self, path: str, content: bytes) -> None:
        target = self.source / Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    def prepare(self, files: tuple[FileDigest, ...]) -> PreparedWorkspace:
        return materialize_workspace(
            self.source,
            source_head_sha="a" * 40,
            tracked_files=files,
            destination=self.destination,
        )


class TestPatchApplication(WorkspaceFixture):
    def test_materializes_exact_utf8_tracked_tree_without_git_metadata(self) -> None:
        self.write("src/app.py", b"value = 1\n")
        (self.source / ".git").mkdir()
        (self.source / ".git" / "config").write_text("secret", encoding="utf-8")
        files = (digest("src/app.py", b"value = 1\n"),)

        workspace = self.prepare(files)

        self.assertEqual(files, workspace.source_files)
        self.assertEqual(b"value = 1\n", (workspace.root / "src/app.py").read_bytes())
        self.assertFalse((workspace.root / ".git").exists())

    def test_ignores_git_pointer_file_and_rejects_copy_verification_mismatch(self) -> None:
        self.write("a.py", b"a\n")
        (self.source / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
        workspace = self.prepare((digest("a.py", b"a\n"),))
        self.assertFalse((workspace.root / ".git").exists())

        second_source = self.root / "copy-source"
        second_source.mkdir()
        (second_source / "a.py").write_bytes(b"a\n")
        with mock.patch("tools.qykw.patches._digest_tree", return_value=()):
            with self.assertRaisesRegex(ValueError, "workspace_copy_mismatch"):
                materialize_workspace(
                    second_source,
                    source_head_sha="a" * 40,
                    tracked_files=(digest("a.py", b"a\n"),),
                    destination=self.root / "copy-out",
                )

    def test_rejects_incomplete_or_forged_tracked_file_list(self) -> None:
        self.write("a.py", b"a\n")
        self.write("b.py", b"b\n")
        with self.assertRaisesRegex(ValueError, "source_file_list_mismatch"):
            self.prepare((digest("a.py", b"a\n"),))

    def test_rejects_wrong_source_digest_and_preexisting_destination(self) -> None:
        self.write("a.py", b"actual\n")
        with self.assertRaisesRegex(ValueError, "source_digest_mismatch"):
            self.prepare((digest("a.py", b"claimed\n"),))
        self.destination.mkdir()
        with self.assertRaisesRegex(ValueError, "destination_must_not_exist"):
            self.prepare((digest("a.py", b"actual\n"),))

    def test_rejects_invalid_head_root_and_destination_parent(self) -> None:
        self.write("a.py", b"a\n")
        files = (digest("a.py", b"a\n"),)
        for head in ("", "A" * 40, "z" * 40, 1):
            with self.subTest(head=head):
                with self.assertRaisesRegex(ValueError, "invalid_source_head_sha"):
                    materialize_workspace(
                        self.source,
                        source_head_sha=head,  # type: ignore[arg-type]
                        tracked_files=files,
                        destination=self.root / f"bad-head-{str(head)[:2]}",
                    )
        with self.assertRaisesRegex(ValueError, "invalid_source_root"):
            materialize_workspace(
                self.root / "missing",
                source_head_sha="a" * 40,
                tracked_files=files,
                destination=self.root / "unused",
            )
        source_file = self.root / "not-a-directory"
        source_file.write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid_source_root"):
            materialize_workspace(
                source_file,
                source_head_sha="a" * 40,
                tracked_files=(),
                destination=self.root / "unused-file-root",
            )
        with self.assertRaisesRegex(ValueError, "invalid_destination_parent"):
            materialize_workspace(
                self.source,
                source_head_sha="a" * 40,
                tracked_files=files,
                destination=self.root / "missing-parent" / "unused",
            )

    def test_rejects_duplicate_case_collision_and_unknown_mode(self) -> None:
        cases = (
            ((digest("A.py", b"a"), digest("a.py", b"a")), "path_collision"),
            ((digest("a.py", b"a", "120000"),), "unsupported_file_mode"),
        )
        self.write("a.py", b"a")
        for files, error in cases:
            with self.subTest(error=error):
                with self.assertRaisesRegex(ValueError, error):
                    materialize_workspace(
                        self.source,
                        source_head_sha="a" * 40,
                        tracked_files=files,
                        destination=self.root / error,
                    )

    def test_materializes_large_binary_and_executable_sentinels_as_raw_bytes(self) -> None:
        app = b"old\n"
        large = b"x" * (300 * 1024) + b"\x00\xfftail"
        pdf = b"%PDF-1.7\r\n\x00\xffbinary-object\r\n%%EOF"
        executable = b"#!/bin/sh\necho safe\n"
        self.write("app.py", app)
        self.write("assets/large.bin", large)
        self.write("docs/reference.pdf", pdf)
        self.write("scripts/check.sh", executable)
        files = (
            digest("app.py", app),
            digest("assets/large.bin", large),
            digest("docs/reference.pdf", pdf),
            digest("scripts/check.sh", executable, "100755"),
        )

        workspace = self.prepare(files)

        self.assertEqual(large, (workspace.root / "assets/large.bin").read_bytes())
        self.assertEqual(pdf, (workspace.root / "docs/reference.pdf").read_bytes())
        self.assertEqual(files, workspace.source_files)
        if os.name != "nt":
            self.assertEqual(0o644, stat.S_IMODE((workspace.root / "app.py").stat().st_mode))
            self.assertEqual(
                0o755, stat.S_IMODE((workspace.root / "scripts/check.sh").stat().st_mode)
            )

        patch = FilePatch(
            "app.py",
            hashlib.sha256(app).hexdigest(),
            False,
            (TextEdit("old", "new"),),
        )
        applied = apply_patch_manifest(manifest(patch), workspace)
        expected_files = (
            digest("app.py", b"new\n"),
            digest("assets/large.bin", large),
            digest("docs/reference.pdf", pdf),
            digest("scripts/check.sh", executable, "100755"),
        )
        self.assertEqual(
            compute_workspace_tree_digest(expected_files), applied.workspace_tree_digest
        )
        if os.name != "nt":
            self.assertEqual(0o644, stat.S_IMODE((workspace.root / "app.py").stat().st_mode))

    def test_binary_file_can_be_hashed_but_cannot_be_a_text_patch_target(self) -> None:
        binary = b"\x00\xffbinary"
        self.write("asset.bin", binary)
        workspace = self.prepare((digest("asset.bin", binary),))
        patch = FilePatch(
            "asset.bin",
            hashlib.sha256(binary).hexdigest(),
            False,
            (TextEdit("binary", "changed"),),
        )
        with self.assertRaisesRegex(ValueError, "binary_workspace_file"):
            apply_patch_manifest(manifest(patch), workspace)

    def test_non_utf8_large_or_executable_blob_cannot_be_a_patch_target(self) -> None:
        cases = (
            ("invalid.bin", b"\xff", "100644", "non_utf8_workspace_file"),
            (
                "large.txt",
                b"x" * (256 * 1024 + 1),
                "100644",
                "patch_file_too_large",
            ),
            ("run.sh", b"#!/bin/sh\n", "100755", "patch_target_mode_forbidden"),
        )
        for index, (path, content, mode, error) in enumerate(cases):
            with self.subTest(error=error):
                source = self.root / f"target-source-{index}"
                source.mkdir()
                (source / path).write_bytes(content)
                workspace = materialize_workspace(
                    source,
                    source_head_sha="a" * 40,
                    tracked_files=(digest(path, content, mode),),
                    destination=self.root / f"target-workspace-{index}",
                )
                patch = FilePatch(
                    path,
                    hashlib.sha256(content).hexdigest(),
                    False,
                    (TextEdit("x", "y"),),
                )
                with self.assertRaisesRegex(ValueError, error):
                    apply_patch_manifest(manifest(patch), workspace)

    def test_workspace_rejects_new_git_metadata_and_hashes_hidden_sentinel(self) -> None:
        app = b"old\n"
        hidden = b"\x00hidden\xff"
        self.write("app.py", app)
        self.write(".hidden-sentinel", hidden)
        files = (digest("app.py", app), digest(".hidden-sentinel", hidden))
        workspace = self.prepare(files)
        patch = FilePatch(
            "app.py",
            hashlib.sha256(app).hexdigest(),
            False,
            (TextEdit("old", "new"),),
        )
        applied = apply_patch_manifest(manifest(patch), workspace)
        expected = (digest("app.py", b"new\n"), digest(".hidden-sentinel", hidden))
        self.assertEqual(
            compute_workspace_tree_digest(expected), applied.workspace_tree_digest
        )

        second_source = self.root / "git-source"
        second_source.mkdir()
        (second_source / "app.py").write_bytes(app)
        second_workspace = materialize_workspace(
            second_source,
            source_head_sha="a" * 40,
            tracked_files=(digest("app.py", app),),
            destination=self.root / "git-workspace",
        )
        (second_workspace.root / ".git").mkdir()
        (second_workspace.root / ".git/index").write_bytes(b"forged")
        with self.assertRaisesRegex(ValueError, "git_metadata_forbidden"):
            apply_patch_manifest(manifest(patch), second_workspace)

    def test_registry_snapshot_is_not_aliased_to_returned_file_digest(self) -> None:
        content = b"old\n"
        self.write("app.py", content)
        workspace = self.prepare((digest("app.py", content),))
        returned_digest = workspace.source_files[0]
        object.__setattr__(returned_digest, "sha256", "0" * 64)
        patch = FilePatch(
            "app.py",
            hashlib.sha256(content).hexdigest(),
            False,
            (TextEdit("old", "new"),),
        )
        with self.assertRaisesRegex(ValueError, "untrusted_workspace"):
            apply_patch_manifest(manifest(patch), workspace)

    def test_rejects_source_symlink_and_destination_collision(self) -> None:
        self.write("real.py", b"ok\n")
        try:
            os.symlink(self.source / "real.py", self.source / "link.py")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        with self.assertRaisesRegex(ValueError, "source_symlink_forbidden"):
            self.prepare(
                (
                    digest("real.py", b"ok\n"),
                    digest("link.py", b"ok\n"),
                )
            )

    def test_rejects_destination_parent_symlink(self) -> None:
        self.write("a.py", b"a\n")
        real_parent = self.root / "real-parent"
        real_parent.mkdir()
        linked_parent = self.root / "linked-parent"
        try:
            os.symlink(real_parent, linked_parent, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlinks unavailable")
        with self.assertRaisesRegex(ValueError, "unsafe_destination_parent"):
            materialize_workspace(
                self.source,
                source_head_sha="a" * 40,
                tracked_files=(digest("a.py", b"a\n"),),
                destination=linked_parent / "workspace",
            )

    def test_applies_exact_edits_preserves_newlines_and_hashes_full_workspace(self) -> None:
        original = b"first\r\nold\r\nlast\r\n"
        sentinel = b"unchanged\n"
        self.write("app.py", original)
        self.write("sentinel.txt", sentinel)
        files = (
            digest("app.py", original),
            digest("sentinel.txt", sentinel),
        )
        workspace = self.prepare(files)
        patch = FilePatch(
            "app.py",
            hashlib.sha256(original).hexdigest(),
            False,
            (TextEdit("old\r\n", "new\r\n"),),
        )

        applied = apply_patch_manifest(manifest(patch), workspace)

        expected = b"first\r\nnew\r\nlast\r\n"
        self.assertEqual(expected, (workspace.root / "app.py").read_bytes())
        self.assertEqual((digest("app.py", expected),), applied.files)
        self.assertEqual(
            compute_workspace_tree_digest(applied.files), applied.output_tree_digest
        )
        full = (digest("app.py", expected), digest("sentinel.txt", sentinel))
        self.assertEqual(compute_workspace_tree_digest(full), applied.workspace_tree_digest)

        prior = applied.workspace_tree_digest
        changed_sentinel = b"changed\n"
        full_changed = (digest("app.py", expected), digest("sentinel.txt", changed_sentinel))
        self.assertNotEqual(prior, compute_workspace_tree_digest(full_changed))

    def test_rejects_wrong_head_manifest_digest_base_hash_and_forged_workspace(self) -> None:
        content = b"old\n"
        self.write("app.py", content)
        files = (digest("app.py", content),)
        workspace = self.prepare(files)
        patch = FilePatch("app.py", "0" * 64, False, (TextEdit("old", "new"),))
        with self.assertRaisesRegex(ValueError, "base_digest_mismatch"):
            apply_patch_manifest(manifest(patch), workspace)

        good_patch = replace(patch, base_sha256=hashlib.sha256(content).hexdigest())
        with self.assertRaisesRegex(ValueError, "source_head_mismatch"):
            apply_patch_manifest(manifest(good_patch, head="c" * 40), workspace)
        with self.assertRaisesRegex(ValueError, "manifest_digest_mismatch"):
            apply_patch_manifest(replace(manifest(good_patch), digest="0" * 64), workspace)
        forged = PreparedWorkspace(workspace.root, workspace.source_head_sha, files)
        with self.assertRaisesRegex(ValueError, "untrusted_workspace"):
            apply_patch_manifest(manifest(good_patch), forged)

    def test_rejects_zero_or_multiple_matches_and_new_file_conflict(self) -> None:
        content = b"same same\n"
        self.write("app.py", content)
        self.write("existing.py", b"exists\n")
        files = (digest("app.py", content), digest("existing.py", b"exists\n"))
        workspace = self.prepare(files)
        for before in ("missing", "same"):
            with self.subTest(before=before):
                patch = FilePatch(
                    "app.py",
                    hashlib.sha256(content).hexdigest(),
                    False,
                    (TextEdit(before, "new"),),
                )
                with self.assertRaisesRegex(ValueError, "ambiguous_edit"):
                    apply_patch_manifest(manifest(patch), workspace)

        create = FilePatch(
            "existing.py", None, True, (TextEdit("", "created\n"),)
        )
        with self.assertRaisesRegex(ValueError, "create_path_exists"):
            apply_patch_manifest(manifest(create), workspace)

    def test_creates_nested_text_file_and_rejects_policy_forbidden_target(self) -> None:
        content = b"old\n"
        self.write("app.py", content)
        workspace = self.prepare((digest("app.py", content),))
        create = FilePatch(
            "new/deep/file.py", None, True, (TextEdit("", "created\n"),)
        )
        applied = apply_patch_manifest(manifest(create), workspace)
        self.assertEqual(b"created\n", (workspace.root / "new/deep/file.py").read_bytes())
        self.assertEqual(("new/deep/file.py",), tuple(item.path for item in applied.files))
        self.assertEqual("100644", applied.files[0].mode)
        if os.name != "nt":
            self.assertEqual(
                0o644,
                stat.S_IMODE((workspace.root / "new/deep/file.py").stat().st_mode),
            )

        other_source = self.root / "policy-source"
        other_source.mkdir()
        (other_source / "app.py").write_bytes(content)
        other_workspace = materialize_workspace(
            other_source,
            source_head_sha="a" * 40,
            tracked_files=(digest("app.py", content),),
            destination=self.root / "policy-workspace",
        )
        forbidden = FilePatch(
            ".github/workflows/escape.yml",
            None,
            True,
            (TextEdit("", "name: unsafe\n"),),
        )
        with self.assertRaisesRegex(ValueError, "sensitive_path_forbidden"):
            apply_patch_manifest(manifest(forbidden), other_workspace)

    def test_rejects_untrusted_verification_profile_even_with_valid_digest(self) -> None:
        content = b"old\n"
        self.write("app.py", content)
        workspace = self.prepare((digest("app.py", content),))
        patch = FilePatch(
            "app.py",
            hashlib.sha256(content).hexdigest(),
            False,
            (TextEdit("old", "new"),),
        )
        invalid = replace(manifest(patch), verification_profile="shell")
        invalid = replace(invalid, digest=compute_manifest_digest(invalid))
        with self.assertRaisesRegex(ValueError, "unknown_verification_profile"):
            apply_patch_manifest(invalid, workspace)

    def test_rejects_manifest_shape_duplicate_and_unknown_source(self) -> None:
        content = b"old\n"
        self.write("app.py", content)
        workspace = self.prepare((digest("app.py", content),))
        valid_patch = FilePatch(
            "app.py",
            hashlib.sha256(content).hexdigest(),
            False,
            (TextEdit("old", "new"),),
        )
        wrong_schema = replace(manifest(valid_patch), schema_version=2)
        wrong_schema = replace(wrong_schema, digest=compute_manifest_digest(wrong_schema))
        with self.assertRaisesRegex(ValueError, "unsupported_manifest_schema"):
            apply_patch_manifest(wrong_schema, workspace)

        empty = manifest()
        with self.assertRaisesRegex(ValueError, "empty_patch_manifest"):
            apply_patch_manifest(empty, workspace)
        duplicate = manifest(valid_patch, valid_patch)
        with self.assertRaisesRegex(ValueError, "duplicate_patch_path"):
            apply_patch_manifest(duplicate, workspace)
        no_edits = manifest(replace(valid_patch, edits=()))
        with self.assertRaisesRegex(ValueError, "invalid_edit_count"):
            apply_patch_manifest(no_edits, workspace)
        unknown = FilePatch(
            "unknown.py", "0" * 64, False, (TextEdit("old", "new"),)
        )
        with self.assertRaisesRegex(ValueError, "unknown_source_path"):
            apply_patch_manifest(manifest(unknown), workspace)
        invalid_edit = replace(valid_patch, edits=(TextEdit("old", "old"),))
        with self.assertRaisesRegex(ValueError, "invalid_text_edit"):
            apply_patch_manifest(manifest(invalid_edit), workspace)
        with self.assertRaisesRegex(ValueError, "untrusted_workspace"):
            apply_patch_manifest(manifest(valid_patch), object())  # type: ignore[arg-type]

    def test_rejects_invalid_create_contract_and_unsafe_output(self) -> None:
        content = b"old\n"
        self.write("app.py", content)
        workspace = self.prepare((digest("app.py", content),))
        cases = (
            (
                FilePatch("new.py", "0" * 64, True, (TextEdit("", "new\n"),)),
                "new_file_base_digest_forbidden",
            ),
            (
                FilePatch("new.py", None, True, (TextEdit("x", "new\n"),)),
                "invalid_new_file_edit",
            ),
            (
                FilePatch("new.py", None, True, (TextEdit("", ""),)),
                "empty_file_forbidden",
            ),
            (
                FilePatch("new.py", None, True, (TextEdit("", "bad\x00file"),)),
                "binary_content_forbidden",
            ),
            (
                FilePatch(
                    "new.py", None, True, (TextEdit("", "x" * (256 * 1024 + 1)),)
                ),
                "change_file_too_large",
            ),
        )
        for patch, error in cases:
            with self.subTest(error=error):
                with self.assertRaisesRegex(ValueError, error):
                    apply_patch_manifest(manifest(patch), workspace)

    def test_rejects_added_or_missing_workspace_file_before_replay(self) -> None:
        content = b"old\n"
        self.write("app.py", content)
        files = (digest("app.py", content),)
        workspace = self.prepare(files)
        (workspace.root / "extra.py").write_text("extra\n", encoding="utf-8")
        patch = FilePatch(
            "app.py",
            hashlib.sha256(content).hexdigest(),
            False,
            (TextEdit("old", "new"),),
        )
        with self.assertRaisesRegex(ValueError, "workspace_file_list_changed"):
            apply_patch_manifest(manifest(patch), workspace)

    def test_rejects_invalid_digest_entries_and_workspace_paths(self) -> None:
        valid = digest("a.py", b"a")
        with self.assertRaisesRegex(TypeError, "tracked_files_must_be_tuple"):
            compute_workspace_tree_digest([valid])  # type: ignore[arg-type]
        with self.assertRaisesRegex(TypeError, "invalid_file_digest"):
            compute_workspace_tree_digest((object(),))  # type: ignore[arg-type]
        for item, error in (
            (replace(valid, sha256="A" * 64), "invalid_file_digest"),
            (replace(valid, path="/a.py"), "absolute_path_forbidden"),
            (replace(valid, path="a/../b.py"), "path_traversal_forbidden"),
            (replace(valid, path="nested/.git/config"), "git_metadata_forbidden"),
            (replace(valid, path="e\u0301.py"), "noncanonical_unicode_path"),
            (replace(valid, path="a\\b.py"), "invalid_change_path"),
            (replace(valid, path="x" * 256), "path_component_too_long"),
        ):
            with self.subTest(error=error):
                with self.assertRaisesRegex(ValueError, error):
                    compute_workspace_tree_digest((item,))

    def test_rejects_resource_exhaustion_or_unstable_source_read(self) -> None:
        oversized_source = self.root / "oversized"
        oversized_source.mkdir()
        oversized = b"12345"
        (oversized_source / "large.txt").write_bytes(oversized)
        with mock.patch("tools.qykw.patches._MAX_SOURCE_FILE_BYTES", 4):
            with self.assertRaisesRegex(ValueError, "source_file_too_large"):
                materialize_workspace(
                    oversized_source,
                    source_head_sha="a" * 40,
                    tracked_files=(digest("large.txt", oversized),),
                    destination=self.root / "oversized-out",
                )
        with mock.patch("tools.qykw.patches._MAX_WORKSPACE_BYTES", 4):
            with self.assertRaisesRegex(ValueError, "workspace_too_large"):
                materialize_workspace(
                    oversized_source,
                    source_head_sha="a" * 40,
                    tracked_files=(digest("large.txt", oversized),),
                    destination=self.root / "workspace-limit-out",
                )

        stable_source = self.root / "unstable"
        stable_source.mkdir()
        (stable_source / "a.py").write_bytes(b"a\n")
        with mock.patch("tools.qykw.patches._same_file", return_value=False):
            with self.assertRaisesRegex(ValueError, "source_file_changed"):
                materialize_workspace(
                    stable_source,
                    source_head_sha="a" * 40,
                    tracked_files=(digest("a.py", b"a\n"),),
                    destination=self.root / "unstable-out",
                )

    def test_rejects_tampered_workspace_identity_and_non_directory_parent(self) -> None:
        self.write("parent", b"plain file\n")
        files = (digest("parent", b"plain file\n"),)
        workspace = self.prepare(files)
        original_head = workspace.source_head_sha
        object.__setattr__(workspace, "source_head_sha", "b" * 40)
        create = FilePatch(
            "new.py", None, True, (TextEdit("", "created\n"),)
        )
        with self.assertRaisesRegex(ValueError, "untrusted_workspace"):
            apply_patch_manifest(manifest(create), workspace)
        object.__setattr__(workspace, "source_head_sha", original_head)

        nested = FilePatch(
            "parent/file.py", None, True, (TextEdit("", "created\n"),)
        )
        with self.assertRaisesRegex(ValueError, "workspace_parent_not_directory"):
            apply_patch_manifest(manifest(nested), workspace)

    def test_detects_write_and_final_tree_races(self) -> None:
        content = b"old\n"
        sentinel = b"sentinel\n"
        self.write("app.py", content)
        self.write("sentinel.txt", sentinel)
        files = (digest("app.py", content), digest("sentinel.txt", sentinel))
        patch = FilePatch(
            "app.py",
            hashlib.sha256(content).hexdigest(),
            False,
            (TextEdit("old", "new"),),
        )

        workspace = self.prepare(files)
        real_replace = __import__(
            "tools.qykw.patches", fromlist=["_atomic_replace"]
        )._atomic_replace

        def corrupt_write(path: Path, output: bytes, root: Path, *, expected: bytes) -> None:
            real_replace(path, output, root, expected=expected)
            path.write_bytes(b"corrupt\n")

        with mock.patch("tools.qykw.patches._atomic_replace", side_effect=corrupt_write):
            with self.assertRaisesRegex(ValueError, "workspace_write_mismatch"):
                apply_patch_manifest(manifest(patch), workspace)

        second_source = self.root / "race-source"
        second_source.mkdir()
        (second_source / "app.py").write_bytes(content)
        (second_source / "sentinel.txt").write_bytes(sentinel)
        workspace2 = materialize_workspace(
            second_source,
            source_head_sha="a" * 40,
            tracked_files=files,
            destination=self.root / "race-workspace",
        )

        def mutate_sentinel(
            path: Path, output: bytes, root: Path, *, expected: bytes
        ) -> None:
            real_replace(path, output, root, expected=expected)
            (root / "sentinel.txt").write_bytes(b"raced\n")

        with mock.patch("tools.qykw.patches._atomic_replace", side_effect=mutate_sentinel):
            with self.assertRaisesRegex(ValueError, "workspace_file_changed"):
                apply_patch_manifest(manifest(patch), workspace2)

        third_source = self.root / "path-race-source"
        third_source.mkdir()
        (third_source / "app.py").write_bytes(content)
        workspace3 = materialize_workspace(
            third_source,
            source_head_sha="a" * 40,
            tracked_files=(files[0],),
            destination=self.root / "path-race-workspace",
        )
        with mock.patch(
            "tools.qykw.patches._scan_tree_paths",
            side_effect=(("app.py",), ("app.py", "extra.py")),
        ):
            with self.assertRaisesRegex(ValueError, "workspace_file_list_changed"):
                apply_patch_manifest(manifest(patch), workspace3)

    def test_materialization_fails_closed_for_copy_phase_races(self) -> None:
        def fresh_source(name: str, content: bytes = b"actual\n") -> Path:
            source = self.root / name
            source.mkdir()
            (source / "a.py").write_bytes(content)
            return source

        wrong_source = fresh_source("copy-digest-source")
        claimed = b"claimed\n"
        claimed_digest = digest("a.py", claimed)
        with mock.patch(
            "tools.qykw.patches._hash_stable_file",
            return_value=(claimed_digest.sha256, len(claimed)),
        ):
            with self.assertRaisesRegex(ValueError, "source_digest_mismatch"):
                materialize_workspace(
                    wrong_source,
                    source_head_sha="a" * 40,
                    tracked_files=(claimed_digest,),
                    destination=self.root / "copy-digest-out",
                )

        unstable_source = fresh_source("copy-unstable-source")
        with mock.patch(
            "tools.qykw.patches._same_file", side_effect=(True, False)
        ):
            with self.assertRaisesRegex(ValueError, "source_file_changed"):
                materialize_workspace(
                    unstable_source,
                    source_head_sha="a" * 40,
                    tracked_files=(digest("a.py", b"actual\n"),),
                    destination=self.root / "copy-unstable-out",
                )

        large_source = fresh_source("copy-limit-source", b"12345")
        expected = digest("a.py", b"12345")
        with mock.patch(
            "tools.qykw.patches._hash_stable_file",
            return_value=(expected.sha256, 5),
        ), mock.patch("tools.qykw.patches._MAX_SOURCE_FILE_BYTES", 4):
            with self.assertRaisesRegex(ValueError, "source_file_too_large"):
                materialize_workspace(
                    large_source,
                    source_head_sha="a" * 40,
                    tracked_files=(expected,),
                    destination=self.root / "copy-limit-out",
                )

        collision_source = fresh_source("copy-collision-source")
        with mock.patch(
            "tools.qykw.patches.os.link", side_effect=FileExistsError
        ):
            with self.assertRaisesRegex(ValueError, "create_path_exists"):
                materialize_workspace(
                    collision_source,
                    source_head_sha="a" * 40,
                    tracked_files=(digest("a.py", b"actual\n"),),
                    destination=self.root / "copy-collision-out",
                )

        growth_source = fresh_source("copy-growth-source")
        expected_growth = digest("a.py", b"actual\n")
        with mock.patch(
            "tools.qykw.patches._hash_stable_file",
            return_value=(expected_growth.sha256, 4),
        ), mock.patch(
            "tools.qykw.patches._copy_tracked_file", return_value=5
        ), mock.patch("tools.qykw.patches._MAX_WORKSPACE_BYTES", 4):
            with self.assertRaisesRegex(ValueError, "workspace_too_large"):
                materialize_workspace(
                    growth_source,
                    source_head_sha="a" * 40,
                    tracked_files=(expected_growth,),
                    destination=self.root / "copy-growth-out",
                )

    def test_creation_races_and_mode_checks_fail_closed(self) -> None:
        content = b"old\n"
        self.write("app.py", content)
        files = (digest("app.py", content),)
        create = FilePatch("new.py", None, True, (TextEdit("", "new\n"),))

        workspace = self.prepare(files)

        def create_during_lookup(root: Path, relative: str) -> bool:
            (root / relative).write_bytes(b"raced\n")
            return False

        with mock.patch(
            "tools.qykw.patches._member_exists", side_effect=create_during_lookup
        ):
            with self.assertRaisesRegex(ValueError, "create_path_exists"):
                apply_patch_manifest(manifest(create), workspace)

        second_source = self.root / "atomic-source"
        second_source.mkdir()
        (second_source / "app.py").write_bytes(content)
        workspace2 = materialize_workspace(
            second_source,
            source_head_sha="a" * 40,
            tracked_files=files,
            destination=self.root / "atomic-workspace",
        )
        with mock.patch(
            "tools.qykw.patches.os.link", side_effect=FileExistsError
        ):
            with self.assertRaisesRegex(ValueError, "create_path_exists"):
                apply_patch_manifest(manifest(create), workspace2)

        patches_module = __import__("tools.qykw.patches", fromlist=["patches"])
        with self.assertRaisesRegex(ValueError, "unsupported_file_mode"):
            patches_module._set_and_verify_mode(self.root / "missing", "120000")
        fake_metadata = mock.Mock(st_mode=stat.S_IFREG | 0o600)
        with mock.patch("tools.qykw.patches.os.name", "posix"), mock.patch.object(
            Path, "lstat", return_value=fake_metadata
        ):
            with self.assertRaisesRegex(ValueError, "workspace_mode_mismatch"):
                patches_module._verify_actual_mode(self.root / "mode", "100644")
        with mock.patch("tools.qykw.patches.os.name", "posix"), mock.patch(
            "tools.qykw.patches.os.chmod", side_effect=OSError
        ):
            with self.assertRaisesRegex(ValueError, "workspace_mode_mismatch"):
                patches_module._set_and_verify_mode(self.root / "mode", "100644")

    def test_rejects_untrusted_path_symlink_and_tampering_before_write(self) -> None:
        content = b"old\n"
        self.write("dir/app.py", content)
        files = (digest("dir/app.py", content),)
        workspace = self.prepare(files)
        patch = FilePatch(
            "dir/app.py",
            hashlib.sha256(content).hexdigest(),
            False,
            (TextEdit("old", "new"),),
        )
        (workspace.root / "dir/app.py").write_bytes(b"tampered\n")
        with self.assertRaisesRegex(ValueError, "workspace_file_changed"):
            apply_patch_manifest(manifest(patch), workspace)

        # Re-materialize, then replace the trusted parent with a symlink.
        second = self.root / "second"
        second.mkdir()
        (second / "dir").mkdir()
        (second / "dir/app.py").write_bytes(content)
        workspace2 = materialize_workspace(
            second,
            source_head_sha="a" * 40,
            tracked_files=files,
            destination=self.root / "workspace2",
        )
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "app.py").write_bytes(content)
        try:
            (workspace2.root / "dir/app.py").unlink()
            (workspace2.root / "dir").rmdir()
            os.symlink(outside, workspace2.root / "dir", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlinks unavailable")
        with self.assertRaisesRegex(ValueError, "workspace_symlink_forbidden"):
            apply_patch_manifest(manifest(patch), workspace2)

    def test_fails_closed_if_file_changes_between_validation_and_write(self) -> None:
        content = b"old\n"
        self.write("app.py", content)
        files = (digest("app.py", content),)
        workspace = self.prepare(files)
        patch = FilePatch(
            "app.py",
            hashlib.sha256(content).hexdigest(),
            False,
            (TextEdit("old", "new"),),
        )
        real_read = __import__("tools.qykw.patches", fromlist=["_read_stable_bytes"])._read_stable_bytes
        calls = 0

        def racing_read(path: Path, *args: object, **kwargs: object) -> bytes:
            nonlocal calls
            result = real_read(path, *args, **kwargs)
            calls += 1
            if calls == 1:
                path.write_bytes(b"raced\n")
            return result

        with mock.patch("tools.qykw.patches._read_stable_bytes", side_effect=racing_read):
            with self.assertRaisesRegex(ValueError, "workspace_file_changed"):
                apply_patch_manifest(manifest(patch), workspace)
        self.assertEqual(b"raced\n", (workspace.root / "app.py").read_bytes())

    def test_digest_is_versioned_sorted_and_rejects_invalid_entries(self) -> None:
        left = digest("b.py", b"b")
        right = digest("a.py", b"a")
        self.assertEqual(
            compute_workspace_tree_digest((left, right)),
            compute_workspace_tree_digest((right, left)),
        )
        self.assertNotEqual(
            compute_workspace_tree_digest((right,)),
            hashlib.sha256(b"a").hexdigest(),
        )
        with self.assertRaisesRegex(ValueError, "duplicate_file_path"):
            compute_workspace_tree_digest((right, right))
        with self.assertRaisesRegex(ValueError, "unsupported_file_mode"):
            compute_workspace_tree_digest((replace(right, mode="120000"),))


class TestProfiles(unittest.TestCase):
    def test_backend_profile_uses_fixed_literal_argv(self) -> None:
        profile = get_verification_profile("backend")
        self.assertEqual("backend", profile.name)
        self.assertEqual(
            (
                (
                    "python",
                    "-X",
                    "pycache_prefix=/tmp/qykw-pyc",
                    "-m",
                    "compileall",
                    "-q",
                    "agents",
                    "core",
                    "evalkit",
                    "tools",
                    "cli.py",
                    "config.py",
                    "orchestrator.py",
                    "server.py",
                ),
                ("python", "-m", "unittest", "discover", "-s", "tests", "-v"),
            ),
            tuple(command.argv for command in profile.commands),
        )
        self.assertTrue(all(command.env == () for command in profile.commands))

    def test_frontend_and_full_profiles_use_only_trusted_commands(self) -> None:
        frontend = get_verification_profile("frontend")
        self.assertEqual(
            (
                ("node", "--check", "web/view-model.js"),
                ("node", "--check", "web/app.js"),
                ("node", "--test", "tests/frontend-ui.test.mjs"),
                (
                    "python",
                    "-m",
                    "unittest",
                    "tests.test_server.TestOnlineFrontend",
                    "-v",
                ),
            ),
            tuple(command.argv for command in frontend.commands),
        )
        removed_targets = {
            "build_showcase.py",
            "evalkit.snapshot",
            "tests.test_parity",
            "web/engine.js",
        }
        self.assertTrue(
            all(
                removed_targets.isdisjoint(command.argv)
                and not any(
                    target in argument
                    for target in removed_targets
                    for argument in command.argv
                )
                for command in get_verification_profile("full").commands
            )
        )

        full = get_verification_profile("full")
        expected = tuple(
            command.argv
            for command in get_verification_profile("backend").commands
            + frontend.commands
        ) + (("python", "/opt/qykw/verify_smoke.py", "/workspace", "8765"),)
        self.assertEqual(expected, tuple(command.argv for command in full.commands))
        self.assertEqual(len(expected), len(set(expected)))

    def test_profiles_are_immutable_and_unknown_or_injected_names_fail(self) -> None:
        profile = get_verification_profile("backend")
        with self.assertRaisesRegex(Exception, "cannot assign|FrozenInstanceError"):
            profile.name = "full"  # type: ignore[misc]
        for value in ("unknown", "backend; rm -rf /", "Backend", " backend", ""):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "unknown_verification_profile"):
                    get_verification_profile(value)


def _argv_digest(argv: tuple[str, ...]) -> str:
    import json

    encoded = json.dumps(argv, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(b"qykw-command-argv-v1\0" + encoded).hexdigest()


class _StateStore:
    def __init__(self, checkpoints: tuple[bool, ...] = ()) -> None:
        self.checkpoints = list(checkpoints)
        self.calls: list[tuple[int, str]] = []

    def is_cancel_requested(self, pr_number: int, run_id: str) -> bool:
        self.calls.append((pr_number, run_id))
        return self.checkpoints.pop(0) if self.checkpoints else False


class _Executor:
    def __init__(
        self,
        *,
        on_run=None,
        on_close=None,
        failure: Exception | None = None,
        workspace_read_only: bool = True,
        actual_image_digest: str = "sha256:" + "c" * 64,
    ) -> None:
        self.on_run = on_run
        self.on_close = on_close
        self.failure = failure
        self.calls: list[
            tuple[tuple[str, ...], Path, dict[str, str], int, int]
        ] = []
        self.close_calls = 0
        self.cleanup_error: Exception | None = None
        self.workspace_read_only = workspace_read_only
        self.actual_image_digest = actual_image_digest

    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: int,
        output_limit_bytes: int,
    ) -> CommandResult:
        self.calls.append(
            (argv, cwd, dict(env), timeout_seconds, output_limit_bytes)
        )
        if self.failure is not None:
            raise self.failure
        if self.on_run is not None:
            result = self.on_run(len(self.calls), cwd, argv)
            if result is not None:
                return result
        return CommandResult(
            name="untrusted-executor-name",
            argv_digest=_argv_digest(argv),
            exit_code=0,
            timed_out=False,
            duration_ms=7,
            output_digest=hashlib.sha256(b"test output").hexdigest(),
            output_excerpt="Ran 1 test\nOK\nsecret raw source must disappear",
        )

    def close(self) -> None:
        self.close_calls += 1
        if self.on_close is not None:
            self.on_close()
        if self.cleanup_error is not None:
            raise self.cleanup_error


class TestVerificationIntegration(WorkspaceFixture):
    def setUp(self) -> None:
        super().setUp()
        self.original = b"value = 1\n"
        self.changed = b"value = 2\n"
        self.write("app.py", self.original)
        self.workspace = self.prepare((digest("app.py", self.original),))
        patch = FilePatch(
            "app.py",
            hashlib.sha256(self.original).hexdigest(),
            False,
            (TextEdit("value = 1", "value = 2"),),
        )
        self.manifest = manifest(patch, profile="backend")
        context = RunContext(
            run_id=self.manifest.run_id,
            idempotency_key="issue_comment:99",
            repository_id=10,
            repository="owner/target",
            pr_number=self.manifest.source_pr_number,
            event_name="issue_comment",
            event_action="created",
            source_repository=self.manifest.source_repository,
            source_head_sha=self.manifest.source_head_sha,
            target_base_sha=self.manifest.target_base_sha,
            target_base_ref=self.manifest.target_base_ref,
            command=CommandRequest(CommandName.FIX, "update value", CommandMode.CHANGE),
            trigger_actor="xyh202131",
            trigger_comment_id=99,
        )
        self.request = ChangeRequest(
            context=context,
            kind=ChangeKind.FIX,
            instruction="update value",
            source_repository=self.manifest.source_repository,
            target_repository=self.manifest.target_repository,
            source_head_sha=self.manifest.source_head_sha,
            target_base_sha=self.manifest.target_base_sha,
            target_base_ref=self.manifest.target_base_ref,
            verification_profile=self.manifest.verification_profile,
        )
        self.runtime = VerificationRuntimeMetadata(
            workflow_run_id=123,
            image_digest="sha256:" + "c" * 64,
            timeout_seconds=60,
            output_limit_bytes=65536,
        )

    def verify(self, executor: _Executor, state: _StateStore | None = None):
        return verify_change(
            self.request,
            self.manifest,
            self.workspace,
            executor,
            state or _StateStore(),
            runtime=self.runtime,
        )

    def test_runs_fixed_profile_in_order_and_attests_trusted_tree(self) -> None:
        executor = _Executor()
        state = _StateStore()

        attestation = self.verify(executor, state)

        profile = get_verification_profile("backend")
        self.assertEqual(
            tuple(command.argv for command in profile.commands),
            tuple(call[0] for call in executor.calls),
        )
        self.assertTrue(
            all(
                cwd == self.workspace.root
                and env == {}
                and timeout == 60
                and limit == 65536
                for _, cwd, env, timeout, limit in executor.calls
            )
        )
        self.assertEqual(1, executor.close_calls)
        self.assertEqual(1 + 2 * len(profile.commands) + 1, len(state.calls))
        self.assertEqual(
            tuple(command.name for command in profile.commands),
            tuple(result.name for result in attestation.results),
        )
        self.assertEqual(
            tuple(_argv_digest(command.argv) for command in profile.commands),
            tuple(result.argv_digest for result in attestation.results),
        )
        self.assertTrue(attestation.success)
        self.assertFalse(attestation.canceled)
        self.assertEqual(123, attestation.workflow_run_id)
        self.assertEqual("sha256:" + "c" * 64, attestation.image_digest)
        self.assertEqual((digest("app.py", self.changed),), attestation.output_files)
        self.assertEqual(
            compute_workspace_tree_digest((digest("app.py", self.changed),)),
            attestation.workspace_tree_digest,
        )
        serialized = repr(attestation)
        self.assertNotIn("secret raw source", serialized)
        self.assertNotIn("value = 2", serialized)

    def test_nonzero_or_timed_out_result_never_attests_success(self) -> None:
        for exit_code, timed_out in ((2, False), (None, True)):
            with self.subTest(exit_code=exit_code, timed_out=timed_out):
                def fail_result(_count: int, _cwd: Path, argv: tuple[str, ...]):
                    return CommandResult(
                        "ignored",
                        _argv_digest(argv),
                        exit_code,
                        timed_out,
                        5,
                        hashlib.sha256(b"failure").hexdigest(),
                        "ValueError\nFAILED (errors=1)",
                    )

                executor = _Executor(on_run=fail_result)
                attestation = self.verify(executor)
                self.assertFalse(attestation.success)
                self.assertFalse(attestation.canceled)
                self.assertEqual(1, len(attestation.results))
                self.assertEqual(1, executor.close_calls)

                # Each subtest needs a pristine materialized workspace.
                if timed_out is False:
                    self.destination = self.root / "workspace-timeout"
                    self.workspace = self.prepare((digest("app.py", self.original),))

    def test_cancel_is_checked_before_patch_each_command_and_attestation(self) -> None:
        command_count = len(get_verification_profile("backend").commands)
        checkpoint_count = 1 + 2 * command_count + 1
        for cancel_at in range(checkpoint_count):
            with self.subTest(cancel_at=cancel_at):
                destination = self.root / f"cancel-{cancel_at}"
                workspace = materialize_workspace(
                    self.source,
                    source_head_sha="a" * 40,
                    tracked_files=(digest("app.py", self.original),),
                    destination=destination,
                )
                executor = _Executor()
                state = _StateStore((False,) * cancel_at + (True,))
                attestation = verify_change(
                    self.request,
                    self.manifest,
                    workspace,
                    executor,
                    state,
                    runtime=self.runtime,
                )
                self.assertFalse(attestation.success)
                self.assertTrue(attestation.canceled)
                self.assertEqual(1, executor.close_calls)
                expected_runs = min(command_count, cancel_at // 2)
                self.assertEqual(expected_runs, len(executor.calls))

    def test_rejects_tracked_drift_extra_files_and_git_metadata(self) -> None:
        def mutate_source(_count: int, cwd: Path, _argv: tuple[str, ...]):
            target = cwd / "app.py"
            target.chmod(0o644)
            target.write_text("candidate changed source\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "verification_workspace_changed"):
            self.verify(_Executor(on_run=mutate_source))

        cases = (
            ("extra.py", "verification_workspace_file_list_changed"),
            (".git/index", "verification_git_metadata_created"),
        )
        for index, (path, error) in enumerate(cases):
            with self.subTest(path=path):
                workspace = materialize_workspace(
                    self.source,
                    source_head_sha="a" * 40,
                    tracked_files=(digest("app.py", self.original),),
                    destination=self.root / f"malicious-{index}",
                )

                def create_file(_count: int, cwd: Path, _argv: tuple[str, ...]):
                    target = cwd / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("forged\n", encoding="utf-8")

                with self.assertRaisesRegex(ValueError, error):
                    verify_change(
                        self.request,
                        self.manifest,
                        workspace,
                        _Executor(on_run=create_file),
                        _StateStore(),
                        runtime=self.runtime,
                    )

    def test_executor_and_cleanup_failures_produce_no_attestation(self) -> None:
        executor = _Executor(
            failure=SandboxError("sandbox_output_limit_exceeded")
        )
        with self.assertRaisesRegex(SandboxError, "sandbox_output_limit_exceeded"):
            self.verify(executor)
        self.assertEqual(1, executor.close_calls)

        workspace = materialize_workspace(
            self.source,
            source_head_sha="a" * 40,
            tracked_files=(digest("app.py", self.original),),
            destination=self.root / "cleanup-failure",
        )
        executor = _Executor()
        executor.cleanup_error = SandboxError("sandbox_cleanup_unconfirmed")
        with self.assertRaisesRegex(SandboxError, "sandbox_cleanup_unconfirmed"):
            verify_change(
                self.request,
                self.manifest,
                workspace,
                executor,
                _StateStore(),
                runtime=self.runtime,
            )

    def test_rechecks_workspace_after_container_removal(self) -> None:
        def mutate_after_close() -> None:
            target = self.workspace.root / "app.py"
            target.chmod(0o644)
            target.write_text("late background write\n", encoding="utf-8")

        executor = _Executor(on_close=mutate_after_close)
        with self.assertRaisesRegex(ValueError, "verification_workspace_changed"):
            self.verify(executor)
        self.assertEqual(1, executor.close_calls)

    def test_requires_read_only_executor_and_exact_runtime_image(self) -> None:
        for executor in (
            _Executor(workspace_read_only=False),
            _Executor(actual_image_digest="sha256:" + "d" * 64),
        ):
            with self.subTest(executor=executor):
                with self.assertRaisesRegex(ValueError, "untrusted_verification_executor"):
                    self.verify(executor)
                self.assertEqual([], executor.calls)
                self.assertEqual(1, executor.close_calls)

        class MissingCapabilities:
            def __init__(self) -> None:
                self.calls = 0
                self.close_calls = 0

            def run(self, *args: object, **kwargs: object) -> CommandResult:
                self.calls += 1
                raise AssertionError("must not execute")

            def close(self) -> None:
                self.close_calls += 1

        missing = MissingCapabilities()
        with self.assertRaisesRegex(ValueError, "untrusted_verification_executor"):
            verify_change(
                self.request,
                self.manifest,
                self.workspace,
                missing,  # type: ignore[arg-type]
                _StateStore(),
                runtime=self.runtime,
            )
        self.assertEqual(0, missing.calls)
        self.assertEqual(1, missing.close_calls)

    def test_close_then_final_tree_check_then_cancel_decides_attestation(self) -> None:
        closed = False

        class CloseAwareState(_StateStore):
            def is_cancel_requested(self, pr_number: int, run_id: str) -> bool:
                self.calls.append((pr_number, run_id))
                return closed

        def mark_closed() -> None:
            nonlocal closed
            closed = True

        attestation = self.verify(_Executor(on_close=mark_closed), CloseAwareState())
        self.assertTrue(attestation.canceled)
        self.assertFalse(attestation.success)

    def test_attestation_excerpt_has_only_fixed_finite_status(self) -> None:
        sentinel = "8" * 10000

        def numeric_result(_count: int, _cwd: Path, argv: tuple[str, ...]):
            return CommandResult(
                "ignored",
                _argv_digest(argv),
                2,
                False,
                1,
                hashlib.sha256(b"numeric output").hexdigest(),
                f"Ran {sentinel} tests\nFAILED (errors={sentinel})",
            )

        attestation = self.verify(_Executor(on_run=numeric_result))
        self.assertEqual("failed", attestation.results[0].output_excerpt)
        self.assertNotRegex(attestation.results[0].output_excerpt, r"\d")
        self.assertNotIn(sentinel, repr(attestation))

    def test_workspace_hash_uses_one_descriptor_and_no_follow_when_available(self) -> None:
        expected = digest("app.py", self.original)
        real_open = os.open
        real_fstat = os.fstat
        opened: list[tuple[object, int]] = []
        fstat_fds: list[int] = []

        def recording_open(path: object, flags: int) -> int:
            opened.append((path, flags))
            return real_open(path, flags)

        def recording_fstat(fd: int):
            fstat_fds.append(fd)
            return real_fstat(fd)

        with mock.patch.object(
            verification_module.os, "open", side_effect=recording_open
        ), mock.patch.object(
            verification_module.os, "fstat", side_effect=recording_fstat
        ):
            actual = verification_module._hash_workspace_file(
                self.workspace.root / "app.py", expected
            )

        self.assertEqual(expected, actual)
        self.assertEqual(1, len(opened))
        self.assertEqual(2, len(fstat_fds))
        self.assertEqual(1, len(set(fstat_fds)))
        if hasattr(os, "O_NOFOLLOW"):
            self.assertTrue(opened[0][1] & os.O_NOFOLLOW)

    @unittest.skipUnless(os.name == "posix", "Linux permission contract")
    def test_linux_workspace_permissions_are_world_readable_and_not_writable(self) -> None:
        executor = _Executor()
        self.verify(executor)
        self.assertEqual(0o755, stat.S_IMODE(self.workspace.root.stat().st_mode))
        self.assertEqual(0o444, stat.S_IMODE((self.workspace.root / "app.py").stat().st_mode))

    def test_read_only_permission_plan_covers_root_directories_and_file_modes(self) -> None:
        files = (
            digest("plain.py", b"plain\n", "100644"),
            digest("bin/tool", b"#!/bin/sh\n", "100755"),
        )
        with mock.patch.object(verification_module.os, "name", "posix"), mock.patch.object(
            verification_module, "_fchmod_no_follow"
        ) as chmod:
            verification_module._prepare_read_only_workspace(self.workspace.root, files)

        self.assertIn(
            mock.call(self.workspace.root / "plain.py", 0o444, directory=False),
            chmod.call_args_list,
        )
        self.assertIn(
            mock.call(self.workspace.root / "bin/tool", 0o555, directory=False),
            chmod.call_args_list,
        )
        self.assertIn(
            mock.call(self.workspace.root / "bin", 0o755, directory=True),
            chmod.call_args_list,
        )
        self.assertIn(
            mock.call(self.workspace.root, 0o755, directory=True),
            chmod.call_args_list,
        )

    def test_permission_update_uses_no_follow_fd_and_fails_closed(self) -> None:
        regular_before = mock.Mock(st_mode=stat.S_IFREG | 0o600)
        regular_after = mock.Mock(st_mode=stat.S_IFREG | 0o444)
        with mock.patch.object(verification_module.os, "O_NOFOLLOW", 0x100, create=True), mock.patch.object(
            verification_module.os, "O_CLOEXEC", 0x200, create=True
        ), mock.patch.object(
            verification_module.os, "open", return_value=17
        ) as opened, mock.patch.object(
            verification_module.os, "fstat", side_effect=(regular_before, regular_after)
        ), mock.patch.object(
            verification_module.os, "fchmod", create=True
        ) as chmod, mock.patch.object(
            verification_module.os, "close"
        ) as closed:
            verification_module._fchmod_no_follow(
                self.workspace.root / "app.py", 0o444, directory=False
            )
        self.assertTrue(opened.call_args.args[1] & 0x100)
        chmod.assert_called_once_with(17, 0o444)
        closed.assert_called_once_with(17)

        with mock.patch.object(
            verification_module.os, "open", side_effect=OSError
        ):
            with self.assertRaisesRegex(
                ValueError, "verification_workspace_permission_failed"
            ):
                verification_module._fchmod_no_follow(
                    self.workspace.root, 0o755, directory=True
                )

        wrong_kind = mock.Mock(st_mode=stat.S_IFDIR | 0o755)
        with mock.patch.object(
            verification_module.os, "open", return_value=19
        ), mock.patch.object(
            verification_module.os, "fstat", return_value=wrong_kind
        ), mock.patch.object(
            verification_module.os, "close"
        ) as closed:
            with self.assertRaisesRegex(
                ValueError, "verification_workspace_permission_failed"
            ):
                verification_module._fchmod_no_follow(
                    self.workspace.root / "app.py", 0o444, directory=False
                )
        closed.assert_called_once_with(19)

        wrong_mode = mock.Mock(st_mode=stat.S_IFREG | 0o600)
        with mock.patch.object(
            verification_module.os, "open", return_value=23
        ), mock.patch.object(
            verification_module.os, "fstat", side_effect=(regular_before, wrong_mode)
        ), mock.patch.object(
            verification_module.os, "fchmod", create=True
        ), mock.patch.object(
            verification_module.os, "close"
        ):
            with self.assertRaisesRegex(
                ValueError, "verification_workspace_permission_failed"
            ):
                verification_module._fchmod_no_follow(
                    self.workspace.root / "app.py", 0o444, directory=False
                )

    def test_rejects_untrusted_runtime_and_request_manifest_mismatch(self) -> None:
        for kwargs in (
            {"workflow_run_id": True},
            {"workflow_run_id": 0},
            {"image_digest": "repo@sha256:" + "c" * 64},
            {"image_digest": "sha256:" + "C" * 64},
            {"timeout_seconds": 0},
            {"timeout_seconds": 901},
            {"output_limit_bytes": 0},
            {"output_limit_bytes": 1024 * 1024 + 1},
        ):
            with self.subTest(kwargs=kwargs):
                values = {
                    "workflow_run_id": 123,
                    "image_digest": "sha256:" + "c" * 64,
                    "timeout_seconds": 60,
                    "output_limit_bytes": 65536,
                    **kwargs,
                }
                with self.assertRaisesRegex(ValueError, "invalid_verification_runtime"):
                    VerificationRuntimeMetadata(**values)

        mismatched = replace(self.request, target_base_sha="d" * 40)
        with self.assertRaisesRegex(ValueError, "verification_request_mismatch"):
            verify_change(
                mismatched,
                self.manifest,
                self.workspace,
                _Executor(),
                _StateStore(),
                runtime=self.runtime,
            )

    def test_rejects_untrusted_cancel_and_executor_result_metadata(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid_cancel_state"):
            self.verify(_Executor(), _StateStore(("yes",)))  # type: ignore[arg-type]

        workspace = materialize_workspace(
            self.source,
            source_head_sha="a" * 40,
            tracked_files=(digest("app.py", self.original),),
            destination=self.root / "invalid-command-result",
        )

        def forged_result(_count: int, _cwd: Path, _argv: tuple[str, ...]):
            return CommandResult(
                "forged-name",
                "0" * 64,
                0,
                False,
                1,
                hashlib.sha256(b"output").hexdigest(),
                "raw source",
            )

        with self.assertRaisesRegex(ValueError, "invalid_command_result"):
            verify_change(
                self.request,
                self.manifest,
                workspace,
                _Executor(on_run=forged_result),
                _StateStore(),
                runtime=self.runtime,
            )


if __name__ == "__main__":
    unittest.main()
