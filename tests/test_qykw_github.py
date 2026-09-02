from __future__ import annotations

import json
import unittest
from collections.abc import Mapping

from tools.qykw.domain import (
    CommandMode,
    CommandName,
    CommandRequest,
    DiffSide,
    InlineComment,
    RunContext,
    TriggerRef,
)
from tools.qykw.github import GitHubError, GitHubGateway, GitHubPullSnapshot, HttpGitHubGateway


HEAD_SHA = "a" * 40
BASE_SHA = "b" * 40
HEAD_TREE_SHA = "c" * 40
BASE_TREE_SHA = "d" * 40


class FakeTransport:
    def __init__(self, routes: Mapping[str, object]) -> None:
        self.routes = dict(routes)
        self.calls: list[tuple[str, str, object | None]] = []

    def __call__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> tuple[int, Mapping[str, str], bytes]:
        self.calls.append((method, url, body))
        response = self.routes.get(f"{method} {url}")
        if response is None:
            raise AssertionError(f"unexpected request: {method} {url}")
        if isinstance(response, list):
            response = response.pop(0)
        status, response_headers, payload = response  # type: ignore[misc]
        return status, response_headers, json.dumps(payload).encode("utf-8")


def response(payload: object, *, status: int = 200, link: str | None = None) -> tuple[int, Mapping[str, str], object]:
    headers: dict[str, str] = {"content-type": "application/json"}
    if link is not None:
        headers["link"] = link
    return status, headers, payload


def pull(*, head: str = HEAD_SHA, state: str = "open") -> dict[str, object]:
    return {
        "number": 53,
        "state": state,
        "draft": False,
        "title": "Title",
        "body": "Body",
        "head": {"sha": head, "ref": "feature", "repo": {"full_name": "source/repo"}},
        "base": {"sha": BASE_SHA, "ref": "main", "repo": {"full_name": "owner/repo"}},
    }


def changed(path: str, *, status: str = "modified", previous: str | None = None) -> dict[str, object]:
    value: dict[str, object] = {
        "filename": path,
        "status": status,
        "sha": f"head-{path}",
        "blob_url": "https://github.com/untrusted/blob",
        "additions": 1,
        "deletions": 1,
        "patch": "@@ -1 +1 @@\n-old\n+new",
    }
    if previous is not None:
        value["previous_filename"] = previous
    return value


def gateway(routes: Mapping[str, object]) -> tuple[HttpGitHubGateway, FakeTransport]:
    transport = FakeTransport(routes)
    return (
        HttpGitHubGateway(
            api_url="https://api.github.test",
            repository="owner/repo",
            read_token="read-secret",
            review_token="review-secret",
            transport=transport,
        ),
        transport,
    )


def run_context(*, head: str = HEAD_SHA) -> RunContext:
    return RunContext(
        run_id="QY-PR53-X",
        idempotency_key="idempotency",
        repository_id=7,
        repository="owner/repo",
        pr_number=53,
        event_name="pull_request",
        event_action="opened",
        source_repository="source/repo",
        source_head_sha=head,
        target_base_sha=BASE_SHA,
        target_base_ref="main",
        command=CommandRequest(CommandName.REVIEW, "", CommandMode.READ_ONLY),
        trigger_actor="xyh202131",
    )


