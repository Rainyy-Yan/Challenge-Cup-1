from __future__ import annotations

from dataclasses import replace
import unittest

from tools.qykw.domain import (
    CommandMode, CommandName, CommandRequest, CoverageReport, DiffSide, Finding,
    InlineComment, IssueComment, ReviewComment, ReviewResult, RunContext, RunRecord, RunStage, RunStatus, Severity,
)
from tools.qykw.publish import ReviewPublisher, sanitize_public_text
from tools.qykw.state import GitHubCommentStateStore


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


def state_record() -> RunRecord:
    return RunRecord(run(), RunStage.PUBLISHING, RunStatus.ACTIVE, "prompt-v1", 10, False,
        CoverageReport(2, 2, 3, 3, (), True), (), None, "2026-09-02T00:00:00Z", "2026-09-02T00:01:00Z")


class FakeState:
    def __init__(self, item: RunRecord | None = None) -> None:
        self.item = item if item is not None else state_record()

    def get(self, pr_number: int, run_id: str) -> RunRecord | None:
        return self.item if self.item.context.pr_number == pr_number and self.item.context.run_id == run_id else None


class MissingState:
    def get(self, pr_number: int, run_id: str) -> RunRecord | None:
        return None


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


class RoundTripGateway:
    def __init__(self) -> None:
        self.repository = "owner/repo"
        self.issue_comments: list[IssueComment] = []
        self.review_comments: list[ReviewComment] = []
        self.next_id = 1

    def list_issue_comments(self, pr_number: int) -> tuple[IssueComment, ...]:
        return tuple(self.issue_comments)

    def create_issue_comment(self, pr_number: int, body: str) -> int:
        comment_id = self.next_id
        self.next_id += 1
        self.issue_comments.append(IssueComment(comment_id, "qykw", body, "2026-09-02T00:01:00Z"))
        return comment_id

    def update_issue_comment(self, comment_id: int, body: str) -> None:
        for index, comment in enumerate(self.issue_comments):
            if comment.comment_id == comment_id:
                self.issue_comments[index] = IssueComment(comment_id, "qykw", body, "2026-09-02T00:02:00Z")
                return
        raise AssertionError("unknown state comment")

    def list_review_comments(self, pr_number: int) -> tuple[ReviewComment, ...]:
        return tuple(self.review_comments)

    def create_review(self, pr_number: int, *, head_sha: str, body: str,
                      comments: tuple[InlineComment, ...]) -> int:
        return 1


