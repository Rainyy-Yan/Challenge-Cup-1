"""Tests for the strict qykw intern claim parser boundary."""

import unittest
import json
import traceback
from collections.abc import Mapping

from tools.qykw.intern_claim import (
    InternClaimService,
    InternCommand,
    InternError,
    InternRecord,
    IssueComment,
    IssueCommentEvent,
    IssueSnapshot,
    PullLifecycleEvent,
    decode_marker,
    normalize_issue_comment_event,
    normalize_pull_event,
    parse_closing_issue,
    parse_intern_command,
    reduce_records,
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
        self.assertEqual(
            reduce_records(records, repository_id=42, repository="qiyuankaiwu/agentedu", issue_number=17),
            (earlier, later),
        )

    def test_reduction_rejects_duplicate_terminal_or_immutable_operation_drift(self) -> None:
        from tools.qykw.intern_claim import InternError, InternRecord, IssueComment, reduce_records
        record = self._record()
        terminal = InternRecord(42, "qiyuankaiwu/agentedu", 17, 101, "alice", "assign", "alice", None, "reconciled")
        cases = (
            (terminal, InternRecord(42, "qiyuankaiwu/agentedu", 17, 101, "alice", "assign", "alice", None, "failed")),
            (InternRecord(42, "qiyuankaiwu/agentedu", 17, 101, "bob", "assign", "bob", None, "pending"),),
            (InternRecord(42, "qiyuankaiwu/agentedu", 17, 101, "alice", "assign", "alice", 9, "pending"),),
        )
        for extras in cases:
            first = terminal if len(extras) == 2 else record
            comments = (
                *(IssueComment(index, "qykw", item.marker(), "now") for index, item in enumerate((first,) + extras, start=1)),
            )
            with self.subTest(extras=extras), self.assertRaisesRegex(InternError, "record_conflict"):
                reduce_records(comments, repository_id=42, repository="qiyuankaiwu/agentedu", issue_number=17)

    def test_reduction_discards_marker_bound_to_another_repository_id_or_issue(self) -> None:
        from tools.qykw.intern_claim import InternRecord, IssueComment, reduce_records
        for record in (
            InternRecord(43, "qiyuankaiwu/agentedu", 17, 101, "alice", "assign", "alice", None, "pending"),
            InternRecord(42, "qiyuankaiwu/other", 17, 101, "alice", "assign", "alice", None, "pending"),
            InternRecord(42, "qiyuankaiwu/agentedu", 18, 101, "alice", "assign", "alice", None, "pending"),
        ):
            with self.subTest(record=record):
                self.assertEqual(
                    reduce_records((IssueComment(1, "qykw", record.marker(), "now"),), repository_id=42,
                                   repository="qiyuankaiwu/agentedu", issue_number=17),
                    (),
                )


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
            {"id": 1, "user": {"login": "alice"}, "body": "one", "updated_at": "now", "issue_url": f"{api}/issues/17"},
        ], headers={"link": f'<{api}/issues/17/comments?per_page=100&page=2>; rel="next"'})
        transport.add("GET", f"{api}/issues/17/comments?per_page=100&page=2", [
            {"id": 2, "user": {"login": "qykw"}, "body": "two", "updated_at": "later", "issue_url": f"{api}/issues/17"},
        ])
        transport.add("GET", f"{api}/issues/9/comments?per_page=100", [
            {"id": 3, "user": {"login": "qykw"}, "body": "pull status", "updated_at": "later", "issue_url": f"{api}/issues/9"},
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

    def test_reads_only_qykw_laugh_reaction_across_fixed_comment_pages(self) -> None:
        transport = InternQueueTransport()
        api = f"{self.API}/repos/{self.REPOSITORY}"
        url = f"{api}/issues/comments/101/reactions?per_page=100"
        transport.add("GET", url, [
            {"id": 1, "user": {"login": "bob"}, "content": "laugh"},
            {"id": 2, "user": {"login": "qykw"}, "content": "heart"},
        ], headers={"link": f'<{url}&page=2>; rel="next"'})
        transport.add("GET", f"{url}&page=2", [
            {"id": 3, "user": {"login": "qykw"}, "content": "laugh"},
        ])

        self.assertTrue(self.gateway(transport).has_reaction(101))

        transport.assert_drained()

    def test_reaction_read_rejects_wrong_identity_shape_and_unsafe_pagination(self) -> None:
        from tools.qykw.intern_claim import InternError
        api = f"{self.API}/repos/{self.REPOSITORY}"
        gateway = self.gateway(InternQueueTransport())
        for invoke, code in (
            (lambda: gateway.has_reaction(False), "invalid_comment_id"),
            (lambda: gateway.has_reaction(101, actor="mallory"), "invalid_reaction_actor"),
            (lambda: gateway.has_reaction(101, content="heart"), "invalid_reaction_content"),
        ):
            with self.subTest(code=code), self.assertRaisesRegex(InternError, code):
                invoke()

        transport = InternQueueTransport()
        url = f"{api}/issues/comments/101/reactions?per_page=100"
        transport.add("GET", url, [], headers={
            "link": f'<{api}/issues/comments/102/reactions?per_page=100&page=2>; rel="next"',
        })
        with self.assertRaisesRegex(InternError, "unsafe_pagination"):
            self.gateway(transport).has_reaction(101)

        transport = InternQueueTransport()
        transport.add("GET", url, [{"id": 1, "user": {"login": "qykw!"}, "content": "laugh"}])
        with self.assertRaisesRegex(InternError, "invalid_login"):
            self.gateway(transport).has_reaction(101)

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
        transport.add("POST", f"{api}/issues/17/comments", {"id": 801, "issue_url": f"{api}/issues/17"})
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

    def test_rejects_changed_collection_query_parameters_before_following_links(self) -> None:
        from tools.qykw.intern_claim import InternError
        api = f"{self.API}/repos/{self.REPOSITORY}"
        for query in ("per_page=100&since=1&page=2", "per_page=100&page=3", "per_page=99&page=2", "per_page=100&page=2&page=2"):
            transport = InternQueueTransport()
            transport.add("GET", f"{api}/issues/17/comments?per_page=100", [], headers={"link": f'<{api}/issues/17/comments?{query}>; rel="next"'})
            with self.subTest(query=query), self.assertRaisesRegex(InternError, "unsafe_pagination"):
                self.gateway(transport).list_issue_comments(17)

    def test_redacts_token_and_body_from_gateway_errors_and_repr(self) -> None:
        from tools.qykw.intern_claim import InternError
        for transport_error in (RuntimeError("intern-secret private-body"), InternError("intern-secret private-body")):
            transport = InternQueueTransport()
            transport.add("GET", f"{self.API}/repos/{self.REPOSITORY}/issues/17", transport_error)
            gateway = self.gateway(transport)
            with self.subTest(transport_error=transport_error), self.assertRaises(InternError) as caught:
                gateway.get_issue(17)
            self.assertEqual(caught.exception.code, "transport_failed")
            self.assertNotIn("intern-secret", str(caught.exception))
            self.assertNotIn("private-body", repr(caught.exception))
            self.assertNotIn("intern-secret", repr(gateway))
            formatted = "".join(traceback.format_exception(caught.exception))
            self.assertNotIn("intern-secret", formatted)
            self.assertNotIn("private-body", formatted)

    def test_rejects_private_route_matrix_bypasses(self) -> None:
        from tools.qykw.intern_claim import InternError
        gateway = self.gateway(InternQueueTransport())
        api = f"{self.API}/repos/{self.REPOSITORY}"
        for method, url, body in (
            ("DELETE", f"{api}/issues/comments/101", None),
            ("POST", f"{api}/issues/17/labels", b'{"labels":["triage"]}'),
            ("PATCH", f"{api}/issues/17/labels/status%3Ain-progress", b'{}'),
            ("POST", f"{api}/issues/17/comments", b'{"body":"ok","extra":true}'),
            ("POST", f"{api}/issues/17/labels?ignored=1", b'{"labels":["status:in-progress"]}'),
            ("PATCH", f"{api}/issues/17?ignored=1", b'{"state":"closed"}'),
            ("DELETE", f"{api}/issues/17/labels/intern%3Aclaimable?ignored=1", None),
        ):
            with self.subTest(method=method, url=url), self.assertRaisesRegex(InternError, "invalid_request"):
                gateway._request(method, url, body=body)

    def test_rejects_comment_and_create_response_with_wrong_issue_locator(self) -> None:
        from tools.qykw.intern_claim import InternError
        api = f"{self.API}/repos/{self.REPOSITORY}"
        transport = InternQueueTransport()
        transport.add("GET", f"{api}/issues/17/comments?per_page=100", [
            {"id": 1, "user": {"login": "qykw"}, "body": "marker", "updated_at": "now", "issue_url": f"{api}/issues/18"},
        ])
        with self.assertRaisesRegex(InternError, "comment_repository_mismatch"):
            self.gateway(transport).list_issue_comments(17)
        transport = InternQueueTransport()
        self.identity(transport)
        transport.add("POST", f"{api}/issues/17/comments", {"id": 801, "issue_url": f"{api}/issues/18"})
        with self.assertRaisesRegex(InternError, "comment_repository_mismatch"):
            self.gateway(transport).create_comment(17, "status")

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


class InternMemoryGateway:
    """Stateful GitHub boundary fake with observable writes and failures."""

    def __init__(self, *, state: str = "open", labels: tuple[str, ...] = ("intern:claimable",),
                 assignees: tuple[str, ...] = ()) -> None:
        self.issue = IssueSnapshot(17, state, labels, assignees)
        self.comments: list[IssueComment] = []
        self.reactions: set[int] = set()
        self.writes: list[tuple[object, ...]] = []
        self.trace: list[tuple[object, ...]] = []
        self.failures: dict[str, list[str]] = {}
        self.next_comment_id = 1000
        self.authenticated = 0

    def command(self, comment_id: int, actor: str, command: str) -> None:
        self.comments.append(IssueComment(comment_id, actor, command, "now"))

    def record(self, record: InternRecord, *, comment_id: int | None = None, message: str = "seed") -> None:
        if comment_id is None:
            comment_id = self.next_comment_id
            self.next_comment_id += 1
        self.comments.append(IssueComment(comment_id, "qykw", f"{message}\n\n{record.marker()}", "now"))

    def fail_next(self, method: str, *, after: bool = False) -> None:
        self.failures.setdefault(method, []).append("after" if after else "before")

    def _begin_write(self, method: str, *args: object) -> str | None:
        self.writes.append((method, *args))
        self.trace.append((method, *args))
        failures = self.failures.get(method, [])
        mode = failures.pop(0) if failures else None
        if mode == "before":
            raise InternError("injected_failure")
        return mode

    @staticmethod
    def _finish_write(mode: str | None) -> None:
        if mode == "after":
            raise InternError("injected_failure")

    def assert_bot_identity(self, expected_login: str = "qykw") -> None:
        self.trace.append(("assert_bot_identity", expected_login))
        self.authenticated += 1
        if expected_login != "qykw":
            raise InternError("bot_identity_mismatch")

    def get_issue(self, issue_number: int) -> IssueSnapshot:
        self.assertEqualIssue(issue_number)
        self.trace.append(("get_issue", issue_number))
        return self.issue

    def list_issue_comments(self, issue_number: int) -> tuple[IssueComment, ...]:
        self.assertEqualIssue(issue_number)
        self.trace.append(("list_issue_comments", issue_number))
        return tuple(self.comments)

    def list_pull_comments(self, pull_number: int) -> tuple[IssueComment, ...]:
        return ()

    def get_pull(self, pull_number: int):
        raise AssertionError("pull reads are outside Task 3")

    def add_reaction(self, comment_id: int) -> None:
        mode = self._begin_write("add_reaction", comment_id)
        self.reactions.add(comment_id)
        self._finish_write(mode)

    def has_reaction(self, comment_id: int, actor: str = "qykw", content: str = "laugh") -> bool:
        self.trace.append(("has_reaction", comment_id, actor, content))
        if actor != "qykw" or content != "laugh":
            raise InternError("invalid_reaction_query")
        return comment_id in self.reactions

    def add_assignee(self, issue_number: int, login: str) -> None:
        self.assertEqualIssue(issue_number)
        mode = self._begin_write("add_assignee", login)
        if login not in self.issue.assignees:
            self.issue = IssueSnapshot(17, self.issue.state, self.issue.labels, self.issue.assignees + (login,))
        self._finish_write(mode)

    def remove_assignee(self, issue_number: int, login: str) -> None:
        self.assertEqualIssue(issue_number)
        mode = self._begin_write("remove_assignee", login)
        self.issue = IssueSnapshot(17, self.issue.state, self.issue.labels,
                                   tuple(value for value in self.issue.assignees if value != login))
        self._finish_write(mode)

    def add_label(self, issue_number: int, label: str) -> None:
        self.assertEqualIssue(issue_number)
        mode = self._begin_write("add_label", label)
        if label not in self.issue.labels:
            self.issue = IssueSnapshot(17, self.issue.state, self.issue.labels + (label,), self.issue.assignees)
        self._finish_write(mode)

    def remove_label(self, issue_number: int, label: str) -> None:
        self.assertEqualIssue(issue_number)
        mode = self._begin_write("remove_label", label)
        self.issue = IssueSnapshot(17, self.issue.state,
                                   tuple(value for value in self.issue.labels if value != label), self.issue.assignees)
        self._finish_write(mode)

    def create_comment(self, issue_number: int, body: str) -> int:
        self.assertEqualIssue(issue_number)
        mode = self._begin_write("create_comment", body)
        comment_id = self.next_comment_id
        self.next_comment_id += 1
        self.comments.append(IssueComment(comment_id, "qykw", body, "now"))
        self._finish_write(mode)
        return comment_id

    def update_comment(self, comment_id: int, body: str) -> None:
        mode = self._begin_write("update_comment", comment_id, body)
        for index, comment in enumerate(self.comments):
            if comment.comment_id == comment_id:
                self.comments[index] = IssueComment(comment_id, "qykw", body, "later")
                break
        else:
            raise AssertionError(f"unknown comment {comment_id}")
        self._finish_write(mode)

    def close_issue(self, issue_number: int) -> None:
        self.assertEqualIssue(issue_number)
        mode = self._begin_write("close_issue")
        self.issue = IssueSnapshot(17, "closed", self.issue.labels, self.issue.assignees)
        self._finish_write(mode)

    @staticmethod
    def assertEqualIssue(issue_number: int) -> None:
        if issue_number != 17:
            raise AssertionError(f"unexpected issue {issue_number}")

    def records(self) -> tuple[InternRecord, ...]:
        return reduce_records(tuple(self.comments), repository_id=42,
                              repository="qiyuankaiwu/agentedu", issue_number=17)

    def bot_body_for(self, trigger_comment_id: int) -> str:
        bodies = []
        for comment in self.comments:
            record = decode_marker(comment.body)
            if comment.author_login == "qykw" and record and record.trigger_comment_id == trigger_comment_id:
                bodies.append(comment.body)
        if len(bodies) != 1:
            raise AssertionError(f"expected one bot status for {trigger_comment_id}, got {len(bodies)}")
        return bodies[0]


class TestInternClaimService(unittest.TestCase):
    REPOSITORY = "qiyuankaiwu/agentedu"

    def event(self, comment_id: int, actor: str, command: InternCommand) -> IssueCommentEvent:
        return IssueCommentEvent(self.REPOSITORY, 42, 17, comment_id, actor, command)

    def service(self, gateway: InternMemoryGateway) -> InternClaimService:
        return InternClaimService(gateway)

    def test_assign_reconciles_each_mutation_in_order_and_preserves_unrelated_labels(self) -> None:
        gateway = InternMemoryGateway(labels=("intern:claimable", "area:docs"))
        gateway.command(101, "alice", "/intern-assign")

        outcome = self.service(gateway).handle_issue_event(self.event(101, "alice", InternCommand.ASSIGN))

        self.assertEqual(outcome.status, "reconciled")
        self.assertEqual(gateway.issue.assignees, ("alice",))
        self.assertEqual(gateway.issue.labels, ("area:docs", "status:in-progress"))
        self.assertEqual([write[0] for write in gateway.writes], [
            "add_reaction", "create_comment", "add_assignee", "remove_label", "add_label", "update_comment",
        ])
        self.assertEqual(gateway.records()[0].stage, "reconciled")
        self.assertIn("@alice", gateway.bot_body_for(101))

    def test_contenders_are_processed_by_numeric_comment_id_and_later_claim_gets_fixed_reply(self) -> None:
        gateway = InternMemoryGateway()
        gateway.command(202, "bob", "/intern-assign")
        gateway.command(101, "alice", "/intern-assign")

        outcome = self.service(gateway).handle_issue_event(self.event(202, "bob", InternCommand.ASSIGN))

        self.assertEqual(outcome.processed_comment_ids, (101, 202))
        self.assertEqual(gateway.issue.assignees, ("alice",))
        self.assertEqual([write[1] for write in gateway.writes if write[0] == "add_reaction"], [101, 202])
        self.assertTrue(gateway.bot_body_for(202).startswith("该任务已由 @alice 领取，请选择其他 Issue。"))

    def test_terminal_replay_performs_zero_duplicate_writes(self) -> None:
        gateway = InternMemoryGateway()
        gateway.command(101, "alice", "/intern-assign")
        service = self.service(gateway)
        service.handle_issue_event(self.event(101, "alice", InternCommand.ASSIGN))
        writes = tuple(gateway.writes)

        outcome = service.handle_issue_event(self.event(101, "alice", InternCommand.ASSIGN))

        self.assertEqual(outcome.status, "noop")
        self.assertEqual(gateway.writes, list(writes))

    def test_closed_blocked_and_not_claimable_issues_reject_without_issue_mutation(self) -> None:
        cases = (
            ("closed", ("intern:claimable",), "Issue 已关闭"),
            ("open", ("intern:claimable", "status:blocked"), "Issue 已阻塞"),
            ("open", ("area:docs",), "当前不可领取"),
        )
        for state, labels, expected in cases:
            gateway = InternMemoryGateway(state=state, labels=labels)
            gateway.command(101, "alice", "/intern-assign")
            before = gateway.issue
            with self.subTest(state=state, labels=labels):
                outcome = self.service(gateway).handle_issue_event(self.event(101, "alice", InternCommand.ASSIGN))
                self.assertEqual(outcome.status, "reconciled")
                self.assertEqual(gateway.issue, before)
                self.assertIn(expected, gateway.bot_body_for(101))
                self.assertFalse(any(write[0] in {"add_assignee", "remove_assignee", "add_label", "remove_label"}
                                     for write in gateway.writes))

    def test_unexpected_assignment_conflict_fails_closed_without_deleting_assignee(self) -> None:
        gateway = InternMemoryGateway(labels=("intern:claimable",), assignees=("bob",))
        gateway.command(101, "alice", "/intern-assign")

        outcome = self.service(gateway).handle_issue_event(self.event(101, "alice", InternCommand.ASSIGN))

        self.assertEqual(outcome.status, "conflict")
        self.assertEqual(gateway.issue.assignees, ("bob",))
        self.assertEqual(gateway.issue.labels, ("intern:claimable",))
        self.assertEqual(gateway.records()[0].stage, "conflict")
        self.assertIn("冲突", gateway.bot_body_for(101))

    def test_claimable_and_in_progress_label_conflict_has_zero_issue_mutations(self) -> None:
        gateway = InternMemoryGateway(labels=("intern:claimable", "status:in-progress", "area:docs"))
        gateway.command(101, "alice", "/intern-assign")
        before = gateway.issue

        outcome = self.service(gateway).handle_issue_event(self.event(101, "alice", InternCommand.ASSIGN))

        self.assertEqual(outcome.status, "conflict")
        self.assertEqual(gateway.issue, before)
        self.assertFalse(any(write[0] in {"add_assignee", "remove_assignee", "add_label", "remove_label"}
                             for write in gateway.writes))
        self.assertIn("冲突", gateway.bot_body_for(101))

    def test_reaction_failure_is_the_first_write_and_has_zero_followup_writes(self) -> None:
        gateway = InternMemoryGateway()
        gateway.command(101, "alice", "/intern-assign")
        gateway.fail_next("add_reaction")

        outcome = self.service(gateway).handle_issue_event(self.event(101, "alice", InternCommand.ASSIGN))

        self.assertEqual(outcome.status, "failed")
        self.assertEqual(gateway.writes, [("add_reaction", 101)])
        self.assertEqual(gateway.issue, IssueSnapshot(17, "open", ("intern:claimable",), ()))
        self.assertEqual(gateway.records(), ())

    def test_assign_and_comment_failures_recover_without_repeating_completed_mutations(self) -> None:
        cases = (
            ("create_comment", False),
            ("add_assignee", True),
            ("remove_label", True),
            ("add_label", True),
            ("update_comment", False),
        )
        for failed_method, after in cases:
            gateway = InternMemoryGateway()
            gateway.command(101, "alice", "/intern-assign")
            gateway.fail_next(failed_method, after=after)
            service = self.service(gateway)
            with self.subTest(failed_method=failed_method, after=after):
                first = service.handle_issue_event(self.event(101, "alice", InternCommand.ASSIGN))
                counts_after_failure = {
                    method: sum(write[0] == method for write in gateway.writes)
                    for method in ("add_assignee", "remove_label", "add_label")
                }
                second = service.handle_issue_event(self.event(101, "alice", InternCommand.ASSIGN))
                self.assertEqual(first.status, "failed")
                self.assertEqual(second.status, "reconciled")
                self.assertEqual(gateway.issue.assignees, ("alice",))
                self.assertNotIn("intern:claimable", gateway.issue.labels)
                self.assertIn("status:in-progress", gateway.issue.labels)
                for method, count in counts_after_failure.items():
                    if count:
                        self.assertEqual(sum(write[0] == method for write in gateway.writes), count)

    def test_comment_creation_failure_replay_never_posts_a_second_reaction(self) -> None:
        for after in (False, True):
            gateway = InternMemoryGateway()
            gateway.command(101, "alice", "/intern-assign")
            gateway.fail_next("create_comment", after=after)
            service = self.service(gateway)
            with self.subTest(after=after):
                first = service.handle_issue_event(self.event(101, "alice", InternCommand.ASSIGN))
                second = service.handle_issue_event(self.event(101, "alice", InternCommand.ASSIGN))
                self.assertEqual((first.status, second.status), ("failed", "reconciled"))
                self.assertEqual(sum(write[0] == "add_reaction" for write in gateway.writes), 1)

    def test_unknown_reaction_result_is_read_before_replay_and_never_posted_twice(self) -> None:
        gateway = InternMemoryGateway()
        gateway.command(101, "alice", "/intern-assign")
        gateway.fail_next("add_reaction", after=True)
        service = self.service(gateway)

        first = service.handle_issue_event(self.event(101, "alice", InternCommand.ASSIGN))
        second = service.handle_issue_event(self.event(101, "alice", InternCommand.ASSIGN))

        self.assertEqual((first.status, second.status), ("failed", "reconciled"))
        self.assertEqual(sum(write[0] == "add_reaction" for write in gateway.writes), 1)

    def test_release_failures_recover_for_owner_and_admin_without_repeating_mutations(self) -> None:
        for actor in ("alice", "xyh202131"):
            for failed_method in ("remove_assignee", "remove_label", "add_label"):
                gateway = InternMemoryGateway(labels=("status:in-progress",), assignees=("alice",))
                gateway.command(101, actor, "/intern-unassign")
                gateway.fail_next(failed_method, after=True)
                service = self.service(gateway)
                with self.subTest(actor=actor, failed_method=failed_method):
                    first = service.handle_issue_event(self.event(101, actor, InternCommand.UNASSIGN))
                    counts = {
                        method: sum(write[0] == method for write in gateway.writes)
                        for method in ("remove_assignee", "remove_label", "add_label")
                    }
                    second = service.handle_issue_event(self.event(101, actor, InternCommand.UNASSIGN))
                    self.assertEqual((first.status, second.status), ("failed", "reconciled"))
                    self.assertEqual(gateway.issue, IssueSnapshot(17, "open", ("intern:claimable",), ()))
                    for method, count in counts.items():
                        if count:
                            self.assertEqual(sum(write[0] == method for write in gateway.writes), count)

    def test_admin_release_replay_never_rebinds_frozen_claimant_to_unexpected_assignee(self) -> None:
        gateway = InternMemoryGateway(labels=("status:in-progress",), assignees=("alice",))
        gateway.command(101, "xyh202131", "/intern-unassign")
        gateway.fail_next("remove_assignee")
        service = self.service(gateway)
        first = service.handle_issue_event(self.event(101, "xyh202131", InternCommand.UNASSIGN))
        self.assertEqual(first.status, "failed")
        marker_claimants = [
            decode_marker(write[-1]).claimant_login
            for write in gateway.writes
            if write[0] in {"create_comment", "update_comment"} and decode_marker(write[-1]) is not None
        ]
        self.assertEqual(marker_claimants, ["alice"] * len(marker_claimants))
        self.assertEqual(gateway.records()[0].claimant_login, "alice")
        gateway.issue = IssueSnapshot(17, "open", ("status:in-progress",), ("bob",))
        prior_removals = sum(write[0] == "remove_assignee" for write in gateway.writes)

        second = service.handle_issue_event(self.event(101, "xyh202131", InternCommand.UNASSIGN))

        self.assertEqual(second.status, "conflict")
        self.assertEqual(gateway.issue.assignees, ("bob",))
        self.assertEqual(sum(write[0] == "remove_assignee" for write in gateway.writes), prior_removals)
        self.assertEqual(gateway.records()[0].claimant_login, "alice")

    def test_partial_release_replay_rechecks_active_pull_before_progress_label_mutation(self) -> None:
        for failed_method in ("remove_assignee", "add_label"):
            gateway = InternMemoryGateway(labels=("status:in-progress",), assignees=("alice",))
            gateway.command(101, "alice", "/intern-unassign")
            gateway.fail_next(failed_method, after=True)
            service = self.service(gateway)
            first = service.handle_issue_event(self.event(101, "alice", InternCommand.UNASSIGN))
            self.assertEqual(first.status, "failed")
            gateway.record(
                InternRecord(42, self.REPOSITORY, 17, 90, "alice", "pull", "alice", 9, "reconciled"),
                comment_id=900,
            )
            before = gateway.issue
            prior_label_writes = sum(write[0] in {"add_label", "remove_label"} for write in gateway.writes)

            with self.subTest(failed_method=failed_method):
                second = service.handle_issue_event(self.event(101, "alice", InternCommand.UNASSIGN))
                self.assertEqual(second.status, "reconciled")
                self.assertEqual(gateway.issue, before)
                self.assertEqual(
                    sum(write[0] in {"add_label", "remove_label"} for write in gateway.writes), prior_label_writes,
                )
                self.assertIn("不允许释放", gateway.bot_body_for(101))

    def test_unknown_mutation_result_is_reread_before_failed_marker_write(self) -> None:
        gateway = InternMemoryGateway()
        gateway.command(101, "alice", "/intern-assign")
        gateway.fail_next("add_assignee", after=True)

        self.service(gateway).handle_issue_event(self.event(101, "alice", InternCommand.ASSIGN))

        mutation_index = next(index for index, item in enumerate(gateway.trace) if item[0] == "add_assignee")
        later = gateway.trace[mutation_index + 1:]
        self.assertEqual(later[0][0], "get_issue")
        self.assertLess(
            next(index for index, item in enumerate(later) if item[0] == "list_issue_comments"),
            next(index for index, item in enumerate(later) if item[0] == "update_comment"),
        )

    def test_release_rejects_unauthorized_actor_without_issue_mutation(self) -> None:
        gateway = InternMemoryGateway(labels=("status:in-progress", "area:docs"), assignees=("alice",))
        gateway.command(101, "mallory", "/intern-unassign")
        before = gateway.issue

        outcome = self.service(gateway).handle_issue_event(self.event(101, "mallory", InternCommand.UNASSIGN))

        self.assertEqual(outcome.status, "reconciled")
        self.assertEqual(gateway.issue, before)
        self.assertIn("无权释放", gateway.bot_body_for(101))

    def test_first_release_without_assignee_or_claimable_state_fails_closed(self) -> None:
        gateway = InternMemoryGateway(labels=("area:docs",))
        gateway.command(101, "mallory", "/intern-unassign")
        before = gateway.issue

        outcome = self.service(gateway).handle_issue_event(self.event(101, "mallory", InternCommand.UNASSIGN))

        self.assertEqual(outcome.status, "conflict")
        self.assertEqual(gateway.issue, before)
        self.assertFalse(any(write[0] in {"add_assignee", "remove_assignee", "add_label", "remove_label"}
                             for write in gateway.writes))
        self.assertIn("冲突", gateway.bot_body_for(101))

    def test_first_release_without_assignee_cannot_repair_in_progress_label(self) -> None:
        gateway = InternMemoryGateway(labels=("status:in-progress", "area:docs"))
        gateway.command(101, "mallory", "/intern-unassign")
        before = gateway.issue

        outcome = self.service(gateway).handle_issue_event(self.event(101, "mallory", InternCommand.UNASSIGN))

        self.assertEqual(outcome.status, "conflict")
        self.assertEqual(gateway.issue, before)
        self.assertFalse(any(write[0] in {"add_assignee", "remove_assignee", "add_label", "remove_label"}
                             for write in gateway.writes))
        self.assertIn("冲突", gateway.bot_body_for(101))

    def test_owner_and_exact_admin_can_release(self) -> None:
        for actor in ("alice", "xyh202131"):
            gateway = InternMemoryGateway(labels=("status:in-progress", "area:docs"), assignees=("alice",))
            gateway.command(101, actor, "/intern-unassign")
            with self.subTest(actor=actor):
                outcome = self.service(gateway).handle_issue_event(self.event(101, actor, InternCommand.UNASSIGN))
                self.assertEqual(outcome.status, "reconciled")
                self.assertEqual(gateway.issue.assignees, ())
                self.assertEqual(gateway.issue.labels, ("area:docs", "intern:claimable"))

    def test_release_rejects_in_review_or_active_pull(self) -> None:
        cases = ("review", "pull")
        for case in cases:
            labels = ("status:in-review",) if case == "review" else ("status:in-progress",)
            gateway = InternMemoryGateway(labels=labels, assignees=("alice",))
            if case == "pull":
                gateway.record(InternRecord(42, self.REPOSITORY, 17, 90, "alice", "pull", "alice", 9, "reconciled"),
                               comment_id=900)
            gateway.command(101, "alice", "/intern-unassign")
            before = gateway.issue
            with self.subTest(case=case):
                outcome = self.service(gateway).handle_issue_event(self.event(101, "alice", InternCommand.UNASSIGN))
                self.assertEqual(outcome.status, "reconciled")
                self.assertEqual(gateway.issue, before)
                self.assertIn("不允许释放", gateway.bot_body_for(101))

    def test_repeated_release_is_acknowledged_once_without_issue_mutation(self) -> None:
        gateway = InternMemoryGateway(labels=("intern:claimable", "area:docs"))
        gateway.command(101, "alice", "/intern-unassign")
        service = self.service(gateway)

        first = service.handle_issue_event(self.event(101, "alice", InternCommand.UNASSIGN))
        writes = tuple(gateway.writes)
        second = service.handle_issue_event(self.event(101, "alice", InternCommand.UNASSIGN))

        self.assertEqual((first.status, second.status), ("reconciled", "noop"))
        self.assertEqual(gateway.issue, IssueSnapshot(17, "open", ("intern:claimable", "area:docs"), ()))
        self.assertEqual(gateway.writes, list(writes))
        self.assertIn("已处于可领取状态", gateway.bot_body_for(101))

    def test_status_reports_claimable_in_progress_in_review_and_conflict_read_only(self) -> None:
        cases = (
            (("intern:claimable",), (), "可领取"),
            (("status:in-progress",), ("alice",), "由 @alice 处理中"),
            (("status:in-review",), ("alice",), "审查中"),
            (("status:in-progress",), ("alice", "bob"), "冲突"),
        )
        mutation_names = {"add_assignee", "remove_assignee", "add_label", "remove_label", "close_issue"}
        for labels, assignees, expected in cases:
            gateway = InternMemoryGateway(labels=labels, assignees=assignees)
            gateway.command(101, "viewer", "/intern-status")
            before = gateway.issue
            with self.subTest(labels=labels, assignees=assignees):
                outcome = self.service(gateway).handle_issue_event(self.event(101, "viewer", InternCommand.STATUS))
                self.assertIn(outcome.status, {"reconciled", "conflict"})
                self.assertEqual(gateway.issue, before)
                self.assertIn(expected, gateway.bot_body_for(101))
                self.assertFalse(any(write[0] in mutation_names for write in gateway.writes))
