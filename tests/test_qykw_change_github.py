from __future__ import annotations

import base64
from dataclasses import fields
import hashlib
from io import BytesIO
import json
from typing import Mapping
import unittest
from unittest.mock import patch
from urllib.parse import quote
from urllib.error import HTTPError

from tools.qykw.change import (
    CommitIdentity,
    GitTreeEntry,
    SourceTreeIndex as PrepareSourceTreeIndex,
)
from tools.qykw.change_publish import (
    PublicationWriteDisposition,
    PublicationWriteError,
    PublishedPullSnapshot,
    SourceTreeIndex as PublishSourceTreeIndex,
)
from tools.qykw.domain import AuthenticatedUser, PullSnapshot, RepositoryPermission
from tools.qykw import change_github as subject
from tools.qykw.change_github import (
    ChangeGitHubError,
    HttpChangeGitHubGateway,
    HttpTrustedSourceTreeProvider,
)


API = "https://api.github.test"
REPOSITORY = "owner/repo"
HEAD = "a" * 40
BASE = "b" * 40
BRANCH = "qykw/qy-pr7-a1b2-fix"
TOKEN = "github_pat_secret-value"


def git_oid(kind: str, content: bytes, *, length: int = 40) -> str:
    encoded = kind.encode("ascii") + b" " + str(len(content)).encode("ascii") + b"\0" + content
    digest = hashlib.sha1 if length == 40 else hashlib.sha256
    return digest(encoded).hexdigest()


def tree_oid(items: tuple[tuple[str, str, str, str], ...], *, length: int = 40) -> str:
    ordered = sorted(
        items,
        key=lambda item: item[0].encode("utf-8") + (b"/" if item[1] == "tree" else b""),
    )
    payload = b"".join(
        ("40000" if kind == "tree" else mode).encode("ascii")
        + b" "
        + name.encode("utf-8")
        + b"\0"
        + bytes.fromhex(oid)
        for name, kind, mode, oid in ordered
    )
    return git_oid("tree", payload, length=length)


README = b"hello\n"
TOOL = b"#!/usr/bin/env python3\nprint('ok')\n"
README_OID = git_oid("blob", README)
TOOL_OID = git_oid("blob", TOOL)
SRC_TREE = tree_oid((("tool.py", "blob", "100755", TOOL_OID),))
ROOT_TREE = tree_oid(
    (
        ("README.md", "blob", "100644", README_OID),
        ("src", "tree", "040000", SRC_TREE),
    )
)


def commit_payload(commit_sha: str = HEAD, root_tree: str = ROOT_TREE) -> dict[str, object]:
    return {
        "sha": commit_sha,
        "commit": {"tree": {"sha": root_tree}},
    }


def tree_entry(
    path: str,
    mode: str,
    kind: str,
    oid: str,
    *,
    size: int | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "path": path,
        "mode": mode,
        "type": kind,
        "sha": oid,
        "url": f"{API}/ignored",
    }
    if size is not None:
        value["size"] = size
    return value


def recursive_tree_payload(
    *, truncated: bool = False, root_tree: str = ROOT_TREE
) -> dict[str, object]:
    return {
        "sha": root_tree,
        "url": f"{API}/ignored",
        "truncated": truncated,
        "tree": [
            tree_entry("README.md", "100644", "blob", README_OID, size=len(README)),
            tree_entry("src", "040000", "tree", SRC_TREE),
            tree_entry("src/tool.py", "100755", "blob", TOOL_OID, size=len(TOOL)),
        ],
    }


def direct_root_payload() -> dict[str, object]:
    return {
        "sha": ROOT_TREE,
        "url": f"{API}/ignored",
        "truncated": False,
        "tree": [
            tree_entry("README.md", "100644", "blob", README_OID, size=len(README)),
            tree_entry("src", "040000", "tree", SRC_TREE),
        ],
    }


def direct_src_payload() -> dict[str, object]:
    return {
        "sha": SRC_TREE,
        "url": f"{API}/ignored",
        "truncated": False,
        "tree": [tree_entry("tool.py", "100755", "blob", TOOL_OID, size=len(TOOL))],
    }


def blob_payload(oid: str, content: bytes) -> dict[str, object]:
    return {
        "sha": oid,
        "size": len(content),
        "encoding": "base64",
        "content": base64.encodebytes(content).decode("ascii"),
        "url": f"{API}/ignored",
    }


