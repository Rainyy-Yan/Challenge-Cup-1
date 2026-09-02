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
    FileDigest,
    FilePatch,
    PatchManifest,
    PreparedWorkspace,
    TextEdit,
    compute_manifest_digest,
)
from tools.qykw.patches import (
    apply_patch_manifest,
    compute_workspace_tree_digest,
    materialize_workspace,
)
from tools.qykw.verification import get_verification_profile


def digest(path: str, content: bytes, mode: str = "100644") -> FileDigest:
    return FileDigest(path, mode, hashlib.sha256(content).hexdigest())


def manifest(*patches: FilePatch, head: str = "a" * 40) -> PatchManifest:
    provisional = PatchManifest(
        schema_version=1,
        run_id="QY-PR3-ABCD",
        source_repository="owner/source",
        target_repository="owner/target",
        source_pr_number=3,
        source_head_sha=head,
        target_base_sha="b" * 40,
        target_base_ref="main",
        verification_profile="full",
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
                    "-m",
                    "compileall",
                    "-q",
                    "agents",
                    "core",
                    "evalkit",
                    "tools",
                    "build_showcase.py",
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
            ("node", "--check", "web/engine.js"), frontend.commands[0].argv
        )
        self.assertEqual(
            ("python", "-m", "evalkit.snapshot", "--out", "/tmp/qykw-snapshot.json"),
            frontend.commands[2].argv,
        )
        self.assertEqual("python", frontend.commands[3].argv[0])
        self.assertEqual("-c", frontend.commands[3].argv[1])
        self.assertIn("{'P-A', 'P-B', 'P-C'}", frontend.commands[3].argv[2])
        self.assertEqual(("python", "build_showcase.py"), frontend.commands[4].argv)

        full = get_verification_profile("full")
        expected = tuple(
            command.argv
            for command in get_verification_profile("backend").commands
            + frontend.commands
        ) + (("/opt/qykw/verify_smoke.py", "/workspace", "8765"),)
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


if __name__ == "__main__":
    unittest.main()
