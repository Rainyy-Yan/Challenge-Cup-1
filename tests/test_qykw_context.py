"""Behavioral tests for deterministic qykw review context planning."""

from __future__ import annotations

import unittest

from tools.qykw.domain import ChangedFile, ChangedLine, DiffSide, PullSnapshot
from tools.qykw.context import ContextError, build_context_plan, estimate_tokens, parse_hunks


def changed_file(
    path: str,
    *,
    patch: str | None = "@@ -1 +1 @@\n-old\n+new\n",
    previous_path: str | None = None,
    status: str = "modified",
    base_content: str | None = "old\n",
    head_content: str | None = "new\n",
    binary: bool = False,
    generated: bool = False,
    additions: int = 1,
    deletions: int = 1,
    base_mode: str | None = None,
    head_mode: str | None = None,
) -> ChangedFile:
    return ChangedFile(
        path=path,
        previous_path=previous_path,
        status=status,
        base_sha="b" * 40 if base_content is not None else None,
        head_sha="h" * 40 if head_content is not None else None,
        base_mode=base_mode if base_mode is not None else ("100644" if base_content is not None else None),
        head_mode=head_mode if head_mode is not None else ("100644" if head_content is not None else None),
        base_content=base_content,
        head_content=head_content,
        patch=patch,
        binary=binary,
        generated=generated,
        additions=additions,
        deletions=deletions,
    )


def snapshot(*files: ChangedFile, omissions: tuple[str, ...] = ()) -> PullSnapshot:
    value = PullSnapshot(
        number=53,
        state="open",
        draft=False,
        source_repository="fork/repo",
        source_head_sha="h" * 40,
        target_repository="owner/repo",
        target_base_sha="b" * 40,
        target_base_ref="main",
        title="title",
        body="body",
        changed_files=files,
        trusted_rules=(),
        related_files=(),
        checks=(),
    )
    if omissions:
        # GitHubPullSnapshot is intentionally accepted structurally in production;
        # this fixture only supplies the same immutable extension.
        from tools.qykw.github import GitHubPullSnapshot

        return GitHubPullSnapshot(**value.__dict__, omissions=omissions)
    return value


def budget(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "run_id": "QY-PR53-A1B2",
        "repository_id": 9001,
        "repository_limit": 1_000,
        "backend_context_window": 1_000,
        "output_reserve": 100,
        "safety_reserve_ratio": 0.10,
        "max_chunk_ratio": 0.25,
    }
    values.update(overrides)
    return values