class TestReviewPublisher(unittest.TestCase):
    def test_shared_public_sanitizer_removes_markup_uris_and_mentions(self) -> None:
        rendered = sanitize_public_text(
            "<img src=x> [label](https://evil.test) <https://evil.test> @team "
            "ftp://evil.test and normal evidence：保留"
        )
        for forbidden in ("<img", "https:", "ftp:", "@team", "[label]"):
            self.assertNotIn(forbidden, rendered.lower())
        self.assertIn("label", rendered)
        self.assertIn("normal evidence：保留", rendered)

    def test_write_guard_blocks_every_public_review_write(self) -> None:
        gateway = FakeGateway()
        result = ReviewPublisher(gateway, FakeState()).publish_review(
            run(), review(finding()), write_guard=lambda: False
        )
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(gateway.calls, [])

    def test_summary_precedes_inline_comments_and_uses_comment_event(self) -> None:
        gateway = FakeGateway()
        result = ReviewPublisher(gateway, FakeState(), max_findings=5).publish_review(run(), review(finding(), finding("src/b.py", 9, fingerprint="f-2")))
        self.assertEqual(gateway.calls, ["update_summary", "create_review"])
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(len(gateway.inline_batches[0]), 2)
        self.assertIn("问题统计", result.summary_body)
        self.assertNotIn("<script>", result.summary_body)
        self.assertNotIn("https://evil.test", result.summary_body)
        self.assertNotIn("@", result.summary_body)

    def test_zero_problems_uses_exact_phrase_and_no_review_write(self) -> None:
        gateway = FakeGateway()
        result = ReviewPublisher(gateway, FakeState()).publish_review(run(), review())
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertIn("未发现有充分证据的问题", result.summary_body)
        self.assertEqual(gateway.calls, ["update_summary"])

    def test_deleted_finding_publishes_left_side(self) -> None:
        gateway = FakeGateway()
        ReviewPublisher(gateway, FakeState()).publish_review(run(), review(finding("old.py", 7, DiffSide.LEFT)))
        self.assertEqual(gateway.inline_batches[0][0].side, DiffSide.LEFT)

    def test_trusted_matching_marker_dedupes_only_same_head_path_line_side_and_fingerprint(self) -> None:
        gateway = FakeGateway()
        from tools.qykw.publish import render_fingerprint_marker
        current = finding()
        gateway.review_comments = [
            ReviewComment(1, "qykw", render_fingerprint_marker(run(), current), "now", "src/a.py", 3, DiffSide.RIGHT),
            ReviewComment(2, "mallory", render_fingerprint_marker(run(), current), "now", "src/a.py", 3, DiffSide.RIGHT),
        ]
        result = ReviewPublisher(gateway, FakeState()).publish_review(run(), review(current, finding("src/a.py", 3, DiffSide.LEFT, "f-1")))
        self.assertEqual(result.published_fingerprints, ("f-1",))
        self.assertEqual(gateway.inline_batches[0][0].side, DiffSide.LEFT)

    def test_partial_inline_failure_keeps_completed_summary(self) -> None:
        gateway = FakeGateway(fail_after=1)
        result = ReviewPublisher(gateway, FakeState(), max_findings=2).publish_review(run(), review(finding(), finding("b.py", 2, fingerprint="f-2")))
        self.assertEqual(result.status, RunStatus.PARTIAL)
        self.assertEqual(result.published_fingerprints, ("f-2",))
        self.assertIn("inline_publish_failed", result.warning_codes)
        self.assertIn("问题统计", result.summary_body)

    def test_summary_failure_writes_no_inline_comments(self) -> None:
        gateway = FakeGateway()
        def fail(comment_id: int, body: str) -> None:
            raise RuntimeError("summary failed")
        gateway.update_issue_comment = fail  # type: ignore[method-assign]
        result = ReviewPublisher(gateway, FakeState()).publish_review(run(), review(finding()))
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(gateway.inline_batches, [])

    def test_review_without_matching_state_fails_closed_before_summary_write(self) -> None:
        gateway = FakeGateway()
        wrong = replace(state_record(), context=replace(run(), idempotency_key="other"))
        result = ReviewPublisher(gateway, FakeState(wrong)).publish_review(run(), review(finding()))
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(gateway.calls, [])
        self.assertEqual(gateway.inline_batches, [])

    def test_status_and_review_preserve_recoverable_canonical_state_markers(self) -> None:
        gateway = FakeGateway()
        state = FakeState()
        publisher = ReviewPublisher(gateway, state)
        publisher.publish_status(state.item)
        self.assertIn("qykw-state:v1", gateway.summary)
        publisher.publish_review(run(), review())
        self.assertIn("qykw-state:v1", gateway.summary)

    def test_status_requires_matching_store_record_and_uses_fetched_current_record(self) -> None:
        stale = state_record()
        current = replace(stale, stage=RunStage.COMPLETED, status=RunStatus.COMPLETED,
            updated_at="2026-09-02T00:02:00Z")
        gateway = FakeGateway()
        ReviewPublisher(gateway, FakeState(current)).publish_status(stale)
        self.assertIn("completed", gateway.summary)
        for state in (
            None,
            MissingState(),
            FakeState(replace(current, context=replace(run(), source_head_sha="c" * 40))),
            FakeState(replace(current, context=replace(run(), target_base_sha="d" * 40))),
            FakeState(replace(current, context=replace(run(), idempotency_key="other"))),
            FakeState(replace(current, prompt_version="other")),
        ):
            with self.subTest(state=state):
                blocked = FakeGateway()
                ReviewPublisher(blocked, state).publish_status(stale)
                self.assertEqual(blocked.calls, [])

    def test_inline_path_keeps_safe_repository_path_exact_and_rejects_unsafe_before_writes(self) -> None:
        gateway = FakeGateway()
        exact = "src/a_b[1].py"
        ReviewPublisher(gateway, FakeState()).publish_review(run(), review(finding(exact)))
        self.assertEqual(gateway.inline_batches[0][0].path, exact)
        for unsafe in ("../escape.py", "/absolute.py", "dir//empty.py", "dir\\windows.py", "dir/./dot.py", "dir/../up.py"):
            with self.subTest(path=unsafe):
                blocked = FakeGateway()
                result = ReviewPublisher(blocked, FakeState()).publish_review(run(), review(finding(unsafe)))
                self.assertEqual(result.status, RunStatus.FAILED)
                self.assertEqual(blocked.calls, [])

    def test_sanitizer_removes_all_uri_schemes_without_damaging_chinese_or_windows_paths(self) -> None:
        gateway = FakeGateway()
        unsafe = "ftp://x file:///x data:text/html,javascript ssh://x custom:thing //host.example java script:alert h t t p : //x [link](ssh://x) <FTP://x>"
        noisy = Finding("src/a.py", 3, DiffSide.RIGHT, Severity.P1, unsafe,
            "说明：保留；Windows C:\\work\\file.txt 与 C:/work/file.txt", "ok", "ok", "ok", "uri")
        result = ReviewPublisher(gateway, FakeState()).publish_review(run(), review(noisy))
        public = result.summary_body + "\n" + gateway.inline_batches[0][0].body
        for forbidden in ("ftp:", "file:", "data:", "javascript", "ssh:", "custom:", "//host.example", "h t t p", "[link]"):
            self.assertNotIn(forbidden, public.lower())
        self.assertIn("说明：保留", public)
        self.assertIn("C:\\work\\file.txt", public)
        self.assertIn("C:/work/file.txt", public)

    def test_sanitizer_preserves_normal_labels_and_source_comments(self) -> None:
        cases = (
            ("Failure: timeout", "Failure: timeout"),
            ("Version: 1", "Version: 1"),
            ("证据：正常", "证据：正常"),
            ("x //comment", "x //comment"),
            ("// TODO keep", "// TODO keep"),
            ("C:\\work\\file.txt and C:/work/file.txt", "C:\\work\\file.txt"),
        )
        for source, expected in cases:
            with self.subTest(source=source):
                gateway = FakeGateway()
                normal = Finding("src/a.py", 3, DiffSide.RIGHT, Severity.P1,
                    source, "impact", "evidence", "suggestion", "verify", "normal")
                result = ReviewPublisher(gateway, FakeState()).publish_review(run(), review(normal))
                public = result.summary_body + "\n" + gateway.inline_batches[0][0].body
                self.assertIn(expected, public)

    def test_real_store_recovers_context_after_status_and_review_summary_updates(self) -> None:
        gateway = RoundTripGateway()
        store = GitHubCommentStateStore(gateway, repository="owner/repo")
        original = replace(state_record(), summary_comment_id=None)
        self.assertTrue(store.create(original))
        stored = store.get(53, "run-1")
        self.assertIsNotNone(stored)
        publisher = ReviewPublisher(gateway, store)
        publisher.publish_status(stored)  # type: ignore[arg-type]
        after_status = store.get(53, "run-1")
        self.assertEqual(after_status.context, run())  # type: ignore[union-attr]
        self.assertEqual(after_status.prompt_version, "prompt-v1")  # type: ignore[union-attr]
        published = publisher.publish_review(run(), review())
        self.assertEqual(published.status, RunStatus.COMPLETED)
        after_review = store.get(53, "run-1")
        self.assertEqual(after_review.context, run())  # type: ignore[union-attr]
        self.assertEqual(after_review.summary_comment_id, stored.summary_comment_id)  # type: ignore[union-attr]

    def test_sanitizer_neutralizes_urls_html_images_and_mentions(self) -> None:
        gateway = FakeGateway()
        malicious = Finding("src/a.py", 3, DiffSide.RIGHT, Severity.P1,
            "<A HREF='HTTPS://evil.test'>@victim</A> [label](hTTps://evil.test) www.evil.test",
            "mailto:staff@example.test <https://evil.test> &#64;team", "![alt](https://evil.test/a.png)",
            "<img src=x> https%3A%2F%2Fevil.test", "@reviewer", "fingerprint")
        result = ReviewPublisher(gateway, FakeState()).publish_review(run(), review(malicious))
        public = result.summary_body + "\n" + gateway.inline_batches[0][0].body
        for forbidden in ("http", "www.", "mailto:", "<img", "@victim", "@team", "@reviewer", "[label]", "![alt]"):
            self.assertNotIn(forbidden, public.lower())
        self.assertIn("label", public)

    def test_summary_includes_bounded_coverage_and_sanitized_omissions(self) -> None:
        gateway = FakeGateway()
        noisy = ReviewResult("ok", (), CoverageReport(2, 1, 7, 6,
            ("<b>omit</b> https://evil.test @owner",), False), (), ("<a href=x>limit</a> @user",))
        result = ReviewPublisher(gateway, FakeState()).publish_review(run(), noisy)
        self.assertIn("覆盖情况", result.summary_body)
        self.assertIn("文件：1/2", result.summary_body)
        self.assertIn("变更块：6/7", result.summary_body)
        self.assertIn("未完全覆盖", result.summary_body)
        self.assertIn("omit", result.summary_body)
        self.assertIn("limit", result.summary_body)
        self.assertNotIn("https://", result.summary_body)
        self.assertNotIn("@", result.summary_body)


if __name__ == "__main__":
    unittest.main()
