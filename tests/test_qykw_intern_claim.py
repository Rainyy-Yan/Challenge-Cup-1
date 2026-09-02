"""Tests for the strict qykw intern claim parser boundary."""

import unittest

from tools.qykw.intern_claim import (
    InternCommand,
    IssueCommentEvent,
    PullLifecycleEvent,
    normalize_issue_comment_event,
    normalize_pull_event,
    parse_closing_issue,
    parse_intern_command,
)


def issue_comment(body: str = "/intern-assign", **overrides: object) -> dict:
    payload = {
        "action": "created",
        "repository": {"full_name": "qiyuankaiwu/agentedu", "id": 42},
        "issue": {"number": 17, "repository_url": "https://api.github.com/repos/qiyuankaiwu/agentedu"},
        "comment": {"id": 101, "body": body, "user": {"login": "alice"}},
        "sender": {"login": "alice"},
    }
    payload.update(overrides)
    return payload


def pull_event(action: str = "opened", **overrides: object) -> dict:
    payload = {
        "action": action,
        "repository": {"full_name": "qiyuankaiwu/agentedu", "id": 42},
        "pull_request": {
            "number": 9,
            "base": {"repo": {"full_name": "qiyuankaiwu/agentedu"}},
        },
    }
    payload.update(overrides)
    return payload


class TestInternCommandParsing(unittest.TestCase):
    def test_accepts_exact_command_on_first_visible_line_with_later_explanation(self) -> None:
        self.assertIs(parse_intern_command("\n/intern-assign\n\nplease take this"), InternCommand.ASSIGN)
        self.assertEqual(parse_intern_command("/intern-status\nwhy"), InternCommand.STATUS)

    def test_rejects_arguments_and_non_visible_command_forms(self) -> None:
        for body in (
            "/intern-assign now",
            "> /intern-assign",
            "`/intern-assign`",
            "<!-- /intern-assign -->",
            "```\n/intern-assign\n```",
            "/intern-\u200bassign",
            "/intern-assign\u200b",
        ):
            with self.subTest(body=body):
                self.assertIsNone(parse_intern_command(body))


class TestInternEventNormalization(unittest.TestCase):
    def test_normalizes_issue_comment_event(self) -> None:
        event = normalize_issue_comment_event(issue_comment("/intern-unassign"))
        self.assertEqual(event, IssueCommentEvent("qiyuankaiwu/agentedu", 42, 17, 101, "alice", InternCommand.UNASSIGN))

    def test_rejects_invalid_issue_comment_context(self) -> None:
        cases = [
            {"action": "edited"},
            {"issue": {"number": 17, "pull_request": {}}},
            {"repository": {"full_name": "other/repo", "id": 42}},
            {"issue": {"number": 17, "repository_url": "https://api.github.com/repos/other/repo"}},
            {"repository": {"full_name": "qiyuankaiwu/agentedu", "id": True}},
            {"comment": {"id": True, "body": "/intern-assign", "user": {"login": "alice"}}},
            {"sender": {"login": "mallory"}},
        ]
        for changes in cases:
            payload = issue_comment()
            for key, value in changes.items():
                if key in {"issue", "repository", "comment", "sender"}:
                    payload[key] = value
                else:
                    payload[key] = value
            with self.subTest(changes=changes):
                self.assertIsNone(normalize_issue_comment_event(payload))

    def test_normalizes_allowed_pull_lifecycle_actions(self) -> None:
        event = normalize_pull_event(pull_event("closed"))
        self.assertEqual(event, PullLifecycleEvent("qiyuankaiwu/agentedu", 42, 9, "closed"))
        for action in ("opened", "edited", "ready_for_review", "reopened", "closed"):
            self.assertIsNotNone(normalize_pull_event(pull_event(action)))

    def test_rejects_invalid_pull_context(self) -> None:
        self.assertIsNone(normalize_pull_event(pull_event("synchronize")))
        self.assertIsNone(normalize_pull_event(pull_event("opened", repository={"full_name": "other/repo", "id": 42})))
        self.assertIsNone(normalize_pull_event(pull_event("opened", repository={"full_name": "qiyuankaiwu/agentedu", "id": True})))
        self.assertIsNone(normalize_pull_event(pull_event("opened", pull_request={"number": False})))


class TestClosingIssueParsing(unittest.TestCase):
    def test_accepts_one_visible_closes_reference(self) -> None:
        self.assertEqual(parse_closing_issue("Fixes this\nCloses #17"), 17)

    def test_rejects_ambiguous_or_unsafe_references(self) -> None:
        for body in (
            "Closes #17\nCloses #18",
            "Closes https://github.com/qiyuankaiwu/agentedu/issues/17",
            "Closes qiyuankaiwu/agentedu#17",
            "Closes #017",
            "[Closes #17](https://example.test/17)",
            "```\nCloses #17\n```",
        ):
            with self.subTest(body=body):
                self.assertIsNone(parse_closing_issue(body))


if __name__ == "__main__":
    unittest.main()