class TestGitHubGateway(unittest.TestCase):
    def test_protocol_has_only_allowlisted_methods(self) -> None:
        self.assertEqual(GitHubGateway.get_pull_snapshot.__annotations__["return"], "GitHubPullSnapshot")
        self.assertEqual(HttpGitHubGateway.get_pull_snapshot.__annotations__["return"], "GitHubPullSnapshot")
        self.assertTrue(hasattr(GitHubPullSnapshot, "omissions"))
        self.assertEqual(
            {name for name in GitHubGateway.__dict__ if not name.startswith("_")},
            {
                "get_pull_ref", "get_pull_snapshot", "get_head_sha", "get_actor_permission",
                "get_authenticated_user", "assert_bot_identity", "try_add_reaction",
                "list_issue_comments", "list_review_comments", "list_changed_files", "list_check_runs",
                "get_file_for_run", "get_default_branch_rules", "create_issue_comment",
                "update_issue_comment", "create_review",
            },
        )
        forbidden = ("approve", "merge", "delete", "force", "setting", "secret", "fetch", "url")
        self.assertFalse(any(word in name.lower() for name in GitHubGateway.__dict__ if not name.startswith("_") for word in forbidden))
        implementation_methods = {
            name for name, value in HttpGitHubGateway.__dict__.items()
            if callable(value) and not name.startswith("_")
        }
        self.assertEqual(implementation_methods, {
            "get_pull_ref", "get_pull_snapshot", "get_head_sha", "get_actor_permission",
            "get_authenticated_user", "assert_bot_identity", "try_add_reaction",
            "list_issue_comments", "list_review_comments", "list_changed_files", "list_check_runs",
            "get_file_for_run", "get_default_branch_rules", "create_issue_comment",
            "update_issue_comment", "create_review",
        })

    def test_reads_all_link_pages_without_first_hundred_truncation(self) -> None:
        api = "https://api.github.test/repos/owner/repo"
        issue_1 = [{"id": number, "user": {"login": "user"}, "body": "ok", "updated_at": "now"} for number in range(1, 101)]
        issue_2 = [{"id": 101, "user": {"login": "user"}, "body": "late", "updated_at": "now"}]
        file_1 = [changed(f"file-{number}.py") for number in range(100)]
        file_2 = [changed("late.py")]
        gw, _ = gateway({
            f"GET {api}/issues/53/comments?per_page=100": response(issue_1, link=f'<{api}/issues/53/comments?per_page=100&page=2>; rel="next"'),
            f"GET {api}/issues/53/comments?per_page=100&page=2": response(issue_2),
            f"GET {api}/pulls/53/files?per_page=100": response(file_1, link=f'<{api}/pulls/53/files?per_page=100&page=2>; rel="next"'),
            f"GET {api}/pulls/53/files?per_page=100&page=2": response(file_2),
        })
        self.assertEqual(len(gw.list_issue_comments(53)), 101)
        self.assertEqual(len(gw.list_changed_files(53)), 101)

    def test_link_pagination_rejects_cross_origin_loop_and_malformed_next(self) -> None:
        api = "https://api.github.test/repos/owner/repo"
        for link in (
            '<https://evil.example/comments?page=2>; rel="next"',
            f'<{api}/issues/53/comments?per_page=100>; rel="next"',
            'not-a-link',
        ):
            gw, _ = gateway({f"GET {api}/issues/53/comments?per_page=100": response([], link=link)})
            with self.assertRaisesRegex(GitHubError, "unsafe_pagination"):
                gw.list_issue_comments(53)

    def test_identity_mismatch_has_zero_reaction_comment_and_review_writes(self) -> None:
        api = "https://api.github.test/repos/owner/repo"
        routes = {
            "GET https://api.github.test/user": response({"login": "not-qykw", "id": 2}),
            "GET https://api.github.test/repos/owner/repo/pulls/53": response(pull()),
        }
        gw, transport = gateway(routes)
        with self.assertRaisesRegex(GitHubError, "bot_identity_mismatch"):
            gw.try_add_reaction(TriggerRef("issue_comment", 11))
        with self.assertRaisesRegex(GitHubError, "bot_identity_mismatch"):
            gw.create_issue_comment(53, "hello")
        with self.assertRaisesRegex(GitHubError, "bot_identity_mismatch"):
            gw.create_review(53, head_sha=HEAD_SHA, body="review", comments=())
        self.assertTrue(all(method == "GET" for method, _, _ in transport.calls))

    def test_reaction_failure_is_non_blocking_warning_after_identity_check(self) -> None:
        api = "https://api.github.test/repos/owner/repo"
        gw, transport = gateway({
            "GET https://api.github.test/user": response({"login": "qykw", "id": 1}),
            f"POST {api}/issues/comments/11/reactions": response({"message": "no"}, status=500),
        })
        result = gw.try_add_reaction(TriggerRef("issue_comment", 11))
        self.assertEqual(result.warning_code, "reaction_failed")
        self.assertEqual([call[0] for call in transport.calls], ["GET", "POST"])

    def test_get_pull_ref_rejects_closed_and_wrong_repository_payloads(self) -> None:
        api = "https://api.github.test/repos/owner/repo/pulls/53"
        for payload in (pull(state="closed"), {**pull(), "base": {"sha": BASE_SHA, "ref": "main", "repo": {"full_name": "other/repo"}}}):
            gw, _ = gateway({f"GET {api}": response(payload)})
            with self.assertRaisesRegex(GitHubError, "pull_(not_open|repository_mismatch)"):
                gw.get_pull_ref(53)

    def test_snapshot_rejects_stale_run_context_before_collecting(self) -> None:
        api = "https://api.github.test/repos/owner/repo/pulls/53"
        gw, transport = gateway({f"GET {api}": response(pull(head="e" * 40))})
        with self.assertRaisesRegex(GitHubError, "stale_pull_ref"):
            gw.get_pull_snapshot(53, run=run_context(head=HEAD_SHA))
        self.assertEqual(len(transport.calls), 1)

    def test_snapshot_requires_a_fixed_run_context_before_collecting(self) -> None:
        gw, transport = gateway({})
        with self.assertRaisesRegex(GitHubError, "invalid_run_context"):
            gw.get_pull_snapshot(53, run=None)  # type: ignore[arg-type]
        self.assertEqual(transport.calls, [])

    def test_snapshot_rechecks_pull_ref_after_collection_and_rejects_synchronize_race(self) -> None:
        api = "https://api.github.test/repos/owner/repo"
        routes = {
            f"GET {api}/pulls/53": [response(pull()), response(pull(head="e" * 40))],
            f"GET {api}/pulls/53/files?per_page=100": response([]),
            f"GET {api}/git/commits/{BASE_SHA}": response({"sha": BASE_SHA, "tree": {"sha": BASE_TREE_SHA}}),
            f"GET https://api.github.test/repos/source/repo/git/commits/{HEAD_SHA}": response({"sha": HEAD_SHA, "tree": {"sha": HEAD_TREE_SHA}}),
            f"GET {api}/git/trees/{BASE_TREE_SHA}?recursive=1": response({"tree": []}),
            f"GET https://api.github.test/repos/source/repo/git/trees/{HEAD_TREE_SHA}?recursive=1": response({"tree": []}),
            f"GET {api}/commits/{HEAD_SHA}/check-runs?per_page=100": response({"check_runs": []}),
            f"GET {api}/contents/AGENTS.md?ref=main": response({"message": "missing"}, status=404),
            f"GET {api}/contents/.github/qykw.toml?ref=main": response({"message": "missing"}, status=404),
        }
        gw, transport = gateway(routes)
        with self.assertRaisesRegex(GitHubError, "stale_pull_ref"):
            gw.get_pull_snapshot(53, run=run_context())
        self.assertEqual([url for _, url, _ in transport.calls].count(f"{api}/pulls/53"), 2)

    def test_snapshot_covers_ordinary_missing_renamed_and_deleted_data_on_both_sides(self) -> None:
        api = "https://api.github.test/repos/owner/repo"
        files = [
            changed("deleted.py", status="removed"),
            changed("new.py", status="renamed", previous="old.py"),
            changed("missing.py"),
        ]
        routes = {
            f"GET {api}/pulls/53": [response(pull()), response(pull())],
            f"GET {api}/pulls/53/files?per_page=100": response(files),
            f"GET {api}/git/commits/{BASE_SHA}": response({"sha": BASE_SHA, "tree": {"sha": BASE_TREE_SHA}}),
            f"GET https://api.github.test/repos/source/repo/git/commits/{HEAD_SHA}": response({"sha": HEAD_SHA, "tree": {"sha": HEAD_TREE_SHA}}),
            f"GET {api}/git/trees/{BASE_TREE_SHA}?recursive=1": response({"tree": [
                {"path": "deleted.py", "mode": "100644", "type": "blob", "sha": "base-delete"},
                {"path": "old.py", "mode": "100644", "type": "blob", "sha": "base-old"},
            ]}),
            f"GET https://api.github.test/repos/source/repo/git/trees/{HEAD_TREE_SHA}?recursive=1": response({"tree": [
                {"path": "new.py", "mode": "100755", "type": "blob", "sha": "head-new"},
            ]}),
            f"GET {api}/contents/deleted.py?ref={BASE_SHA}": response({"sha": "base-delete", "content": "b2xk", "encoding": "base64"}),
            f"GET {api}/contents/old.py?ref={BASE_SHA}": response({"sha": "base-old", "content": "b2xk", "encoding": "base64"}),
            f"GET https://api.github.test/repos/source/repo/contents/new.py?ref={HEAD_SHA}": response({"sha": "head-new", "content": "bmV3", "encoding": "base64"}),
            f"GET {api}/contents/missing.py?ref={BASE_SHA}": response({"message": "missing"}, status=404),
            f"GET https://api.github.test/repos/source/repo/contents/missing.py?ref={HEAD_SHA}": response({"message": "missing"}, status=404),
            f"GET {api}/commits/{HEAD_SHA}/check-runs?per_page=100": response({"check_runs": []}),
            f"GET {api}/contents/AGENTS.md?ref=main": response({"message": "missing"}, status=404),
            f"GET {api}/contents/.github/qykw.toml?ref=main": response({"message": "missing"}, status=404),
        }
        gw, _ = gateway(routes)
        snapshot = gw.get_pull_snapshot(53, run=run_context())
        deleted, renamed, missing = snapshot.changed_files
        self.assertEqual(deleted.base_content, "old")
        self.assertIsNone(deleted.head_content)
        self.assertEqual(deleted.base_mode, "100644")
        self.assertIsNone(deleted.head_mode)
        self.assertEqual(renamed.previous_path, "old.py")
        self.assertEqual(renamed.base_content, "old")
        self.assertEqual(renamed.head_content, "new")
        self.assertEqual(renamed.base_mode, "100644")
        self.assertEqual(renamed.head_mode, "100755")
        self.assertIsNone(missing.base_content)
        self.assertIsNone(missing.head_content)
        self.assertIn("base_content_missing:missing.py", snapshot.omissions)
        self.assertIn("head_mode_missing:missing.py", snapshot.omissions)
        self.assertIn("trusted_rule_missing:AGENTS.md", snapshot.omissions)

    def test_added_file_has_only_fixed_head_content_and_mode(self) -> None:
        api = "https://api.github.test/repos/owner/repo"
        routes = {
            f"GET {api}/pulls/53": [response(pull()), response(pull())],
            f"GET {api}/pulls/53/files?per_page=100": response([changed("added.py", status="added")]),
            f"GET {api}/git/commits/{BASE_SHA}": response({"sha": BASE_SHA, "tree": {"sha": BASE_TREE_SHA}}),
            f"GET https://api.github.test/repos/source/repo/git/commits/{HEAD_SHA}": response({"sha": HEAD_SHA, "tree": {"sha": HEAD_TREE_SHA}}),
            f"GET {api}/git/trees/{BASE_TREE_SHA}?recursive=1": response({"tree": []}),
            f"GET https://api.github.test/repos/source/repo/git/trees/{HEAD_TREE_SHA}?recursive=1": response({"tree": [{"path": "added.py", "mode": "100755", "type": "blob", "sha": "added-blob"}]}),
            f"GET https://api.github.test/repos/source/repo/contents/added.py?ref={HEAD_SHA}": response({"sha": "added-blob", "content": "YWRkZWQ=", "encoding": "base64"}),
            f"GET {api}/commits/{HEAD_SHA}/check-runs?per_page=100": response({"check_runs": []}),
            f"GET {api}/contents/AGENTS.md?ref=main": response({"message": "missing"}, status=404),
            f"GET {api}/contents/.github/qykw.toml?ref=main": response({"message": "missing"}, status=404),
        }
        gw, _ = gateway(routes)
        snapshot = gw.get_pull_snapshot(53, run=run_context())
        added = snapshot.changed_files[0]
        self.assertIsNone(added.base_content)
        self.assertIsNone(added.base_mode)
        self.assertEqual(added.head_content, "added")
        self.assertEqual(added.head_mode, "100755")
        self.assertNotIn("base_content_missing:added.py", snapshot.omissions)
        self.assertNotIn("base_mode_missing:added.py", snapshot.omissions)

    def test_consecutive_snapshots_keep_their_own_immutable_omissions(self) -> None:
        api = "https://api.github.test/repos/owner/repo"
        missing = response({"message": "missing"}, status=404)
        routes = {
            f"GET {api}/pulls/53": [response(pull()), response(pull()), response(pull()), response(pull())],
            f"GET {api}/pulls/53/files?per_page=100": [response([]), response([])],
            f"GET {api}/git/commits/{BASE_SHA}": [response({"sha": BASE_SHA, "tree": {"sha": BASE_TREE_SHA}}), response({"sha": BASE_SHA, "tree": {"sha": BASE_TREE_SHA}})],
            f"GET https://api.github.test/repos/source/repo/git/commits/{HEAD_SHA}": [response({"sha": HEAD_SHA, "tree": {"sha": HEAD_TREE_SHA}}), response({"sha": HEAD_SHA, "tree": {"sha": HEAD_TREE_SHA}})],
            f"GET {api}/git/trees/{BASE_TREE_SHA}?recursive=1": [response({"tree": []}), response({"tree": []})],
            f"GET https://api.github.test/repos/source/repo/git/trees/{HEAD_TREE_SHA}?recursive=1": [response({"tree": []}), response({"tree": []})],
            f"GET {api}/commits/{HEAD_SHA}/check-runs?per_page=100": [response({"check_runs": []}), response({"check_runs": []})],
            f"GET {api}/contents/AGENTS.md?ref=main": [missing, response({"sha": "a", "content": "cnVsZQ==", "encoding": "base64"})],
            f"GET {api}/contents/.github/qykw.toml?ref=main": [missing, missing],
        }
        gw, _ = gateway(routes)
        first = gw.get_pull_snapshot(53, run=run_context())
        second = gw.get_pull_snapshot(53, run=run_context())
        self.assertIn("trusted_rule_missing:AGENTS.md", first.omissions)
        self.assertNotIn("trusted_rule_missing:AGENTS.md", second.omissions)

    def test_trusted_rule_reads_are_exact_default_branch_paths_only(self) -> None:
        api = "https://api.github.test/repos/owner/repo"
        gw, transport = gateway({
            f"GET {api}/contents/AGENTS.md?ref=main": response({"sha": "a", "content": "cnVsZQ==", "encoding": "base64"}),
            f"GET {api}/contents/.github/qykw.toml?ref=main": response({"message": "missing"}, status=404),
        })
        rules = gw.get_default_branch_rules()
        self.assertEqual([rule.path for rule in rules], ["AGENTS.md"])
        self.assertTrue(all("ref=main" in url for _, url, _ in transport.calls))
        with self.assertRaisesRegex(GitHubError, "unsafe_path"):
            gw.get_file_for_run(run_context(), "../AGENTS.md", DiffSide.RIGHT)

    def test_typed_file_selection_rejects_mismatched_context_before_content_read(self) -> None:
        api = "https://api.github.test/repos/owner/repo/pulls/53"
        gw, transport = gateway({f"GET {api}": response(pull(head="e" * 40))})
        with self.assertRaisesRegex(GitHubError, "stale_pull_ref"):
            gw.get_file_for_run(run_context(), "safe.py", DiffSide.RIGHT)
        self.assertEqual(len(transport.calls), 1)

    def test_review_posts_head_sha_and_inline_side_after_head_and_identity_rechecks(self) -> None:
        api = "https://api.github.test/repos/owner/repo"
        gw, transport = gateway({
            f"GET {api}/pulls/53": response(pull()),
            "GET https://api.github.test/user": response({"login": "qykw", "id": 1}),
            f"POST {api}/pulls/53/reviews": response({"id": 9}, status=200),
        })
        review_id = gw.create_review(
            53,
            head_sha=HEAD_SHA,
            body="body",
            comments=(InlineComment("deleted.py", 7, DiffSide.LEFT, "note", "f"),),
        )
        self.assertEqual(review_id, 9)
        body = json.loads(transport.calls[-1][2].decode("utf-8"))  # type: ignore[union-attr]
        self.assertEqual(body["commit_id"], HEAD_SHA)
        self.assertEqual(body["comments"][0]["side"], "LEFT")
        self.assertEqual([method for method, _, _ in transport.calls], ["GET", "GET", "POST"])

    def test_review_stale_head_does_not_write_or_authenticate(self) -> None:
        api = "https://api.github.test/repos/owner/repo"
        gw, transport = gateway({f"GET {api}/pulls/53": response(pull(head="e" * 40))})
        with self.assertRaisesRegex(GitHubError, "stale_pull_ref"):
            gw.create_review(53, head_sha=HEAD_SHA, body="body", comments=())
        self.assertEqual([method for method, _, _ in transport.calls], ["GET"])

    def test_tokens_are_not_in_error_text_or_repr(self) -> None:
        gw, _ = gateway({})
        with self.assertRaises(GitHubError) as caught:
            gw.get_pull_ref(53)
        self.assertNotIn("read-secret", str(caught.exception))
        self.assertNotIn("review-secret", repr(caught.exception))
        self.assertNotIn("read-secret", repr(gw))
        self.assertNotIn("review-secret", repr(gw))


if __name__ == "__main__":
    unittest.main()