class QueueTransport:
    def __init__(self) -> None:
        self.queue: list[tuple[str, str, int, Mapping[str, str], object]] = []
        self.calls: list[tuple[str, str, Mapping[str, str], bytes | None]] = []

    def add(
        self,
        method: str,
        url: str,
        payload: object,
        *,
        status: int = 200,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.queue.append((method, url, status, headers or {}, payload))

    def __call__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> tuple[int, Mapping[str, str], bytes]:
        self.calls.append((method, url, headers, body))
        if not self.queue:
            raise AssertionError(f"unexpected transport call: {method} {url}")
        expected_method, expected_url, status, response_headers, payload = self.queue.pop(0)
        if (method, url) != (expected_method, expected_url):
            raise AssertionError(
                f"expected {expected_method} {expected_url}, got {method} {url}"
            )
        if isinstance(payload, BaseException):
            raise payload
        if isinstance(payload, bytes):
            response = payload
        else:
            response = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return status, response_headers, response

    def assert_drained(self) -> None:
        self.assertEqual(self.queue, [])

    def assertEqual(self, left: object, right: object) -> None:
        if left != right:
            raise AssertionError(f"{left!r} != {right!r}")


def queue_complete_tree(
    transport: QueueTransport,
    *,
    commit_sha: str = HEAD,
    root_tree: str = ROOT_TREE,
    recursive_payload: object | None = None,
    readme: bytes = README,
    tool: bytes = TOOL,
) -> None:
    transport.add(
        "GET",
        f"{API}/repos/{REPOSITORY}/commits/{commit_sha}",
        commit_payload(commit_sha, root_tree),
    )
    transport.add(
        "GET",
        f"{API}/repos/{REPOSITORY}/git/trees/{root_tree}?recursive=1",
        recursive_payload if recursive_payload is not None else recursive_tree_payload(root_tree=root_tree),
    )
    transport.add(
        "GET", f"{API}/repos/{REPOSITORY}/git/blobs/{README_OID}", blob_payload(README_OID, readme)
    )
    transport.add(
        "GET", f"{API}/repos/{REPOSITORY}/git/blobs/{TOOL_OID}", blob_payload(TOOL_OID, tool)
    )


class TestTrustedSourceTreeProvider(unittest.TestCase):
    def provider(self, transport: QueueTransport) -> HttpTrustedSourceTreeProvider:
        return HttpTrustedSourceTreeProvider(
            api_url=API,
            repository=REPOSITORY,
            source_head_sha=HEAD,
            token=TOKEN,
            transport=transport,
        )

    def test_complete_recursive_tree_is_bound_and_fully_materialized(self) -> None:
        transport = QueueTransport()
        queue_complete_tree(transport)
        provider = self.provider(transport)

        index = provider.get_complete_tree(REPOSITORY, HEAD)

        self.assertIs(type(index), PrepareSourceTreeIndex)
        self.assertEqual(index.schema_version, 1)
        self.assertEqual(index.source_head_sha, HEAD)
        self.assertEqual(index.root_tree_sha, ROOT_TREE)
        self.assertIs(index.complete, True)
        self.assertEqual(
            tuple((entry.path, entry.mode, entry.kind, entry.git_sha) for entry in index.entries),
            (
                ("README.md", "100644", "blob", README_OID),
                ("src", "040000", "tree", SRC_TREE),
                ("src/tool.py", "100755", "blob", TOOL_OID),
            ),
        )
        self.assertEqual(tuple((blob.path, blob.content) for blob in index.blobs), (("README.md", README), ("src/tool.py", TOOL)))
        self.assertEqual(len(index.digest), 64)
        self.assertNotIn(TOKEN, repr(provider))
        self.assertTrue(all(call[2]["Authorization"] == f"Bearer {TOKEN}" for call in transport.calls))
        transport.assert_drained()

    def test_truncated_recursive_result_is_discarded_and_each_tree_is_read_directly(self) -> None:
        transport = QueueTransport()
        partial = recursive_tree_payload(truncated=True)
        partial["tree"] = [tree_entry("attacker.txt", "100644", "blob", "f" * 40, size=1)]
        transport.add("GET", f"{API}/repos/{REPOSITORY}/commits/{HEAD}", commit_payload())
        transport.add("GET", f"{API}/repos/{REPOSITORY}/git/trees/{ROOT_TREE}?recursive=1", partial)
        transport.add("GET", f"{API}/repos/{REPOSITORY}/git/trees/{ROOT_TREE}", direct_root_payload())
        transport.add("GET", f"{API}/repos/{REPOSITORY}/git/trees/{SRC_TREE}", direct_src_payload())
        transport.add("GET", f"{API}/repos/{REPOSITORY}/git/blobs/{README_OID}", blob_payload(README_OID, README))
        transport.add("GET", f"{API}/repos/{REPOSITORY}/git/blobs/{TOOL_OID}", blob_payload(TOOL_OID, TOOL))

        index = self.provider(transport).get_complete_tree(REPOSITORY, HEAD)

        self.assertNotIn("attacker.txt", tuple(entry.path for entry in index.entries))
        self.assertFalse(any("/git/blobs/" + "f" * 40 in call[1] for call in transport.calls))
        transport.assert_drained()

    def test_fixed_repository_and_head_are_checked_before_transport(self) -> None:
        for repository, head in (("other/repo", HEAD), (REPOSITORY, "b" * 40)):
            with self.subTest(repository=repository, head=head):
                transport = QueueTransport()
                with self.assertRaises(ChangeGitHubError) as caught:
                    self.provider(transport).get_complete_tree(repository, head)
                self.assertEqual(caught.exception.code, "source_binding_mismatch")
                self.assertEqual(transport.calls, [])

    def test_public_surface_has_no_general_or_mutating_capabilities(self) -> None:
        public = {
            name
            for name in HttpTrustedSourceTreeProvider.__dict__
            if not name.startswith("_")
        }
        self.assertEqual(public, {"get_complete_tree"})
        forbidden = {"request", "delete", "merge", "approve", "update_ref", "create_ref", "create_review", "create_issue_comment"}
        self.assertTrue(forbidden.isdisjoint(public))

    def test_recursive_tree_schema_path_kind_mode_and_graph_fail_closed(self) -> None:
        invalid_entries = (
            [tree_entry("README.md", "100644", "blob", README_OID)],
            [tree_entry("bad\\name", "100644", "blob", README_OID, size=len(README))],
            [tree_entry("e\u0301.txt", "100644", "blob", README_OID, size=len(README))],
            [
                tree_entry("Readme.md", "100644", "blob", README_OID, size=len(README)),
                tree_entry("README.md", "100644", "blob", README_OID, size=len(README)),
            ],
            [
                tree_entry("README.md", "100644", "blob", README_OID, size=len(README)),
                tree_entry("README.md", "100644", "blob", README_OID, size=len(README)),
            ],
            [tree_entry("link", "120000", "blob", README_OID, size=len(README))],
            [tree_entry("submodule", "160000", "commit", HEAD)],
            [tree_entry("odd", "100644", "mystery", README_OID, size=len(README))],
            [tree_entry("missing/file", "100644", "blob", README_OID, size=len(README))],
            [tree_entry("dir", "100644", "tree", SRC_TREE)],
            [tree_entry("file", "040000", "blob", README_OID, size=len(README))],
        )
        for entries in invalid_entries:
            with self.subTest(entries=entries):
                transport = QueueTransport()
                payload = {"sha": ROOT_TREE, "truncated": False, "tree": entries}
                transport.add("GET", f"{API}/repos/{REPOSITORY}/commits/{HEAD}", commit_payload())
                transport.add("GET", f"{API}/repos/{REPOSITORY}/git/trees/{ROOT_TREE}?recursive=1", payload)
                with self.assertRaises(ChangeGitHubError) as caught:
                    self.provider(transport).get_complete_tree(REPOSITORY, HEAD)
                self.assertIn(caught.exception.code, {"invalid_tree_response", "unsafe_tree_path", "unsupported_tree_entry", "tree_incomplete"})

    def test_tree_response_metadata_and_budgets_fail_closed(self) -> None:
        cases = (
            ({"sha": "f" * 40, "truncated": False, "tree": []}, "tree_sha_mismatch"),
            ({"sha": ROOT_TREE, "tree": []}, "invalid_tree_response"),
            ({"sha": ROOT_TREE, "truncated": "false", "tree": []}, "invalid_tree_response"),
            ({"sha": ROOT_TREE, "truncated": False, "tree": {}}, "invalid_tree_response"),
        )
        for payload, code in cases:
            with self.subTest(code=code):
                transport = QueueTransport()
                transport.add("GET", f"{API}/repos/{REPOSITORY}/commits/{HEAD}", commit_payload())
                transport.add("GET", f"{API}/repos/{REPOSITORY}/git/trees/{ROOT_TREE}?recursive=1", payload)
                with self.assertRaises(ChangeGitHubError) as caught:
                    self.provider(transport).get_complete_tree(REPOSITORY, HEAD)
                self.assertEqual(caught.exception.code, code)

        transport = QueueTransport()
        payload = {"sha": ROOT_TREE, "truncated": False, "tree": [tree_entry("README.md", "100644", "blob", README_OID, size=len(README))]}
        transport.add("GET", f"{API}/repos/{REPOSITORY}/commits/{HEAD}", commit_payload())
        transport.add("GET", f"{API}/repos/{REPOSITORY}/git/trees/{ROOT_TREE}?recursive=1", payload)
        with patch("tools.qykw.change_github._MAX_TREE_ENTRIES", 0):
            with self.assertRaises(ChangeGitHubError) as caught:
                self.provider(transport).get_complete_tree(REPOSITORY, HEAD)
        self.assertEqual(caught.exception.code, "tree_budget_exceeded")

    def test_recursive_response_rejects_unknown_schema_and_root_cycle(self) -> None:
        cases = (
            (
                {
                    "sha": ROOT_TREE,
                    "truncated": False,
                    "tree": [],
                    "unexpected": "field",
                },
                "invalid_tree_response",
            ),
            (
                {
                    "sha": ROOT_TREE,
                    "truncated": False,
                    "tree": [tree_entry("loop", "040000", "tree", ROOT_TREE)],
                },
                "tree_cycle",
            ),
        )
        for payload, code in cases:
            with self.subTest(code=code):
                transport = QueueTransport()
                transport.add("GET", f"{API}/repos/{REPOSITORY}/commits/{HEAD}", commit_payload())
                transport.add("GET", f"{API}/repos/{REPOSITORY}/git/trees/{ROOT_TREE}?recursive=1", payload)
                with self.assertRaises(ChangeGitHubError) as caught:
                    self.provider(transport).get_complete_tree(REPOSITORY, HEAD)
                self.assertEqual(caught.exception.code, code)

    def test_nonrecursive_tree_must_be_complete_direct_and_acyclic(self) -> None:
        bad_children = (
            {"sha": ROOT_TREE, "truncated": True, "tree": []},
            {"sha": ROOT_TREE, "truncated": False, "tree": [tree_entry("nested/file", "100644", "blob", README_OID, size=len(README))]},
            {"sha": ROOT_TREE, "truncated": False, "tree": [tree_entry("loop", "040000", "tree", ROOT_TREE)]},
        )
        for direct in bad_children:
            with self.subTest(direct=direct):
                transport = QueueTransport()
                transport.add("GET", f"{API}/repos/{REPOSITORY}/commits/{HEAD}", commit_payload())
                transport.add("GET", f"{API}/repos/{REPOSITORY}/git/trees/{ROOT_TREE}?recursive=1", recursive_tree_payload(truncated=True))
                transport.add("GET", f"{API}/repos/{REPOSITORY}/git/trees/{ROOT_TREE}", direct)
                with self.assertRaises(ChangeGitHubError) as caught:
                    self.provider(transport).get_complete_tree(REPOSITORY, HEAD)
                self.assertIn(caught.exception.code, {"tree_incomplete", "unsafe_tree_path", "tree_cycle"})

    def test_blob_base64_size_oid_and_declared_size_are_verified(self) -> None:
        invalid_blobs = (
            {**blob_payload(README_OID, README), "encoding": "utf-8"},
            {**blob_payload(README_OID, README), "content": "%%%"},
            {**blob_payload(README_OID, README), "size": len(README) + 1},
            blob_payload(README_OID, b"tampered"),
            {**blob_payload(README_OID, README), "sha": "f" * 40},
        )
        for payload in invalid_blobs:
            with self.subTest(payload=payload):
                transport = QueueTransport()
                transport.add("GET", f"{API}/repos/{REPOSITORY}/commits/{HEAD}", commit_payload())
                transport.add("GET", f"{API}/repos/{REPOSITORY}/git/trees/{ROOT_TREE}?recursive=1", recursive_tree_payload())
                transport.add("GET", f"{API}/repos/{REPOSITORY}/git/blobs/{README_OID}", payload)
                with self.assertRaises(ChangeGitHubError) as caught:
                    self.provider(transport).get_complete_tree(REPOSITORY, HEAD)
                self.assertIn(caught.exception.code, {"invalid_blob_response", "blob_size_mismatch", "blob_oid_mismatch"})

    def test_empty_blob_is_valid_and_its_sha256_is_bound_into_index_digest(self) -> None:
        content = b""
        oid = git_oid("blob", content)
        root = tree_oid((("empty.txt", "blob", "100644", oid),))
        transport = QueueTransport()
        transport.add("GET", f"{API}/repos/{REPOSITORY}/commits/{HEAD}", commit_payload(HEAD, root))
        transport.add(
            "GET",
            f"{API}/repos/{REPOSITORY}/git/trees/{root}?recursive=1",
            {
                "sha": root,
                "truncated": False,
                "tree": [tree_entry("empty.txt", "100644", "blob", oid, size=0)],
            },
        )
        transport.add("GET", f"{API}/repos/{REPOSITORY}/git/blobs/{oid}", blob_payload(oid, content))

        provider = HttpTrustedSourceTreeProvider(
            api_url=API,
            repository=REPOSITORY,
            source_head_sha=HEAD,
            token=TOKEN,
            transport=transport,
        )
        index = provider.get_complete_tree(REPOSITORY, HEAD)

        self.assertEqual(index.blobs[0].content, b"")
        self.assertEqual(index.digest, subject.compute_prepare_index_digest(index))

    def test_recursive_nested_tree_cycle_is_rejected_before_blob_fetch(self) -> None:
        repeated = "e" * 40
        payload = {
            "sha": ROOT_TREE,
            "truncated": False,
            "tree": [
                tree_entry("a", "040000", "tree", repeated),
                tree_entry("a/b", "040000", "tree", repeated),
            ],
        }
        transport = QueueTransport()
        transport.add("GET", f"{API}/repos/{REPOSITORY}/commits/{HEAD}", commit_payload())
        transport.add("GET", f"{API}/repos/{REPOSITORY}/git/trees/{ROOT_TREE}?recursive=1", payload)
        with self.assertRaises(ChangeGitHubError) as caught:
            self.provider(transport).get_complete_tree(REPOSITORY, HEAD)
        self.assertEqual(caught.exception.code, "tree_cycle")

    def test_commit_and_error_boundaries_are_sanitized(self) -> None:
        cases = (
            ({"sha": "f" * 40, "commit": {"tree": {"sha": ROOT_TREE}}}, "commit_sha_mismatch"),
            ({"sha": HEAD, "commit": {}}, "invalid_commit_response"),
            ({"sha": HEAD, "commit": {"tree": {"sha": "invalid"}}}, "invalid_commit_response"),
        )
        for payload, code in cases:
            with self.subTest(code=code):
                transport = QueueTransport()
                transport.add("GET", f"{API}/repos/{REPOSITORY}/commits/{HEAD}", payload)
                with self.assertRaises(ChangeGitHubError) as caught:
                    self.provider(transport).get_complete_tree(REPOSITORY, HEAD)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(str(caught.exception), code)
                self.assertNotIn(TOKEN, repr(caught.exception))

        transport = QueueTransport()
        response_body = b'{"secret":"response-body-sentinel"}'
        transport.add("GET", f"{API}/repos/{REPOSITORY}/commits/{HEAD}", response_body, status=500)
        with self.assertRaises(ChangeGitHubError) as caught:
            self.provider(transport).get_complete_tree(REPOSITORY, HEAD)
        self.assertEqual(caught.exception.code, "github_read_failed")
        self.assertNotIn("response-body-sentinel", str(caught.exception))

    def test_commit_and_root_tree_oid_algorithms_must_match(self) -> None:
        for commit_sha, root_tree_sha in (
            ("a" * 40, "b" * 64),
            ("a" * 64, "b" * 40),
        ):
            with self.subTest(
                commit_length=len(commit_sha), tree_length=len(root_tree_sha)
            ):
                transport = QueueTransport()
                transport.add(
                    "GET",
                    f"{API}/repos/{REPOSITORY}/commits/{commit_sha}",
                    commit_payload(commit_sha, root_tree_sha),
                )
                provider = HttpTrustedSourceTreeProvider(
                    api_url=API,
                    repository=REPOSITORY,
                    source_head_sha=commit_sha,
                    token=TOKEN,
                    transport=transport,
                )

                with self.assertRaises(ChangeGitHubError) as caught:
                    provider.get_complete_tree(REPOSITORY, commit_sha)

                self.assertEqual(caught.exception.code, "invalid_commit_response")
                transport.assert_drained()


class TestPublicationGateway(unittest.TestCase):
    def gateway(self, transport: QueueTransport) -> HttpChangeGitHubGateway:
        return HttpChangeGitHubGateway(API, REPOSITORY, TOKEN, transport=transport)

    def test_protocol_surface_is_exact_and_repository_bound(self) -> None:
        expected = {
            "repository",
            "get_pull_snapshot",
            "get_actor_permission",
            "get_authenticated_user",
            "commit_exists",
            "get_commit_tree_sha",
            "get_source_tree_index",
            "get_changed_paths",
            "get_blob_at_commit",
            "branch_exists",
            "get_ref_target",
            "find_draft_pull_by_run_marker",
            "get_published_pull_snapshot",
            "create_blob",
            "create_tree",
            "create_commit",
            "create_ref",
            "create_draft_pull_request",
        }
        public = {name for name in HttpChangeGitHubGateway.__dict__ if not name.startswith("_")}
        self.assertEqual(public, expected)
        self.assertTrue({"delete", "merge", "approve", "update_ref", "request", "create_issue_comment"}.isdisjoint(public))
        transport = QueueTransport()
        gateway = self.gateway(transport)
        self.assertEqual(gateway.repository, REPOSITORY)
        self.assertNotIn(TOKEN, repr(gateway))
        with self.assertRaises(ChangeGitHubError) as caught:
            gateway.commit_exists("other/repo", HEAD)
        self.assertEqual(caught.exception.code, "repository_binding_mismatch")
        self.assertEqual(transport.calls, [])

    def test_pull_permission_identity_commit_and_ref_reads_are_typed(self) -> None:
        transport = QueueTransport()
        pull = {
            "number": 7,
            "state": "open",
            "draft": False,
            "title": "Title",
            "body": "Body",
            "head": {"sha": HEAD, "repo": {"full_name": "fork/repo"}, "ref": "topic"},
            "base": {"sha": BASE, "repo": {"full_name": REPOSITORY}, "ref": "main"},
            "merged": False,
            "user": {"login": "alice"},
        }
        transport.add("GET", f"{API}/repos/{REPOSITORY}/pulls/7", pull)
        transport.add("GET", f"{API}/repos/{REPOSITORY}/collaborators/alice/permission", {"permission": "write", "user": {"login": "alice"}})
        transport.add("GET", f"{API}/user", {"login": "qykw", "id": 12345})
        transport.add("GET", f"{API}/repos/{REPOSITORY}/commits/{HEAD}", commit_payload())
        transport.add("GET", f"{API}/repos/{REPOSITORY}/commits/{BASE}", {}, status=404)
        transport.add("GET", f"{API}/repos/{REPOSITORY}/git/ref/heads/main", {"ref": "refs/heads/main", "object": {"type": "commit", "sha": BASE}})
        transport.add("GET", f"{API}/repos/{REPOSITORY}/git/ref/heads/missing", {}, status=404)
        gateway = self.gateway(transport)

        snapshot = gateway.get_pull_snapshot(7)
        self.assertIs(type(snapshot), PullSnapshot)
        self.assertEqual((snapshot.source_repository, snapshot.target_repository), ("fork/repo", REPOSITORY))
        self.assertEqual(snapshot.changed_files, ())
        self.assertEqual(gateway.get_actor_permission("alice"), RepositoryPermission.WRITE)
        self.assertEqual(gateway.get_authenticated_user(), AuthenticatedUser("qykw", 12345))
        self.assertIs(gateway.commit_exists(REPOSITORY, HEAD), True)
        self.assertIs(gateway.commit_exists(REPOSITORY, BASE), False)
        self.assertEqual(gateway.get_ref_target(REPOSITORY, "main"), BASE)
        self.assertIs(gateway.branch_exists(REPOSITORY, "missing"), False)
        transport.assert_drained()

    def test_pull_snapshot_number_is_bound_to_requested_pull(self) -> None:
        transport = QueueTransport()
        transport.add(
            "GET",
            f"{API}/repos/{REPOSITORY}/pulls/7",
            {
                "number": 8,
                "state": "open",
                "draft": False,
                "title": "Title",
                "body": "Body",
                "head": {"sha": HEAD, "repo": {"full_name": REPOSITORY}, "ref": "topic"},
                "base": {"sha": BASE, "repo": {"full_name": REPOSITORY}, "ref": "main"},
            },
        )
        with self.assertRaises(ChangeGitHubError) as caught:
            self.gateway(transport).get_pull_snapshot(7)
        self.assertEqual(caught.exception.code, "pull_number_mismatch")

    def test_commit_tree_read_and_missing_blob_are_fail_closed(self) -> None:
        transport = QueueTransport()
        transport.add("GET", f"{API}/repos/{REPOSITORY}/commits/{HEAD}", commit_payload())
        queue_complete_tree(transport)
        gateway = self.gateway(transport)

        self.assertEqual(gateway.get_commit_tree_sha(REPOSITORY, HEAD), ROOT_TREE)
        with self.assertRaises(ChangeGitHubError) as caught:
            gateway.get_blob_at_commit(REPOSITORY, HEAD, "missing.txt")
        self.assertEqual(caught.exception.code, "blob_not_found")
        transport.assert_drained()

    def test_publish_tree_index_and_blob_are_task5_compatible(self) -> None:
        transport = QueueTransport()
        queue_complete_tree(transport)
        queue_complete_tree(transport)
        gateway = self.gateway(transport)

        index = gateway.get_source_tree_index(REPOSITORY, HEAD)
        blob = gateway.get_blob_at_commit(REPOSITORY, HEAD, "src/tool.py")

        self.assertIs(type(index), PublishSourceTreeIndex)
        self.assertEqual(index.root_tree_sha, ROOT_TREE)
        self.assertIs(index.complete, True)
        self.assertEqual(blob.content, TOOL)
        self.assertEqual(blob.git_sha, TOOL_OID)
        transport.assert_drained()

    def test_changed_paths_are_computed_from_two_complete_trees(self) -> None:
        old_content = b"old\n"
        new_content = b"new\n"
        old_oid = git_oid("blob", old_content)
        new_oid = git_oid("blob", new_content)
        old_root = tree_oid((("file.txt", "blob", "100644", old_oid),))
        new_root = tree_oid((("file.txt", "blob", "100644", new_oid), ("new.txt", "blob", "100644", README_OID)))
        transport = QueueTransport()
        for commit_sha, root, entries, blobs in (
            (BASE, old_root, [tree_entry("file.txt", "100644", "blob", old_oid, size=len(old_content))], ((old_oid, old_content),)),
            (HEAD, new_root, [tree_entry("file.txt", "100644", "blob", new_oid, size=len(new_content)), tree_entry("new.txt", "100644", "blob", README_OID, size=len(README))], ((new_oid, new_content), (README_OID, README))),
        ):
            transport.add("GET", f"{API}/repos/{REPOSITORY}/commits/{commit_sha}", commit_payload(commit_sha, root))
            transport.add("GET", f"{API}/repos/{REPOSITORY}/git/trees/{root}?recursive=1", {"sha": root, "truncated": False, "tree": entries})
            for oid, content in blobs:
                transport.add("GET", f"{API}/repos/{REPOSITORY}/git/blobs/{oid}", blob_payload(oid, content))
        gateway = self.gateway(transport)

        self.assertEqual(gateway.get_changed_paths(REPOSITORY, BASE, HEAD), ("file.txt", "new.txt"))
        transport.assert_drained()

    def test_draft_pull_lookup_requires_exact_run_marker_and_paginates(self) -> None:
        marker = '<!-- qykw-change:v1 {"base_ref":"main","branch":"qykw/qy-pr7-a1b2-fix","run":"QY-PR7-A1B2","target_repository":"owner/repo"} -->'
        wrong = self._published_pull(8, body="no marker", head_sha=HEAD)
        exact = self._published_pull(9, body=marker, head_sha=HEAD)
        first_url = f"{API}/repos/{REPOSITORY}/pulls?state=open&head={quote('owner:' + BRANCH, safe='')}&base=main&per_page=100&page=1"
        second_url = first_url[:-1] + "2"
        transport = QueueTransport()
        transport.add("GET", first_url, [wrong], headers={"Link": f'<{second_url}>; rel="next"'})
        transport.add("GET", second_url, [exact])
        gateway = self.gateway(transport)

        found = gateway.find_draft_pull_by_run_marker(
            REPOSITORY, branch_name=BRANCH, base_ref="main", run_id="QY-PR7-A1B2"
        )

        self.assertEqual(found, PublishedPullSnapshot(9, "open", True, "qykw", REPOSITORY, BRANCH, HEAD, "main", BASE))
        transport.assert_drained()

    def test_published_pull_snapshot_validates_repository_head_and_base(self) -> None:
        transport = QueueTransport()
        transport.add("GET", f"{API}/repos/{REPOSITORY}/pulls/9", self._published_pull(9, body="", head_sha=HEAD))
        snapshot = self.gateway(transport).get_published_pull_snapshot(REPOSITORY, 9)
        self.assertEqual(tuple(field.name for field in fields(snapshot)), ("number", "state", "draft", "author_login", "head_repository", "head_ref", "head_sha", "base_ref", "base_sha"))
        self.assertEqual(snapshot.head_repository, REPOSITORY)
        transport.assert_drained()

    def test_git_data_and_draft_pull_writes_emit_fixed_payloads(self) -> None:
        identity = CommitIdentity("qykw", "qykw", "12345+qykw@users.noreply.github.com")
        new_blob = git_oid("blob", b"new\n")
        new_tree = "c" * 40
        new_commit = "d" * 40
        transport = QueueTransport()
        transport.add("POST", f"{API}/repos/{REPOSITORY}/git/blobs", {"sha": new_blob})
        transport.add("POST", f"{API}/repos/{REPOSITORY}/git/trees", {"sha": new_tree})
        transport.add("POST", f"{API}/repos/{REPOSITORY}/git/commits", {"sha": new_commit, "tree": {"sha": new_tree}, "parents": [{"sha": HEAD}]})
        transport.add("POST", f"{API}/repos/{REPOSITORY}/git/refs", {"ref": f"refs/heads/{BRANCH}", "object": {"type": "commit", "sha": new_commit}})
        transport.add("POST", f"{API}/repos/{REPOSITORY}/pulls", self._published_pull(9, body="Body", head_sha=new_commit))
        gateway = self.gateway(transport)

        self.assertEqual(gateway.create_blob(repository=REPOSITORY, content=b"new\n"), new_blob)
        self.assertEqual(gateway.create_tree(repository=REPOSITORY, base_tree_sha=ROOT_TREE, entries=(GitTreeEntry("src/tool.py", "100755", new_blob),)), new_tree)
        commit = gateway.create_commit(repository=REPOSITORY, parent_sha=HEAD, tree_sha=new_tree, message="qykw: apply authorized change QY-PR7-A1B2", identity=identity)
        self.assertEqual((commit.commit_sha, commit.tree_sha), (new_commit, new_tree))
        gateway.create_ref(repository=REPOSITORY, branch_name=BRANCH, commit_sha=new_commit)
        self.assertEqual(gateway.create_draft_pull_request(repository=REPOSITORY, head=BRANCH, base="main", title="Title", body="Body"), 9)

        bodies = [json.loads(call[3].decode("utf-8")) for call in transport.calls]
        self.assertEqual(bodies[0], {"content": base64.b64encode(b"new\n").decode("ascii"), "encoding": "base64"})
        self.assertEqual(bodies[1], {"base_tree": ROOT_TREE, "tree": [{"mode": "100755", "path": "src/tool.py", "sha": new_blob, "type": "blob"}]})
        self.assertEqual(bodies[2]["parents"], [HEAD])
        self.assertEqual(bodies[2]["author"], {"name": "qykw", "email": "12345+qykw@users.noreply.github.com"})
        self.assertEqual(bodies[2]["committer"], bodies[2]["author"])
        self.assertEqual(bodies[3], {"ref": f"refs/heads/{BRANCH}", "sha": new_commit})
        self.assertEqual(bodies[4], {"base": "main", "body": "Body", "draft": True, "head": BRANCH, "title": "Title"})
        transport.assert_drained()

    def test_write_failure_is_classified_without_body_token_or_retry(self) -> None:
        transport = QueueTransport()
        transport.add("POST", f"{API}/repos/{REPOSITORY}/git/blobs", OSError("response-body-sentinel"))
        gateway = self.gateway(transport)
        with self.assertRaises(PublicationWriteError) as caught:
            gateway.create_blob(repository=REPOSITORY, content=b"new\n")
        self.assertEqual(caught.exception.code, "github_write_transport")
        self.assertIs(caught.exception.disposition, PublicationWriteDisposition.MAY_HAVE_BEEN_ACCEPTED)
        self.assertNotIn(TOKEN, repr(caught.exception))
        self.assertNotIn("response-body-sentinel", str(caught.exception))
        self.assertEqual(len(transport.calls), 1)

    def test_sha256_repository_accepts_matching_blob_object_id(self) -> None:
        content = b"sha256 repository\n"
        oid = git_oid("blob", content, length=64)
        transport = QueueTransport()
        transport.add("POST", f"{API}/repos/{REPOSITORY}/git/blobs", {"sha": oid})
        self.assertEqual(
            self.gateway(transport).create_blob(repository=REPOSITORY, content=content),
            oid,
        )

    def test_lookup_rejects_missing_and_unsafe_pages(self) -> None:
        first_url = f"{API}/repos/{REPOSITORY}/pulls?state=open&head={quote('owner:' + BRANCH, safe='')}&base=main&per_page=100&page=1"
        cases = (
            ([], {}, None),
            ([self._published_pull(index + 1, body="none", head_sha=HEAD) for index in range(100)], {}, "pull_list_incomplete"),
            ([], {"Link": '<https://evil.test/repos/owner/repo/pulls?page=2>; rel="next"'}, "unsafe_pagination"),
            ([], {"Link": '<bad>; nope'}, "unsafe_pagination"),
        )
        for payload, headers, error_code in cases:
            with self.subTest(error_code=error_code):
                transport = QueueTransport()
                transport.add("GET", first_url, payload, headers=headers)
                gateway = self.gateway(transport)
                if error_code is None:
                    self.assertIsNone(
                        gateway.find_draft_pull_by_run_marker(
                            REPOSITORY,
                            branch_name=BRANCH,
                            base_ref="main",
                            run_id="QY-PR7-A1B2",
                        )
                    )
                else:
                    with self.assertRaises(ChangeGitHubError) as caught:
                        gateway.find_draft_pull_by_run_marker(
                            REPOSITORY,
                            branch_name=BRANCH,
                            base_ref="main",
                            run_id="QY-PR7-A1B2",
                        )
                    self.assertEqual(caught.exception.code, error_code)

    def test_gateway_invalid_read_and_write_responses_are_sanitized(self) -> None:
        read_cases = (
            ("collaborators/alice/permission", {"permission": "owner"}, lambda gateway: gateway.get_actor_permission("alice"), "invalid_permission_response"),
            ("git/ref/heads/main", {"ref": "refs/heads/main", "object": {"type": "tag", "sha": HEAD}}, lambda gateway: gateway.get_ref_target(REPOSITORY, "main"), "invalid_ref_response"),
            ("pulls/7", self._published_pull(8, body="", head_sha=HEAD), lambda gateway: gateway.get_published_pull_snapshot(REPOSITORY, 7), "pull_number_mismatch"),
        )
        for suffix, payload, call, code in read_cases:
            with self.subTest(code=code):
                transport = QueueTransport()
                transport.add("GET", f"{API}/repos/{REPOSITORY}/{suffix}", payload)
                with self.assertRaises(ChangeGitHubError) as caught:
                    call(self.gateway(transport))
                self.assertEqual(caught.exception.code, code)

        for status, disposition in (
            (422, PublicationWriteDisposition.DEFINITELY_NOT_SENT),
            (500, PublicationWriteDisposition.MAY_HAVE_BEEN_ACCEPTED),
        ):
            with self.subTest(status=status):
                transport = QueueTransport()
                transport.add("POST", f"{API}/repos/{REPOSITORY}/git/blobs", {"message": "secret body"}, status=status)
                with self.assertRaises(PublicationWriteError) as caught:
                    self.gateway(transport).create_blob(repository=REPOSITORY, content=b"new\n")
                self.assertEqual(caught.exception.code, "github_write_rejected")
                self.assertIs(caught.exception.disposition, disposition)
                self.assertNotIn("secret body", str(caught.exception))

    def test_write_rejection_allowlist_is_conservative_across_write_targets(self) -> None:
        definite_statuses = (400, 401, 403, 404, 409, 422)
        uncertain_statuses = (405, 408, 421, 425, 429, 451, 500)
        status_cases = tuple(
            (status, PublicationWriteDisposition.DEFINITELY_NOT_SENT)
            for status in definite_statuses
        ) + tuple(
            (status, PublicationWriteDisposition.MAY_HAVE_BEEN_ACCEPTED)
            for status in uncertain_statuses
        )
        targets = (
            (
                "create_ref",
                f"{API}/repos/{REPOSITORY}/git/refs",
                lambda transport: self.gateway(transport).create_ref(
                    repository=REPOSITORY,
                    branch_name=BRANCH,
                    commit_sha=HEAD,
                ),
            ),
            (
                "create_pull_request",
                f"{API}/repos/{REPOSITORY}/pulls",
                lambda transport: self.gateway(
                    transport
                ).create_draft_pull_request(
                    repository=REPOSITORY,
                    head=BRANCH,
                    base="main",
                    title="Title",
                    body="Body",
                ),
            ),
            (
                "comment",
                f"{API}/repos/{REPOSITORY}/issues/7/comments",
                lambda transport: subject._Client(API, TOKEN, transport).write(
                    f"{API}/repos/{REPOSITORY}/issues/7/comments",
                    {"body": "review summary"},
                ),
            ),
        )
        for name, url, call in targets:
            for status, disposition in status_cases:
                with self.subTest(target=name, status=status):
                    transport = QueueTransport()
                    transport.add(
                        "POST",
                        url,
                        {"message": "response-body-sentinel"},
                        status=status,
                    )

                    with self.assertRaises(PublicationWriteError) as caught:
                        call(transport)

                    self.assertEqual(caught.exception.code, "github_write_rejected")
                    self.assertIs(caught.exception.disposition, disposition)
                    self.assertNotIn("response-body-sentinel", str(caught.exception))
                    self.assertEqual(len(transport.calls), 1)
                    transport.assert_drained()

    def test_local_constructor_and_argument_validation_precedes_transport(self) -> None:
        constructors = (
            lambda: HttpChangeGitHubGateway("http://api.github.test", REPOSITORY, TOKEN),
            lambda: HttpChangeGitHubGateway(API, "bad", TOKEN),
            lambda: HttpChangeGitHubGateway(API, REPOSITORY, ""),
        )
        for constructor in constructors:
            with self.subTest(constructor=constructor):
                with self.assertRaises(ChangeGitHubError):
                    constructor()
        with self.assertRaises(ValueError):
            ChangeGitHubError("UPPER")

        transport = QueueTransport()
        gateway = self.gateway(transport)
        invalid_calls = (
            lambda: gateway.get_actor_permission("bad login"),
            lambda: gateway.get_ref_target(REPOSITORY, "bad..ref"),
            lambda: gateway.find_draft_pull_by_run_marker(REPOSITORY, branch_name=BRANCH, base_ref="main", run_id="bad run"),
            lambda: gateway.get_blob_at_commit(REPOSITORY, HEAD, "../bad"),
            lambda: gateway.create_blob(repository=REPOSITORY, content="not-bytes"),
            lambda: gateway.create_tree(repository=REPOSITORY, base_tree_sha=ROOT_TREE, entries=()),
            lambda: gateway.create_commit(repository=REPOSITORY, parent_sha=HEAD, tree_sha=ROOT_TREE, message="Co-Authored-By: Codex", identity=CommitIdentity("qykw", "qykw", "12345+qykw@users.noreply.github.com")),
            lambda: gateway.create_draft_pull_request(repository=REPOSITORY, head=BRANCH, base="main", title="", body="Body"),
        )
        for call in invalid_calls:
            with self.subTest(call=call):
                with self.assertRaises(ChangeGitHubError):
                    call()
        self.assertEqual(transport.calls, [])

    def test_write_response_integrity_and_tree_entry_validation_fail_closed(self) -> None:
        identity = CommitIdentity("qykw", "qykw", "12345+qykw@users.noreply.github.com")
        new_blob = git_oid("blob", b"new\n")
        invalid_local_calls = (
            lambda gateway: gateway.create_tree(
                repository=REPOSITORY,
                base_tree_sha=ROOT_TREE,
                entries=(object(),),
            ),
            lambda gateway: gateway.create_tree(
                repository=REPOSITORY,
                base_tree_sha=ROOT_TREE,
                entries=(
                    GitTreeEntry("A.txt", "100644", new_blob),
                    GitTreeEntry("a.txt", "100644", new_blob),
                ),
            ),
            lambda gateway: gateway.create_commit(
                repository=REPOSITORY,
                parent_sha=HEAD,
                tree_sha=ROOT_TREE,
                message="message",
                identity=CommitIdentity("qykw", "different", "mail@example.test"),
            ),
        )
        for call in invalid_local_calls:
            with self.subTest(call=call):
                transport = QueueTransport()
                with self.assertRaises(ChangeGitHubError):
                    call(self.gateway(transport))
                self.assertEqual(transport.calls, [])

        cases = (
            (
                "git/blobs",
                {"sha": "f" * 40},
                lambda gateway: gateway.create_blob(repository=REPOSITORY, content=b"new\n"),
            ),
            (
                "git/commits",
                {"sha": "d" * 40, "tree": {"sha": "f" * 40}, "parents": [{"sha": HEAD}]},
                lambda gateway: gateway.create_commit(repository=REPOSITORY, parent_sha=HEAD, tree_sha=ROOT_TREE, message="message", identity=identity),
            ),
            (
                "git/commits",
                {"sha": "d" * 40, "tree": {"sha": ROOT_TREE}, "parents": []},
                lambda gateway: gateway.create_commit(repository=REPOSITORY, parent_sha=HEAD, tree_sha=ROOT_TREE, message="message", identity=identity),
            ),
            (
                "git/refs",
                {"ref": f"refs/heads/{BRANCH}", "object": {"type": "commit", "sha": "f" * 40}},
                lambda gateway: gateway.create_ref(repository=REPOSITORY, branch_name=BRANCH, commit_sha=HEAD),
            ),
            (
                "pulls",
                {**self._published_pull(9, body="Body", head_sha=HEAD), "draft": False},
                lambda gateway: gateway.create_draft_pull_request(repository=REPOSITORY, head=BRANCH, base="main", title="Title", body="Body"),
            ),
        )
        for suffix, payload, call in cases:
            with self.subTest(suffix=suffix):
                transport = QueueTransport()
                transport.add("POST", f"{API}/repos/{REPOSITORY}/{suffix}", payload)
                with self.assertRaises(PublicationWriteError) as caught:
                    call(self.gateway(transport))
                self.assertIs(
                    caught.exception.disposition,
                    PublicationWriteDisposition.MAY_HAVE_BEEN_ACCEPTED,
                )

    def test_tree_request_and_blob_budgets_fail_closed(self) -> None:
        transport = QueueTransport()
        transport.add("GET", f"{API}/repos/{REPOSITORY}/commits/{HEAD}", commit_payload())
        transport.add("GET", f"{API}/repos/{REPOSITORY}/git/trees/{ROOT_TREE}?recursive=1", recursive_tree_payload(truncated=True))
        with patch("tools.qykw.change_github._MAX_TREE_REQUESTS", 0):
            with self.assertRaises(ChangeGitHubError) as caught:
                self.gateway(transport).get_source_tree_index(REPOSITORY, HEAD)
        self.assertEqual(caught.exception.code, "tree_budget_exceeded")

        transport = QueueTransport()
        transport.add("GET", f"{API}/repos/{REPOSITORY}/commits/{HEAD}", commit_payload())
        transport.add("GET", f"{API}/repos/{REPOSITORY}/git/trees/{ROOT_TREE}?recursive=1", recursive_tree_payload())
        with patch("tools.qykw.change_github._MAX_TOTAL_BLOB_BYTES", 1):
            with self.assertRaises(ChangeGitHubError) as caught:
                self.gateway(transport).get_source_tree_index(REPOSITORY, HEAD)
        self.assertEqual(caught.exception.code, "tree_budget_exceeded")

    @staticmethod
    def _published_pull(number: int, *, body: str, head_sha: str) -> dict[str, object]:
        return {
            "number": number,
            "state": "open",
            "draft": True,
            "body": body,
            "user": {"login": "qykw"},
            "head": {"ref": BRANCH, "sha": head_sha, "repo": {"full_name": REPOSITORY}},
            "base": {"ref": "main", "sha": BASE, "repo": {"full_name": REPOSITORY}},
        }


class TestTransportAndDefensiveParsers(unittest.TestCase):
    def test_stdlib_transport_returns_http_status_without_exposing_body(self) -> None:
        error = HTTPError(
            f"{API}/repos/{REPOSITORY}/commits/{HEAD}",
            404,
            "not found",
            {"Content-Type": "application/json"},
            BytesIO(b'{"message":"body sentinel"}'),
        )

        class Opener:
            def open(self, request: object, timeout: int) -> object:
                raise error

        with patch("tools.qykw.change_github.build_opener", return_value=Opener()):
            status, headers, body = subject._stdlib_transport(
                "GET", error.url, {}, None
            )
        self.assertEqual(status, 404)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertIn(b"body sentinel", body)

    def test_private_client_classifies_transport_shape_json_and_http_failures(self) -> None:
        url = f"{API}/repos/{REPOSITORY}/commits/{HEAD}"
        client = subject._Client(API, TOKEN, lambda *args: (_ for _ in ()).throw(OSError("secret")))
        with self.assertRaises(ChangeGitHubError) as caught:
            client.read(url)
        self.assertEqual(caught.exception.code, "github_read_failed")

        propagated = PublicationWriteError(
            "already_safe", PublicationWriteDisposition.DEFINITELY_NOT_SENT
        )
        client = subject._Client(API, TOKEN, lambda *args: (_ for _ in ()).throw(propagated))
        with self.assertRaises(PublicationWriteError) as caught:
            client.write(f"{API}/repos/{REPOSITORY}/git/blobs", {"x": "y"})
        self.assertIs(caught.exception, propagated)

        invalid_responses = (
            (lambda *args: ("200", {}, b"{}"), "invalid_response"),
            (lambda *args: (200, {}, b"{"), "invalid_response"),
        )
        for transport, code in invalid_responses:
            with self.subTest(code=code):
                with self.assertRaises(ChangeGitHubError) as caught:
                    subject._Client(API, TOKEN, transport).read(url)
                self.assertEqual(caught.exception.code, code)

        for transport in (
            lambda *args: (200, {}, b"{"),
            lambda *args: (200, {}, "not-bytes"),
        ):
            with self.subTest(transport=transport):
                with self.assertRaises(PublicationWriteError) as caught:
                    subject._Client(API, TOKEN, transport).write(
                        f"{API}/repos/{REPOSITORY}/git/blobs", {"x": "y"}
                    )
                self.assertEqual(caught.exception.code, "github_write_response")

    def test_private_client_rejects_unsafe_or_unserializable_requests(self) -> None:
        client = subject._Client(API, TOKEN, QueueTransport())
        with self.assertRaises(ChangeGitHubError) as caught:
            client.url(REPOSITORY, "/absolute")
        self.assertEqual(caught.exception.code, "invalid_request")
        with self.assertRaises(ChangeGitHubError) as caught:
            client.read("https://evil.test/user")
        self.assertEqual(caught.exception.code, "unsafe_url")
        with self.assertRaises(ChangeGitHubError) as caught:
            client.write(f"{API}/repos/{REPOSITORY}/git/blobs", {"bad": object()})
        self.assertEqual(caught.exception.code, "invalid_write_request")
        with patch("tools.qykw.change_github._MAX_RESPONSE_BYTES", 1):
            with self.assertRaises(ChangeGitHubError) as caught:
                client.write(f"{API}/repos/{REPOSITORY}/git/blobs", {"x": "long"})
        self.assertEqual(caught.exception.code, "write_request_too_large")

    def test_parser_boundaries_reject_wrong_types_and_invalid_git_shapes(self) -> None:
        invalid_helpers = (
            (lambda: subject._repository(None), "invalid_repository"),
            (lambda: subject._login("-bad"), "invalid_login"),
            (lambda: subject._oid(True), "invalid_git_oid"),
            (lambda: subject._ref("bad ref"), "invalid_ref"),
            (lambda: subject._run_id("bad/run"), "invalid_run_id"),
            (lambda: subject._tree_path(".git/config"), "unsafe_tree_path"),
            (lambda: subject._mapping([], "bad_mapping"), "bad_mapping"),
            (lambda: subject._list({}, "bad_list"), "bad_list"),
            (lambda: subject._string("", "bad_string"), "bad_string"),
            (lambda: subject._bool(1, "bad_bool"), "bad_bool"),
            (lambda: subject._positive_int(True, "bad_int"), "bad_int"),
            (lambda: subject._nonnegative_int(-1, "bad_count"), "bad_count"),
            (lambda: subject._git_oid("blob", b"x", 41), "invalid_git_oid"),
        )
        for call, code in invalid_helpers:
            with self.subTest(code=code):
                with self.assertRaises(ChangeGitHubError) as caught:
                    call()
                self.assertEqual(caught.exception.code, code)
        self.assertIsNone(subject._optional_string(None, "bad_optional"))
        self.assertFalse(subject._safe_text(""))

    def test_malformed_marker_and_link_never_gain_authority(self) -> None:
        self.assertFalse(
            subject._marker_matches(
                "<!-- qykw-change:v1 {bad} -->",
                REPOSITORY,
                BRANCH,
                "main",
                "QY-PR7-A1B2",
            )
        )
        self.assertIsNone(subject._next_link(None))
        with self.assertRaises(ChangeGitHubError) as caught:
            subject._next_link('<one>; rel="next", <two>; rel="next"')
        self.assertEqual(caught.exception.code, "unsafe_pagination")


if __name__ == "__main__":
    unittest.main()