class TestContextPlanning(unittest.TestCase):
    def test_late_high_risk_file_is_not_silently_lost(self) -> None:
        files = [changed_file(f"docs/{index:03}.txt") for index in range(101)]
        files.append(changed_file("auth/permissions.py"))
        plan = build_context_plan(
            snapshot(*files),
            **budget(repository_limit=30_000, backend_context_window=40_000),
        )

        self.assertIn("auth/permissions.py", plan.manifest.paths)
        self.assertEqual(plan.manifest.risk_order[0], "auth/permissions.py")
        self.assertTrue(plan.coverage.explains_every_file)

    def test_every_eligible_file_receives_minimum_triage_before_risk_depth(self) -> None:
        plan = build_context_plan(
            snapshot(*(changed_file(f"src/{index}.py") for index in range(3))),
            **budget(repository_limit=800, backend_context_window=1_000, max_chunk_ratio=1.0),
        )

        self.assertEqual(plan.coverage.reviewed_files, 3)
        self.assertFalse(any(item.startswith("budget_exhausted:") for item in plan.coverage.omissions))
        for path in plan.manifest.paths:
            self.assertIn(f"minimum_triage:{path}", plan.coverage.omissions)

    def test_insufficient_budget_for_every_minimum_triage_is_rejected_upfront(self) -> None:
        with self.assertRaisesRegex(ContextError, "impossible_triage_budget"):
            build_context_plan(
                snapshot(*(changed_file(f"src/{index}.py") for index in range(5))),
                **budget(repository_limit=500, backend_context_window=1_000, max_chunk_ratio=1.0),
            )

    def test_each_chunk_respects_effective_budget_for_a_giant_line(self) -> None:
        giant = "x" * 10_000
        plan = build_context_plan(
            snapshot(changed_file("src/giant.py", head_content=giant, patch="@@ -0,0 +1 @@\n+" + giant)),
            **budget(repository_limit=800, backend_context_window=1_000, max_chunk_ratio=1.0),
        )

        self.assertTrue(plan.chunks)
        self.assertLessEqual(sum(chunk.estimated_tokens for chunk in plan.chunks), 800)
        self.assertTrue(all(chunk.estimated_tokens <= plan.max_chunk_tokens for chunk in plan.chunks))

    def test_effective_input_budget_is_immutable_and_cjk_chunks_do_not_exceed_it(self) -> None:
        plan = build_context_plan(
            snapshot(changed_file("src/cjk.py", base_content="", head_content="长" * 1_000,
                                  patch="@@ -0,0 +1 @@\n+" + ("长" * 1_000))),
            **budget(repository_limit=900, backend_context_window=1_000, max_chunk_ratio=1.0),
        )

        self.assertEqual(plan.effective_input_budget_tokens, 800)
        self.assertLessEqual(sum(chunk.estimated_tokens for chunk in plan.chunks), plan.effective_input_budget_tokens)
        self.assertTrue(all(chunk.estimated_tokens <= plan.max_chunk_tokens for chunk in plan.chunks))

    def test_deleted_line_is_commentable_on_left_side(self) -> None:
        file = changed_file(
            "old.py",
            patch="@@ -7 +6,0 @@\n-removed\n",
            base_content=("unchanged\n" * 6) + "removed\n",
            head_content=None,
            status="removed",
            additions=0,
        )
        plan = build_context_plan(snapshot(file), **budget())

        self.assertIn(ChangedLine("old.py", 7, DiffSide.LEFT), plan.commentable_lines)
        self.assertNotIn(ChangedLine("old.py", 6, DiffSide.RIGHT), plan.commentable_lines)

    def test_rename_preserves_old_and_new_path_mapping(self) -> None:
        file = changed_file(
            "new.py",
            previous_path="old.py",
            status="renamed",
            patch="@@ -3 +3 @@\n-old\n+new\n",
            base_content="one\ntwo\nold\n",
            head_content="one\ntwo\nnew\n",
        )
        hunks = parse_hunks(file)

        self.assertEqual(hunks[0].previous_path, "old.py")
        self.assertEqual(
            hunks[0].changed_lines,
            (ChangedLine("old.py", 3, DiffSide.LEFT), ChangedLine("new.py", 3, DiffSide.RIGHT)),
        )

    def test_unsafe_previous_path_is_rejected_before_commentable_mapping(self) -> None:
        for previous_path in ("../old.py", "/old.py", r"old\\name.py", "old//name.py", "old/\x01name.py"):
            with self.subTest(previous_path=previous_path):
                unsafe = changed_file("new.py", previous_path=previous_path)
                with self.assertRaises(ContextError):
                    parse_hunks(unsafe)
                with self.assertRaises(ContextError):
                    build_context_plan(snapshot(unsafe), **budget())

    def test_hunk_rejects_incorrect_counts_and_never_marks_context_commentable(self) -> None:
        valid = changed_file(
            "safe.py", patch="@@ -4,2 +4,2 @@\n keep\n-old\n+new\n",
            base_content="one\ntwo\nthree\nkeep\nold\n", head_content="one\ntwo\nthree\nkeep\nnew\n",
        )
        self.assertEqual(
            parse_hunks(valid)[0].changed_lines,
            (ChangedLine("safe.py", 5, DiffSide.LEFT), ChangedLine("safe.py", 5, DiffSide.RIGHT)),
        )
        malformed = changed_file("bad.py", patch="@@ -1,2 +1 @@\n-old\n+new\n")
        with self.assertRaises(ContextError):
            parse_hunks(malformed)

    def test_hunk_rejects_changed_lines_outside_available_base_or_head_content(self) -> None:
        out_of_range = changed_file(
            "short.py",
            patch="@@ -7 +9 @@\n-old\n+new\n",
            base_content="old\n",
            head_content="new\n",
        )

        with self.assertRaises(ContextError):
            parse_hunks(out_of_range)

    def test_invalid_budgets_are_rejected_and_gateway_omissions_are_immutable_coverage_reasons(self) -> None:
        file = changed_file("unreadable.py", head_content=None)
        plan = build_context_plan(snapshot(file, omissions=("head_content_missing:unreadable.py",)), **budget())
        self.assertIn("head_content_missing:unreadable.py", plan.coverage.omissions)
        self.assertTrue(plan.coverage.explains_every_file)
        with self.assertRaises(ContextError):
            build_context_plan(snapshot(file), **budget(repository_limit=0))

    def test_utf8_estimate_is_conservative_and_deterministic(self) -> None:
        text = "函数(value):\n    return 值 + 1\n"
        self.assertEqual(estimate_tokens(text), estimate_tokens(text))
        self.assertGreaterEqual(estimate_tokens(text), len(text))

    def test_split_chunks_keep_pr_path_side_and_line_provenance(self) -> None:
        patch = "@@ -1,0 +1,1 @@\n+" + ("长" * 1_000) + "\n"
        plan = build_context_plan(
            snapshot(changed_file("src/large.py", patch=patch, base_content="", head_content="长" * 1_000)),
            **budget(repository_limit=900, backend_context_window=1_000, max_chunk_ratio=1.0),
        )

        self.assertGreater(len(plan.chunks), 1)
        diff_chunks = [chunk for chunk in plan.chunks if "side=RIGHT" in chunk.text]
        self.assertTrue(diff_chunks)
        for chunk in diff_chunks:
            self.assertIn("repo=owner/repo", chunk.text)
            self.assertIn("pr=53", chunk.text)
            self.assertIn("path=src/large.py", chunk.text)
            self.assertIn("side=RIGHT", chunk.text)
            self.assertRegex(chunk.text, r"new=1-1")

    def test_diff_fragments_keep_fixed_refs_and_exact_later_side_coordinates(self) -> None:
        additions = "".join(f"+line_{line:04}\n" for line in range(1, 101))
        file = changed_file(
            "new.py",
            previous_path="old.py",
            status="renamed",
            patch="@@ -1,1 +1,100 @@\n-old\n" + additions,
            base_content="old\n",
            head_content="".join(f"line_{line:04}\n" for line in range(1, 101)),
        )
        plan = build_context_plan(
            snapshot(file),
            **budget(repository_limit=40_000, backend_context_window=50_000, output_reserve=1_000),
        )
        line_100 = next(chunk.text for chunk in plan.chunks if "+line_0100" in chunk.text)
        deleted = next(chunk.text for chunk in plan.chunks if "-old" in chunk.text)

        self.assertIn("side=LEFT", deleted)
        self.assertIn("old=1-1", deleted)
        self.assertIn("new=-", deleted)
        self.assertIn("path=old.py", deleted)
        self.assertIn("side=RIGHT", line_100)
        self.assertIn("old=-", line_100)
        self.assertIn("new=100-100", line_100)
        self.assertIn("bs=" + ("b" * 40), line_100)
        self.assertIn("hs=" + ("h" * 40), line_100)
        self.assertIn("path=new.py", line_100)
        self.assertIn("prev=old.py", line_100)
        self.assertTrue(all("bs=" + ("b" * 40) in chunk.text for chunk in plan.chunks))
        self.assertTrue(all("hs=" + ("h" * 40) in chunk.text for chunk in plan.chunks))

    def test_ten_thousand_line_patch_is_bounded_without_prefix_truncation(self) -> None:
        patch = "@@ -1,0 +1,10000 @@\n" + "".join(f"+value_{line}\n" for line in range(1, 10_001))
        plan = build_context_plan(
            snapshot(changed_file("src/large.py", patch=patch, head_content="x\n" * 10_000)),
            **budget(repository_limit=3_000, backend_context_window=4_000),
        )

        self.assertEqual(plan.coverage.total_hunks, 1)
        self.assertLessEqual(sum(chunk.estimated_tokens for chunk in plan.chunks), 3_000)
        self.assertTrue(plan.coverage.explains_every_file)

    def test_no_newline_marker_additions_deletions_and_mode_only_have_explicit_contracts(self) -> None:
        added = changed_file(
            "new.py", status="added", base_content=None,
            patch="@@ -0,0 +1 @@\n+new\n\\ No newline at end of file\n", additions=1, deletions=0,
        )
        deleted = changed_file(
            "gone.py", status="removed", head_content=None,
            patch="@@ -1 +0,0 @@\n-old\n\\ No newline at end of file\n", additions=0,
        )
        mode_only = changed_file("script.sh", patch="", base_mode="100644", head_mode="100755")
        plan = build_context_plan(snapshot(added, deleted, mode_only), **budget())

        self.assertIn(ChangedLine("new.py", 1, DiffSide.RIGHT), plan.commentable_lines)
        self.assertIn(ChangedLine("gone.py", 1, DiffSide.LEFT), plan.commentable_lines)
        self.assertNotIn(ChangedLine("script.sh", 1, DiffSide.RIGHT), plan.commentable_lines)
        self.assertIn("mode_only:script.sh", plan.coverage.omissions)

    def test_binary_generated_sensitive_and_malformed_files_have_file_reasons(self) -> None:
        plan = build_context_plan(
            snapshot(
                changed_file("image.png", binary=True),
                changed_file("web/bundle.generated.js", generated=True),
                changed_file(".env.production"),
                changed_file("broken.py", patch="not a hunk\n"),
            ),
            **budget(),
        )

        for expected in ("binary:image.png", "generated:web/bundle.generated.js", "sensitive:.env.production"):
            self.assertIn(expected, plan.coverage.omissions)
        self.assertTrue(any(item.startswith("malformed_patch:broken.py:") for item in plan.coverage.omissions))
        self.assertTrue(plan.coverage.explains_every_file)

    def test_context_is_keyed_to_its_exact_pr_head_and_repository(self) -> None:
        first = build_context_plan(snapshot(changed_file("same.py")), **budget())
        second_snapshot = snapshot(changed_file("same.py"))
        second_snapshot = PullSnapshot(**{**second_snapshot.__dict__, "number": 54, "source_head_sha": "z" * 40})
        second = build_context_plan(second_snapshot, **budget(run_id="QY-PR54-A1B2"))

        self.assertNotEqual(first.run_id, second.run_id)
        self.assertIn("run_id=QY-PR53-A1B2", first.run_id)
        self.assertIn("repository_id=9001", first.run_id)
        self.assertTrue(all("run_id=QY-PR53-A1B2" in chunk.chunk_id for chunk in first.chunks))
        self.assertTrue(all("run_id=QY-PR54-A1B2" in chunk.chunk_id for chunk in second.chunks))

    def test_truncated_hunk_record_is_not_counted_as_reviewed(self) -> None:
        giant = "x" * 10_000
        plan = build_context_plan(
            snapshot(changed_file("src/giant.py", base_content="", head_content=giant, patch="@@ -0,0 +1 @@\n+" + giant)),
            **budget(repository_limit=450, backend_context_window=1_000, max_chunk_ratio=1.0),
        )

        self.assertEqual(plan.coverage.total_hunks, 1)
        self.assertEqual(plan.coverage.reviewed_hunks, 0)
        self.assertTrue(any(item.startswith("budget_truncated_") and ":src/giant.py:hunk=0:" in item for item in plan.coverage.omissions))

    def test_ten_thousand_record_truncation_has_bounded_exact_range_summary(self) -> None:
        patch = "@@ -0,0 +1,10000 @@\n" + "".join(f"+line_{line:04}\n" for line in range(1, 10_001))
        plan = build_context_plan(
            snapshot(changed_file("src/huge.py", base_content="", head_content="x\n" * 10_000, patch=patch)),
            **budget(repository_limit=500, backend_context_window=1_000, max_chunk_ratio=1.0),
        )
        truncations = tuple(item for item in plan.coverage.omissions if item.startswith("budget_truncated_"))

        self.assertEqual(plan.coverage.reviewed_hunks, 0)
        self.assertLessEqual(len(truncations), 2)
        self.assertTrue(any(item.startswith("budget_truncated_unallocated:src/huge.py:hunk=0:records=") and item.endswith("-10000") for item in truncations))
        self.assertLess(len("\n".join(plan.coverage.omissions)), 1_000)
        self.assertTrue(all(chunk.estimated_tokens <= plan.max_chunk_tokens for chunk in plan.chunks))

    def test_truncation_summaries_stay_distinct_and_deterministic_per_file_and_hunk(self) -> None:
        first = changed_file(
            "a.py", patch="@@ -1 +1 @@\n-old\n+new\n@@ -3 +3 @@\n-old\n+new\n",
            base_content="old\nmid\nold\n", head_content="new\nmid\nnew\n",
        )
        second = changed_file(
            "b.py", patch="@@ -1 +1 @@\n-old\n+new\n", base_content="old\n", head_content="new\n",
        )
        tight = budget(repository_limit=600, backend_context_window=1_000, max_chunk_ratio=1.0)
        first_plan = build_context_plan(snapshot(first, second), **tight)
        repeated_plan = build_context_plan(snapshot(first, second), **tight)
        summaries = tuple(item for item in first_plan.coverage.omissions if item.startswith("budget_truncated_"))

        self.assertEqual(summaries, tuple(item for item in repeated_plan.coverage.omissions if item.startswith("budget_truncated_")))
        self.assertTrue(any(":a.py:hunk=0:" in item for item in summaries))
        self.assertTrue(any(":a.py:hunk=1:" in item for item in summaries))
        self.assertTrue(any(":b.py:hunk=0:" in item for item in summaries))
        self.assertLessEqual(len(summaries), 4)

    def test_contiguous_fully_unallocated_hunks_are_coalesced(self) -> None:
        hunk_count = 512
        patch = "".join(f"@@ -{line} +{line} @@\n-old\n+new\n" for line in range(1, hunk_count + 1))
        file = changed_file(
            "src/tiny.py", patch=patch, base_content="old\n" * hunk_count,
            head_content="new\n" * hunk_count,
        )
        plan = build_context_plan(
            snapshot(file),
            **budget(repository_limit=500, backend_context_window=1_000, max_chunk_ratio=1.0),
        )
        summaries = tuple(item for item in plan.coverage.omissions if item.startswith("budget_truncated_"))

        self.assertTrue(any(item.startswith("budget_truncated_unallocated_hunks:src/tiny.py:hunks=") for item in summaries))
        self.assertLessEqual(len(summaries), 3)
        self.assertLess(len("\n".join(plan.coverage.omissions)), 1_000)

    def test_controller_run_ids_isolate_identical_snapshots_and_reject_unsafe_values(self) -> None:
        same_snapshot = snapshot(changed_file("same.py"))
        first = build_context_plan(same_snapshot, **budget(run_id="QY-PR53-FIRST"))
        repeated = build_context_plan(same_snapshot, **budget(run_id="QY-PR53-FIRST"))
        second = build_context_plan(same_snapshot, **budget(run_id="QY-PR53-SECOND"))

        self.assertEqual(first.run_id, repeated.run_id)
        self.assertEqual(tuple(chunk.chunk_id for chunk in first.chunks), tuple(chunk.chunk_id for chunk in repeated.chunks))
        self.assertTrue(set(chunk.chunk_id for chunk in first.chunks).isdisjoint(chunk.chunk_id for chunk in second.chunks))
        self.assertIn("base_sha=" + ("b" * 40), first.run_id)
        self.assertIn("base_ref=main", first.run_id)
        self.assertIn("head_sha=" + ("h" * 40), first.run_id)
        with self.assertRaises(ContextError):
            build_context_plan(same_snapshot, **budget(run_id="unsafe/run"))


if __name__ == "__main__":
    unittest.main()
