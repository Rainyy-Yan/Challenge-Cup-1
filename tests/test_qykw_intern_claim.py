"""Tests for the strict qykw intern claim parser boundary."""

import unittest
import json
from collections.abc import Mapping

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

    def test_rejects_lazy_quote_continuation_and_indented_code(self) -> None:
        self.assertIsNone(parse_intern_command("> quoted context\n/intern-assign"))
        self.assertIsNone(parse_intern_command("    /intern-assign"))


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
            "Closes #17\nsee #18",
            "Closes #17suffix",
            "Closes #17 extra #18",
            "```\nCloses #17\n```",
        ):
            with self.subTest(body=body):
                self.assertIsNone(parse_closing_issue(body))


if __name__ == "__main__":
    unittest.main()


class InternQueueTransport:
    """Literal transport queue; unexpected routes are test failures."""

    def __init__(self) -> None:
        self.queue: list[tuple[str, str, int, Mapping[str, str], object]] = []
        self.calls: list[tuple[str, str, Mapping[str, str], bytes | None]] = []

    def add(self, method: str, url: str, payload: object, *, status: int = 200,
            headers: Mapping[str, str] | None = None) -> None:
        response_headers = {"content-type": "application/json"}
        if headers is not None:
            response_headers.update(headers)
        self.queue.append((method, url, status, response_headers, payload))

    def __call__(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> tuple[int, Mapping[str, str], bytes]:
        self.calls.append((method, url, headers, body))
        if not self.queue:
            raise AssertionError(f"unexpected transport request: {method} {url}")
        expected_method, expected_url, status, response_headers, payload = self.queue.pop(0)
        if (method, url) != (expected_method, expected_url):
            raise AssertionError(f"expected {expected_method} {expected_url}, got {method} {url}")
        if isinstance(payload, BaseException):
            raise payload
        response = payload if isinstance(payload, bytes) else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return status, response_headers, response

    def assert_drained(self) -> None:
        if self.queue:
            raise AssertionError(f"unconsumed responses: {self.queue!r}")


class TestInternMarkerCodec(unittest.TestCase):
    def _record(self):
        from tools.qykw.intern_claim import InternRecord
        return InternRecord(
            repository_id=42, repository="qiyuankaiwu/agentedu", issue_number=17,
            trigger_comment_id=101, actor_login="alice", operation="assign",
            claimant_login="alice", pull_number=None, stage="pending",
        )

    def test_round_trips_only_the_versioned_fixed_marker_shape(self) -> None:
        from tools.qykw.intern_claim import decode_marker, encode_marker
        marker = encode_marker(self._record())
        self.assertEqual(
            marker,
            '<!-- qykw-intern:v1 {"actor_login":"alice","claimant_login":"alice","issue_number":17,"operation":"assign","pull_number":null,"repository":"qiyuankaiwu/agentedu","repository_id":42,"stage":"pending","trigger_comment_id":101} -->',
        )
        self.assertEqual(decode_marker(marker), self._record())
        for body in (
            marker.replace('"stage":"pending",', '"stage":"pending","extra":true,'),
            marker.replace('"stage":"pending"', '"stage":"pending","stage":"done"'),
            marker.replace('qykw-intern:v1', 'qykw-intern:v2'),
        ):
            with self.subTest(body=body):
                self.assertIsNone(decode_marker(body))
        self.assertEqual(decode_marker("状态更新。\n\n" + marker), self._record())

    def test_rejects_mismatched_repository_and_invalid_immutable_identity(self) -> None:
        from tools.qykw.intern_claim import InternError, InternRecord, decode_marker, encode_marker
        marker = encode_marker(self._record())
        self.assertIsNone(decode_marker(marker, repository="other/repository"))
        with self.assertRaisesRegex(InternError, "invalid_operation"):
            InternRecord(42, "qiyuankaiwu/agentedu", 17, 101, "alice", "erase", "alice", None, "pending")
        with self.assertRaisesRegex(InternError, "invalid_operation"):
            InternRecord(42, "qiyuankaiwu/agentedu", 17, 101, "alice", "assign", "bob", None, "pending")

    def test_reduces_trusted_records_deterministically_by_comment_id(self) -> None:
        from tools.qykw.intern_claim import IssueComment, InternRecord, reduce_records
        earlier = self._record()
        later = InternRecord(42, "qiyuankaiwu/agentedu", 17, 102, "alice", "assign", "alice", None, "reconciled")
        records = (
            IssueComment(32, "qykw", later.marker(), "now"),
            IssueComment(31, "mallory", earlier.marker(), "now"),
            IssueComment(30, "qykw", earlier.marker(), "now"),
        )
        self.assertEqual(reduce_records(records), (earlier, later))


class TestInternGitHubGateway(unittest.TestCase):
    API = "https://api.github.test"
    REPOSITORY = "qiyuankaiwu/agentedu"

    def gateway(self, transport: InternQueueTransport):
        from tools.qykw.intern_claim import HttpInternGateway
        return HttpInternGateway(self.API, self.REPOSITORY, "intern-secret", transport=transport)

    def identity(self, transport: InternQueueTransport) -> None:
        transport.add("GET", f"{self.API}/user", {"login": "qykw", "id": 1})

    def test_reads_issue_pull_and_all_conversation_comment_pages(self) -> None:
        transport = InternQueueTransport()
        api = f"{self.API}/repos/{self.REPOSITORY}"
        transport.add("GET", f"{api}/issues/17", {
            "number": 17, "state": "open", "labels": [{"name": "intern:claimable"}],
            "assignees": [{"login": "alice"}],
        })
        transport.add("GET", f"{api}/issues/17/comments?per_page=100", [
            {"id": 1, "user": {"login": "alice"}, "body": "one", "updated_at": "now"},
        ], headers={"link": f'<{api}/issues/17/comments?per_page=100&page=2>; rel="next"'})
        transport.add("GET", f"{api}/issues/17/comments?per_page=100&page=2", [
            {"id": 2, "user": {"login": "qykw"}, "body": "two", "updated_at": "later"},
        ])
        transport.add("GET", f"{api}/issues/9/comments?per_page=100", [
            {"id": 3, "user": {"login": "qykw"}, "body": "pull status", "updated_at": "later"},
        ])
        transport.add("GET", f"{api}/pulls/9", {
            "number": 9, "state": "open", "merged": False, "user": {"login": "alice"}, "body": "Closes #17",
            "base": {"repo": {"full_name": self.REPOSITORY}},
        })
        gateway = self.gateway(transport)
        issue = gateway.get_issue(17)
        self.assertEqual((issue.number, issue.labels, issue.assignees), (17, ("intern:claimable",), ("alice",)))
        self.assertEqual([item.comment_id for item in gateway.list_issue_comments(17)], [1, 2])
        self.assertEqual([item.comment_id for item in gateway.list_pull_comments(9)], [3])
        pull = gateway.get_pull(9)
        self.assertEqual((pull.number, pull.author_login, pull.body), (9, "alice", "Closes #17"))
        transport.assert_drained()

    def test_writes_only_the_fixed_reaction_assignee_label_comment_and_close_routes(self) -> None:
        transport = InternQueueTransport()
        api = f"{self.API}/repos/{self.REPOSITORY}"
        self.identity(transport)
        transport.add("POST", f"{api}/issues/comments/101/reactions", {})
        self.identity(transport)
        transport.add("POST", f"{api}/issues/17/assignees", {})
        self.identity(transport)
        transport.add("DELETE", f"{api}/issues/17/assignees", {})
        self.identity(transport)
        transport.add("POST", f"{api}/issues/17/labels", {})
        self.identity(transport)
        transport.add("DELETE", f"{api}/issues/17/labels/intern%3Aclaimable", {})
        self.identity(transport)
        transport.add("POST", f"{api}/issues/17/comments", {"id": 801})
        self.identity(transport)
        transport.add("PATCH", f"{api}/issues/comments/801", {})
        self.identity(transport)
        transport.add("PATCH", f"{api}/issues/17", {})
        gateway = self.gateway(transport)
        gateway.add_reaction(101)
        gateway.add_assignee(17, "alice")
        gateway.remove_assignee(17, "alice")
        gateway.add_label(17, "status:in-progress")
        gateway.remove_label(17, "intern:claimable")
        self.assertEqual(gateway.create_comment(17, "status"), 801)
        gateway.update_comment(801, "updated")
        gateway.close_issue(17)
        self.assertEqual(json.loads(transport.calls[1][3].decode("utf-8")), {"content": "laugh"})  # type: ignore[union-attr]
        self.assertEqual(json.loads(transport.calls[3][3].decode("utf-8")), {"assignees": ["alice"]})  # type: ignore[union-attr]
        self.assertEqual(json.loads(transport.calls[7][3].decode("utf-8")), {"labels": ["status:in-progress"]})  # type: ignore[union-attr]
        self.assertTrue(all(call[2]["Authorization"] == "Bearer intern-secret" for call in transport.calls))
        transport.assert_drained()

    def test_fails_closed_for_unsafe_pagination_oversize_strict_json_and_arbitrary_writes(self) -> None:
        from tools.qykw.intern_claim import InternError
        api = f"{self.API}/repos/{self.REPOSITORY}"
        for link in (f'<https://evil.example/issues/17/comments?page=2>; rel="next"', f'<{api}/issues/18/comments?page=2>; rel="next"'):
            transport = InternQueueTransport()
            transport.add("GET", f"{api}/issues/17/comments?per_page=100", [], headers={"link": link})
            with self.subTest(link=link), self.assertRaisesRegex(InternError, "unsafe_pagination"):
                self.gateway(transport).list_issue_comments(17)
        transport = InternQueueTransport()
        transport.add("GET", f"{api}/issues/17", b"x" * (2 * 1024 * 1024 + 1))
        with self.assertRaisesRegex(InternError, "invalid_response"):
            self.gateway(transport).get_issue(17)
        transport = InternQueueTransport()
        transport.add("GET", f"{api}/issues/17", b'{"number":17,"number":18}')
        with self.assertRaisesRegex(InternError, "invalid_response"):
            self.gateway(transport).get_issue(17)
        gateway = self.gateway(InternQueueTransport())
        with self.assertRaisesRegex(InternError, "invalid_label"):
            gateway.add_label(17, "triage")
        self.assertFalse(hasattr(gateway, "delete"))
        self.assertFalse(hasattr(gateway, "request"))

    def test_redacts_token_and_body_from_gateway_errors_and_repr(self) -> None:
        from tools.qykw.intern_claim import InternError
        transport = InternQueueTransport()
        transport.add("GET", f"{self.API}/repos/{self.REPOSITORY}/issues/17", RuntimeError("intern-secret private-body"))
        gateway = self.gateway(transport)
        with self.assertRaises(InternError) as caught:
            gateway.get_issue(17)
        self.assertEqual(caught.exception.code, "transport_failed")
        self.assertNotIn("intern-secret", str(caught.exception))
        self.assertNotIn("private-body", repr(caught.exception))
        self.assertNotIn("intern-secret", repr(gateway))

    def test_rejects_invalid_write_identifiers_before_authentication_or_transport(self) -> None:
        from tools.qykw.intern_claim import InternError
        transport = InternQueueTransport()
        gateway = self.gateway(transport)
        for invoke, code in (
            (lambda: gateway.add_reaction(False), "invalid_comment_id"),
            (lambda: gateway.add_assignee(17, "alice!"), "invalid_login"),
            (lambda: gateway.remove_assignee(0, "alice"), "invalid_issue_number"),
            (lambda: gateway.create_comment(17, ""), "invalid_write_body"),
            (lambda: gateway.update_comment(0, "body"), "invalid_comment_id"),
            (lambda: gateway.close_issue(0), "invalid_issue_number"),
        ):
            with self.subTest(code=code), self.assertRaisesRegex(InternError, code):
                invoke()
        self.assertEqual(transport.calls, [])
