"""Configuration tests for the MiniMax review workflow."""

from __future__ import annotations

from pathlib import Path
import unittest


WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "minimax-review.yml"
)


class TestMiniMaxWorkflow(unittest.TestCase):
    def test_uses_qykw_machine_user_token(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("GITHUB_TOKEN: ${{ secrets.QYKW_TOKEN }}", workflow)
        self.assertNotIn("create-github-app-token", workflow)
        self.assertNotIn("MINIMAX_REVIEW_APP_PRIVATE_KEY", workflow)

    def test_mentions_in_pr_comments_trigger_a_review(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("issue_comment:", workflow)
        self.assertIn("pull_request_review_comment:", workflow)
        self.assertIn("contains(github.event.comment.body", workflow)
        self.assertIn("TRIGGER_COMMENT_ID:", workflow)
        self.assertIn("TRIGGER_COMMENT_KIND:", workflow)
        self.assertIn("REVIEW_REQUEST:", workflow)

    def test_bot_comments_do_not_cancel_the_active_review(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")

        jobs_index = workflow.index("jobs:")
        review_index = workflow.index("  review:", jobs_index)
        concurrency_index = workflow.index("    concurrency:", review_index)

        self.assertGreater(concurrency_index, review_index)
        self.assertIn("      cancel-in-progress: true", workflow)
        self.assertNotIn("\nconcurrency:\n", workflow)


if __name__ == "__main__":
    unittest.main()
