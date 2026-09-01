from __future__ import annotations

from dataclasses import replace
import unittest

from tools.qykw.domain import (
    CommandMode, CommandName, CommandRequest, CoverageReport, DiffSide,
    IssueComment, RunContext, RunRecord, RunStage, RunStatus,
)
from tools.qykw.state import GitHubCommentStateStore, render_cancel_marker, render_state_marker


def context(run_id: str = "run-1", key: str = "key-1") -> RunContext:
    return RunContext(run_id, key, 8, "owner/repo", 53, "pull_request", "opened",
        "fork/repo", "a" * 40, "b" * 40, "main",
        CommandRequest(CommandName.REVIEW, "", CommandMode.READ_ONLY), "alice")


def record(*, run_id: str = "run-1", key: str = "key-1", stage: RunStage = RunStage.ACCEPTED,
           status: RunStatus = RunStatus.ACTIVE, updated: str = "2026-09-02T00:00:00Z",
           initial: bool = False) -> RunRecord:
    return RunRecord(context(run_id, key), stage, status, "prompt-v1", None, initial,
        CoverageReport(1, 1, 1, 1, (), True), (), None, "2026-09-02T00:00:00Z", updated)


class FakeGateway:
    def __init__(self) -> None:
        self.comments: list[IssueComment] = []
        self.writes: list[str] = []
        self.next_id = 1

    def list_issue_comments(self, pr_number: int) -> tuple[IssueComment, ...]:
        self.last_pr = pr_number
        return tuple(self.comments)

    def create_issue_comment(self, pr_number: int, body: str) -> int:
        comment_id = self.next_id
        self.next_id += 1
        self.comments.append(IssueComment(comment_id, "qykw", body, "2026-09-02T00:00:00Z"))
        self.writes.append("create")
        return comment_id

    def update_issue_comment(self, comment_id: int, body: str) -> None:
        for index, item in enumerate(self.comments):
            if item.comment_id == comment_id:
                self.comments[index] = IssueComment(comment_id, item.author_login, body, "2026-09-02T00:00:00Z")
                self.writes.append("update")
                return
        raise AssertionError("unknown comment")


class TestGitHubCommentStateStore(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = FakeGateway()
        self.store = GitHubCommentStateStore(self.gateway, now=lambda: "2026-09-02T01:00:00Z")

    def test_create_find_and_save_are_idempotent_and_versioned(self) -> None:
        item = record()
        self.assertTrue(self.store.create(item))
        self.assertFalse(self.store.create(item))
        found = self.store.find_by_idempotency_key(53, "key-1")
        self.assertIsNotNone(found)
        self.assertEqual(found.context.run_id, "run-1")  # type: ignore[union-attr]
        self.assertIn("qykw-state:v1", self.gateway.comments[0].body)
        self.store.save(replace(item, stage=RunStage.ANALYZING, updated_at="2026-09-02T02:00:00Z"))
        self.assertEqual(self.store.get(53, "run-1").stage, RunStage.ANALYZING)  # type: ignore[union-attr]

    def test_state_scan_uses_all_comments_and_ignores_forged_author_marker(self) -> None:
        item = record(run_id="late", key="late-key")
        marker = render_state_marker(item)
        self.gateway.comments = [
            *(IssueComment(index, "someone", marker, "2026-09-02T00:00:00Z") for index in range(1, 101)),
            IssueComment(101, "qykw", marker, "2026-09-02T00:00:01Z"),
        ]
        self.assertEqual(self.store.get(53, "late").context.idempotency_key, "late-key")  # type: ignore[union-attr]

    def test_legacy_own_marker_is_read_only_compatible_and_not_rewritten(self) -> None:
        item = record()
        payload = render_state_marker(item).removeprefix("<!-- qykw-state:v1 ").removesuffix(" -->")
        legacy = "<!-- qykw-state " + payload + " -->"
        self.gateway.comments = [IssueComment(1, "qykw", legacy, "2026-09-02T00:00:00Z")]
        self.assertEqual(self.store.get(53, "run-1").context.run_id, "run-1")  # type: ignore[union-attr]
        self.store.create(record(run_id="next", key="next"))
        self.assertIn("qykw-state:v1", self.gateway.comments[-1].body)
        self.assertNotIn("qykw-state ", self.gateway.comments[-1].body)

    def test_latest_active_uses_stage_then_updated_and_successful_initial_is_exact(self) -> None:
        for item in (
            record(run_id="old", key="old", stage=RunStage.ANALYZING, updated="2026-09-02T03:00:00Z"),
            record(run_id="late", key="late", stage=RunStage.PUBLISHING, updated="2026-09-02T02:00:00Z"),
            record(run_id="done", key="done", stage=RunStage.COMPLETED, status=RunStatus.COMPLETED, initial=True),
        ):
            self.store.create(item)
        self.assertEqual(self.store.find_latest_active(53).context.run_id, "late")  # type: ignore[union-attr]
        self.assertEqual(self.store.find_latest(53).context.run_id, "done")  # type: ignore[union-attr]
        self.assertTrue(self.store.has_successful_initial_review(53))
        self.store.save(replace(record(run_id="done", key="done", stage=RunStage.COMPLETED,
            status=RunStatus.PARTIAL, initial=True), updated_at="2026-09-02T04:00:00Z"))
        self.assertFalse(self.store.has_successful_initial_review(53))

    def test_save_rejects_immutable_run_context_or_prompt_mismatch(self) -> None:
        self.store.create(record())
        with self.assertRaises(ValueError):
            self.store.save(replace(record(), prompt_version="other"))
        with self.assertRaises(ValueError):
            self.store.save(replace(record(), context=replace(context(), source_head_sha="c" * 40)))

    def test_cancel_is_append_only_idempotent_and_old_save_cannot_erase_it(self) -> None:
        item = record()
        self.store.create(item)
        cancelled = self.store.request_cancel(53, "run-1", stop_comment_id=77, actor_login="alice")
        again = self.store.request_cancel(53, "run-1", stop_comment_id=77, actor_login="alice")
        self.assertEqual(cancelled, again)
        self.store.save(replace(item, stage=RunStage.ANALYZING))
        self.assertTrue(self.store.is_cancel_requested(53, "run-1"))
        self.assertIn("qykw-cancel:v1", self.gateway.comments[1].body)
        self.assertEqual(self.gateway.writes.count("create"), 2)

    def test_cancel_rescan_rejects_forged_marker_and_accepts_late_own_marker(self) -> None:
        forged = render_cancel_marker(53, "run-1", 88, "mallory", "2026-09-02T00:00:00Z")
        self.gateway.comments = [
            *(IssueComment(index, "mallory", forged, "2026-09-02T00:00:00Z") for index in range(1, 101)),
            IssueComment(101, "qykw", forged, "2026-09-02T00:00:00Z"),
        ]
        self.assertTrue(self.store.is_cancel_requested(53, "run-1"))
        self.assertFalse(self.store.is_cancel_requested(53, "other"))


if __name__ == "__main__":
    unittest.main()
