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


if __name__ == "__main__":
    unittest.main()
