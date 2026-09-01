from __future__ import annotations

import unittest

from tools.qykw.domain import (
    CommandMode, CommandName, CommandRequest, CoverageReport, DiffSide, Finding,
    InlineComment, IssueComment, ReviewComment, ReviewResult, RunContext, RunStatus, Severity,
)
from tools.qykw.publish import ReviewPublisher


def run() -> RunContext:
    return RunContext("run-1", "key-1", 8, "owner/repo", 53, "pull_request", "opened", "fork/repo",
        "a" * 40, "b" * 40, "main", CommandRequest(CommandName.REVIEW, "", CommandMode.READ_ONLY), "alice")


def finding(path: str = "src/a.py", line: int = 3, side: DiffSide = DiffSide.RIGHT,
            fingerprint: str = "f-1") -> Finding:
    return Finding(path, line, side, Severity.P1, "<b>failure</b> @victim", "impact https://evil.test",
        "<img src=x> evidence", "[click](https://evil.test) @mention", "verify", fingerprint)


def review(*items: Finding) -> ReviewResult:
    return ReviewResult("<script>x</script> @all", tuple(items), CoverageReport(2, 2, 3, 3, (), True),
        ("checked",), ("none",))


class FakeGateway:
    def __init__(self, *, fail_after: int | None = None) -> None:
        self.issue_comments: list[IssueComment] = [IssueComment(10, "qykw", "<!-- qykw-state:v1 {} -->", "now")]
        self.review_comments: list[ReviewComment] = []
        self.calls: list[str] = []
        self.inline_batches: list[tuple[InlineComment, ...]] = []
        self.fail_after = fail_after

    def list_review_comments(self, pr_number: int) -> tuple[ReviewComment, ...]:
        return tuple(self.review_comments)

    def list_issue_comments(self, pr_number: int) -> tuple[IssueComment, ...]:
        return tuple(self.issue_comments)

    def create_issue_comment(self, pr_number: int, body: str) -> int:
        self.calls.append("create_summary")
        return 10

    def update_issue_comment(self, comment_id: int, body: str) -> None:
        self.calls.append("update_summary")
        self.summary = body

    def create_review(self, pr_number: int, *, head_sha: str, body: str,
                      comments: tuple[InlineComment, ...]) -> int:
        self.calls.append("create_review")
        if self.fail_after is not None and (len(comments) > 1 or len(self.inline_batches) >= self.fail_after):
            raise RuntimeError("write failed")
        self.inline_batches.append(comments)
        for item in comments:
            self.review_comments.append(ReviewComment(100 + len(self.review_comments), "qykw", item.body, "now",
                item.path, item.line, item.side))
        return 100 + len(self.inline_batches)


class TestReviewPublisher(unittest.TestCase):
    def test_summary_precedes_inline_comments_and_uses_comment_event(self) -> None:
        gateway = FakeGateway()
        result = ReviewPublisher(gateway, max_findings=5).publish_review(run(), review(finding(), finding("src/b.py", 9, fingerprint="f-2")))
        self.assertEqual(gateway.calls, ["update_summary", "create_review"])
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(len(gateway.inline_batches[0]), 2)
        self.assertIn("问题统计", result.summary_body)
        self.assertNotIn("<script>", result.summary_body)
        self.assertNotIn("https://evil.test", result.summary_body)
        self.assertNotIn("@", result.summary_body)

    def test_zero_problems_uses_exact_phrase_and_no_review_write(self) -> None:
        gateway = FakeGateway()
        result = ReviewPublisher(gateway).publish_review(run(), review())
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertIn("未发现有充分证据的问题", result.summary_body)
        self.assertEqual(gateway.calls, ["update_summary"])

    def test_deleted_finding_publishes_left_side(self) -> None:
        gateway = FakeGateway()
        ReviewPublisher(gateway).publish_review(run(), review(finding("old.py", 7, DiffSide.LEFT)))
        self.assertEqual(gateway.inline_batches[0][0].side, DiffSide.LEFT)

    def test_trusted_matching_marker_dedupes_only_same_head_path_line_side_and_fingerprint(self) -> None:
        gateway = FakeGateway()
        from tools.qykw.publish import render_fingerprint_marker
        current = finding()
        gateway.review_comments = [
            ReviewComment(1, "qykw", render_fingerprint_marker(run(), current), "now", "src/a.py", 3, DiffSide.RIGHT),
            ReviewComment(2, "mallory", render_fingerprint_marker(run(), current), "now", "src/a.py", 3, DiffSide.RIGHT),
        ]
        result = ReviewPublisher(gateway).publish_review(run(), review(current, finding("src/a.py", 3, DiffSide.LEFT, "f-1")))
        self.assertEqual(result.published_fingerprints, ("f-1",))
        self.assertEqual(gateway.inline_batches[0][0].side, DiffSide.LEFT)

    def test_partial_inline_failure_keeps_completed_summary(self) -> None:
        gateway = FakeGateway(fail_after=1)
        result = ReviewPublisher(gateway, max_findings=2).publish_review(run(), review(finding(), finding("b.py", 2, fingerprint="f-2")))
        self.assertEqual(result.status, RunStatus.PARTIAL)
        self.assertEqual(result.published_fingerprints, ("f-2",))
        self.assertIn("inline_publish_failed", result.warning_codes)
        self.assertIn("问题统计", result.summary_body)

    def test_summary_failure_writes_no_inline_comments(self) -> None:
        gateway = FakeGateway()
        def fail(comment_id: int, body: str) -> None:
            raise RuntimeError("summary failed")
        gateway.update_issue_comment = fail  # type: ignore[method-assign]
        result = ReviewPublisher(gateway).publish_review(run(), review(finding()))
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(gateway.inline_batches, [])


if __name__ == "__main__":
    unittest.main()
