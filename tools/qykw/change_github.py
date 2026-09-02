"""Repository-bound GitHub adapters for authorized qykw changes.

The public classes deliberately expose only a complete source-tree read and
the exact read/write allowlist consumed by ``change_publish``.  Credentials
remain private constructor inputs; failures contain stable codes only.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePosixPath
import re
import unicodedata
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from tools.qykw.change import (
    CommitIdentity,
    GitTreeEntry,
    PublishedCommit,
    SourceBlob,
    SourceTreeEntry,
    SourceTreeIndex as PrepareSourceTreeIndex,
    compute_source_tree_index_digest as compute_prepare_index_digest,
)
from tools.qykw.change_publish import (
    PublicationWriteDisposition,
    PublicationWriteError,
    PublishedPullSnapshot,
    SourceTreeIndex as PublishSourceTreeIndex,
    compute_source_tree_index_digest as compute_publish_index_digest,
)
from tools.qykw.domain import AuthenticatedUser, PullSnapshot, RepositoryPermission


Transport = Callable[
    [str, str, Mapping[str, str], bytes | None],
    tuple[int, Mapping[str, str], bytes],
]

_REPOSITORY_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"
)
_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_HEX = frozenset("0123456789abcdef")
_BLOB_MODES = frozenset({"100644", "100755"})
_TREE_MODE = "040000"
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_BLOB_BYTES = 8 * 1024 * 1024
_MAX_TOTAL_BLOB_BYTES = 64 * 1024 * 1024
_MAX_TREE_ENTRIES = 100_000
_MAX_TREE_REQUESTS = 20_000
_MAX_PAGES = 100
_MAX_PATH_BYTES = 1_024
_MAX_WRITE_TEXT_BYTES = 64 * 1024
_DEFINITELY_REJECTED_WRITE_STATUSES = frozenset({400, 401, 403, 404, 409, 422})


class ChangeGitHubError(RuntimeError):
    """Sanitized adapter failure with no URL, credential, or response body."""

    def __init__(self, code: str) -> None:
        if type(code) is not str or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) is None:
            raise ValueError("invalid_github_error_code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class _CompleteTree:
    commit_sha: str
    root_tree_sha: str
    entries: tuple[SourceTreeEntry, ...]
    blobs: tuple[SourceBlob, ...]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: object, fp: object, code: int, msg: str, headers: object, newurl: str) -> None:
        return None


def _stdlib_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
) -> tuple[int, Mapping[str, str], bytes]:
    request = Request(url, data=body, headers=dict(headers), method=method)
    try:
        with build_opener(_NoRedirect()).open(request, timeout=30) as response:
            return response.status, dict(response.headers.items()), response.read(_MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        return error.code, dict(error.headers.items()), error.read(_MAX_RESPONSE_BYTES + 1)


class _Client:
    def __init__(self, api_url: str, token: str, transport: Transport | None) -> None:
        self._api_url = _origin(api_url)
        if type(token) is not str or not token or "\r" in token or "\n" in token:
            raise ChangeGitHubError("invalid_credential")
        self._token = token
        self._transport = transport or _stdlib_transport

    def url(self, repository: str, suffix: str, query: Mapping[str, str] | None = None) -> str:
        _repository(repository)
        if not suffix or suffix.startswith("/") or "#" in suffix or "?" in suffix:
            raise ChangeGitHubError("invalid_request")
        value = f"{self._api_url}/repos/{repository}/{suffix}"
        if query:
            value += "?" + urlencode(query)
        return value

    def user_url(self) -> str:
        return f"{self._api_url}/user"

    def read(self, url: str, *, allow_not_found: bool = False) -> object | None:
        return self._request(
            "GET", url, None, allow_not_found=allow_not_found, write=False
        )[0]

    def read_with_headers(
        self, url: str
    ) -> tuple[object, Mapping[str, str]]:
        value, headers = self._request(
            "GET", url, None, allow_not_found=False, write=False
        )
        if value is None:
            raise ChangeGitHubError("invalid_response")
        return value, headers

    def write(self, url: str, payload: Mapping[str, object]) -> object:
        try:
            body = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, UnicodeError):
            raise ChangeGitHubError("invalid_write_request") from None
        if len(body) > _MAX_RESPONSE_BYTES:
            raise ChangeGitHubError("write_request_too_large")
        value = self._request(
            "POST", url, body, allow_not_found=False, write=True
        )[0]
        if value is None:
            raise PublicationWriteError(
                "github_write_response",
                PublicationWriteDisposition.MAY_HAVE_BEEN_ACCEPTED,
            )
        return value

    def _request(
        self,
        method: str,
        url: str,
        body: bytes | None,
        *,
        allow_not_found: bool,
        write: bool,
    ) -> tuple[object | None, Mapping[str, str]]:
        if not self._safe_url(url):
            raise ChangeGitHubError("unsafe_url")
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        try:
            status, response_headers, response_body = self._transport(method, url, headers, body)
        except PublicationWriteError:
            raise
        except Exception:
            if write:
                raise PublicationWriteError(
                    "github_write_transport",
                    PublicationWriteDisposition.MAY_HAVE_BEEN_ACCEPTED,
                ) from None
            raise ChangeGitHubError("github_read_failed") from None
        if (
            type(status) is not int
            or not isinstance(response_headers, Mapping)
            or type(response_body) is not bytes
            or len(response_body) > _MAX_RESPONSE_BYTES
        ):
            if write:
                raise PublicationWriteError(
                    "github_write_response",
                    PublicationWriteDisposition.MAY_HAVE_BEEN_ACCEPTED,
                )
            raise ChangeGitHubError("invalid_response")
        normalized_headers = {
            str(key).casefold(): str(value)
            for key, value in response_headers.items()
        }
        if status == 404 and allow_not_found:
            return None, normalized_headers
        if status < 200 or status >= 300:
            if write:
                disposition = (
                    PublicationWriteDisposition.DEFINITELY_NOT_SENT
                    if status in _DEFINITELY_REJECTED_WRITE_STATUSES
                    else PublicationWriteDisposition.MAY_HAVE_BEEN_ACCEPTED
                )
                raise PublicationWriteError("github_write_rejected", disposition)
            raise ChangeGitHubError("github_read_failed")
        try:
            return json.loads(response_body.decode("utf-8")), normalized_headers
        except (UnicodeError, ValueError, RecursionError):
            if write:
                raise PublicationWriteError(
                    "github_write_response",
                    PublicationWriteDisposition.MAY_HAVE_BEEN_ACCEPTED,
                ) from None
            raise ChangeGitHubError("invalid_response") from None

    def _safe_url(self, url: str) -> bool:
        parsed = urlsplit(url)
        origin = urlsplit(self._api_url)
        return (
            parsed.scheme == "https"
            and parsed.username is None
            and parsed.password is None
            and parsed.hostname == origin.hostname
            and parsed.port == origin.port
            and not parsed.fragment
            and (
                parsed.path == "/user"
                or parsed.path.startswith("/repos/")
            )
        )


class HttpTrustedSourceTreeProvider:
    """Read exactly one configured repository and Head as a complete tree."""

    def __init__(
        self,
        *,
        api_url: str,
        repository: str,
        source_head_sha: str,
        token: str,
        transport: Transport | None = None,
    ) -> None:
        _repository(repository)
        _oid(source_head_sha)
        self._repository = repository
        self._source_head_sha = source_head_sha
        self._client = _Client(api_url, token, transport)

    def __repr__(self) -> str:
        return (
            "HttpTrustedSourceTreeProvider("
            f"repository={self._repository!r}, source_head_sha={self._source_head_sha!r})"
        )

    def get_complete_tree(
        self, source_repository: str, source_head_sha: str
    ) -> PrepareSourceTreeIndex:
        if source_repository != self._repository or source_head_sha != self._source_head_sha:
            raise ChangeGitHubError("source_binding_mismatch")
        tree = _load_complete_tree(self._client, self._repository, self._source_head_sha)
        provisional = PrepareSourceTreeIndex(
            schema_version=1,
            source_head_sha=tree.commit_sha,
            root_tree_sha=tree.root_tree_sha,
            complete=True,
            entries=tree.entries,
            blobs=tree.blobs,
            digest="",
        )
        return PrepareSourceTreeIndex(
            schema_version=provisional.schema_version,
            source_head_sha=provisional.source_head_sha,
            root_tree_sha=provisional.root_tree_sha,
            complete=provisional.complete,
            entries=provisional.entries,
            blobs=provisional.blobs,
            digest=compute_prepare_index_digest(provisional),
        )


class HttpChangeGitHubGateway:
    """Task 5's repository-bound publication capability."""

    def __init__(
        self,
        api_url: str,
        repository: str,
        token: str,
        *,
        transport: Transport | None = None,
    ) -> None:
        _repository(repository)
        self._repository = repository
        self._client = _Client(api_url, token, transport)

    def __repr__(self) -> str:
        return f"HttpChangeGitHubGateway(repository={self._repository!r})"

    @property
    def repository(self) -> str:
        return self._repository

    def get_pull_snapshot(self, pr_number: int) -> PullSnapshot:
        expected_number = _positive_int(pr_number, "invalid_pr_number")
        payload = self._client.read(
            self._client.url(self._repository, f"pulls/{expected_number}")
        )
        data = _pull_data(payload, self._repository)
        if data["number"] != expected_number:
            raise ChangeGitHubError("pull_number_mismatch")
        return PullSnapshot(
            number=data["number"],
            state=data["state"],
            draft=data["draft"],
            source_repository=data["head_repository"],
            source_head_sha=data["head_sha"],
            target_repository=data["base_repository"],
            target_base_sha=data["base_sha"],
            target_base_ref=data["base_ref"],
            title=data["title"],
            body=data["body"],
            changed_files=(),
            trusted_rules=(),
            related_files=(),
            checks=(),
        )

    def get_actor_permission(self, login: str) -> RepositoryPermission:
        _login(login)
        payload = _mapping(
            self._client.read(
                self._client.url(
                    self._repository,
                    f"collaborators/{quote(login, safe='')}/permission",
                )
            ),
            "invalid_permission_response",
        )
        permission = _string(payload.get("permission"), "invalid_permission_response")
        try:
            return RepositoryPermission(permission)
        except ValueError:
            raise ChangeGitHubError("invalid_permission_response") from None

    def get_authenticated_user(self) -> AuthenticatedUser:
        payload = _mapping(
            self._client.read(self._client.user_url()), "invalid_authenticated_user"
        )
        login = _string(payload.get("login"), "invalid_authenticated_user")
        _login(login)
        return AuthenticatedUser(
            login,
            _positive_int(payload.get("id"), "invalid_authenticated_user"),
        )

    def commit_exists(self, repository: str, commit_sha: str) -> bool:
        self._bound(repository)
        _oid(commit_sha)
        payload = self._client.read(
            self._client.url(repository, f"commits/{commit_sha}"), allow_not_found=True
        )
        if payload is None:
            return False
        _commit_tree(payload, commit_sha)
        return True

    def get_commit_tree_sha(self, repository: str, commit_sha: str) -> str:
        self._bound(repository)
        _oid(commit_sha)
        payload = self._client.read(self._client.url(repository, f"commits/{commit_sha}"))
        return _commit_tree(payload, commit_sha)

    def get_source_tree_index(
        self, repository: str, commit_sha: str
    ) -> PublishSourceTreeIndex:
        self._bound(repository)
        _oid(commit_sha)
        tree = _load_complete_tree(self._client, repository, commit_sha)
        digest = compute_publish_index_digest(
            tree.root_tree_sha, tree.entries, tree.blobs
        )
        return PublishSourceTreeIndex(
            tree.root_tree_sha, True, tree.entries, tree.blobs, digest
        )

    def get_changed_paths(
        self, repository: str, base_sha: str, head_sha: str
    ) -> tuple[str, ...]:
        self._bound(repository)
        base = self.get_source_tree_index(repository, base_sha)
        head = self.get_source_tree_index(repository, head_sha)
        before = {
            entry.path: (entry.mode, entry.git_sha)
            for entry in base.entries
            if entry.kind == "blob"
        }
        after = {
            entry.path: (entry.mode, entry.git_sha)
            for entry in head.entries
            if entry.kind == "blob"
        }
        return tuple(
            sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
        )

    def get_blob_at_commit(
        self, repository: str, commit_sha: str, path: str
    ) -> SourceBlob:
        self._bound(repository)
        normalized = _tree_path(path)
        index = self.get_source_tree_index(repository, commit_sha)
        for blob in index.blobs:
            if blob.path == normalized:
                return blob
        raise ChangeGitHubError("blob_not_found")

    def branch_exists(self, repository: str, branch_name: str) -> bool:
        return self.get_ref_target(repository, branch_name) is not None

    def get_ref_target(self, repository: str, branch_name: str) -> str | None:
        self._bound(repository)
        _ref(branch_name)
        payload = self._client.read(
            self._client.url(
                repository, f"git/ref/heads/{quote(branch_name, safe='')}"
            ),
            allow_not_found=True,
        )
        if payload is None:
            return None
        value = _mapping(payload, "invalid_ref_response")
        if value.get("ref") != f"refs/heads/{branch_name}":
            raise ChangeGitHubError("invalid_ref_response")
        target = _mapping(value.get("object"), "invalid_ref_response")
        if target.get("type") != "commit":
            raise ChangeGitHubError("invalid_ref_response")
        return _oid(target.get("sha"), "invalid_ref_response")

    def find_draft_pull_by_run_marker(
        self,
        repository: str,
        *,
        branch_name: str,
        base_ref: str,
        run_id: str,
    ) -> PublishedPullSnapshot | None:
        self._bound(repository)
        _ref(branch_name)
        _ref(base_ref)
        _run_id(run_id)
        owner = repository.split("/", 1)[0]
        query = {
            "state": "open",
            "head": f"{owner}:{branch_name}",
            "base": base_ref,
            "per_page": "100",
            "page": "1",
        }
        url = self._client.url(repository, "pulls", query)
        page = 1
        while page <= _MAX_PAGES:
            payload, headers = self._client.read_with_headers(url)
            items = _list(payload, "invalid_pull_list")
            for item in items:
                snapshot, body = _published_pull(item, repository)
                if (
                    snapshot.draft
                    and snapshot.head_repository == repository
                    and snapshot.head_ref == branch_name
                    and snapshot.base_ref == base_ref
                    and _marker_matches(body, repository, branch_name, base_ref, run_id)
                ):
                    return snapshot
            next_url = _next_link(headers.get("link"))
            if next_url is None:
                if len(items) >= 100:
                    raise ChangeGitHubError("pull_list_incomplete")
                return None
            page += 1
            expected_query = dict(query)
            expected_query["page"] = str(page)
            expected_url = self._client.url(repository, "pulls", expected_query)
            if next_url != expected_url:
                raise ChangeGitHubError("unsafe_pagination")
            url = next_url
        raise ChangeGitHubError("pagination_budget_exceeded")

    def get_published_pull_snapshot(
        self, repository: str, pr_number: int
    ) -> PublishedPullSnapshot:
        self._bound(repository)
        expected_number = _positive_int(pr_number, "invalid_pr_number")
        payload = self._client.read(
            self._client.url(
                repository, f"pulls/{expected_number}"
            )
        )
        snapshot = _published_pull(payload, repository)[0]
        if snapshot.number != expected_number:
            raise ChangeGitHubError("pull_number_mismatch")
        return snapshot

    def create_blob(self, *, repository: str, content: bytes) -> str:
        self._bound(repository)
        if type(content) is not bytes or len(content) > _MAX_BLOB_BYTES:
            raise ChangeGitHubError("invalid_blob_content")
        payload = _mapping(
            self._client.write(
                self._client.url(repository, "git/blobs"),
                {
                    "content": base64.b64encode(content).decode("ascii"),
                    "encoding": "base64",
                },
            ),
            "invalid_blob_write_response",
        )
        actual = _oid(payload.get("sha"), "invalid_blob_write_response")
        expected = _git_oid("blob", content, len(actual))
        if actual != expected:
            raise PublicationWriteError(
                "github_write_response",
                PublicationWriteDisposition.MAY_HAVE_BEEN_ACCEPTED,
            )
        return actual

    def create_tree(
        self,
        *,
        repository: str,
        base_tree_sha: str,
        entries: tuple[GitTreeEntry, ...],
    ) -> str:
        self._bound(repository)
        _oid(base_tree_sha)
        if type(entries) is not tuple or not entries or len(entries) > _MAX_TREE_ENTRIES:
            raise ChangeGitHubError("invalid_tree_write")
        serialized: list[dict[str, str]] = []
        seen: set[str] = set()
        for entry in entries:
            if type(entry) is not GitTreeEntry:
                raise ChangeGitHubError("invalid_tree_write")
            path = _tree_path(entry.path)
            key = _path_key(path)
            if key in seen or entry.mode not in _BLOB_MODES:
                raise ChangeGitHubError("invalid_tree_write")
            seen.add(key)
            serialized.append(
                {
                    "path": path,
                    "mode": entry.mode,
                    "type": "blob",
                    "sha": _oid(entry.blob_sha, "invalid_tree_write"),
                }
            )
        payload = _mapping(
            self._client.write(
                self._client.url(repository, "git/trees"),
                {"base_tree": base_tree_sha, "tree": serialized},
            ),
            "invalid_tree_write_response",
        )
        return _oid(payload.get("sha"), "invalid_tree_write_response")

    def create_commit(
        self,
        *,
        repository: str,
        parent_sha: str,
        tree_sha: str,
        message: str,
        identity: CommitIdentity,
    ) -> PublishedCommit:
        self._bound(repository)
        _oid(parent_sha)
        _oid(tree_sha)
        if type(identity) is not CommitIdentity or not _safe_text(message):
            raise ChangeGitHubError("invalid_commit_write")
        _login(identity.login)
        if identity.name != identity.login or not _safe_text(identity.email):
            raise ChangeGitHubError("invalid_commit_write")
        folded = message.casefold()
        if any(value in folded for value in ("co-authored-by", "codex", "openai")):
            raise ChangeGitHubError("invalid_commit_write")
        author = {"name": identity.name, "email": identity.email}
        payload = _mapping(
            self._client.write(
                self._client.url(repository, "git/commits"),
                {
                    "message": message,
                    "tree": tree_sha,
                    "parents": [parent_sha],
                    "author": author,
                    "committer": author,
                },
            ),
            "invalid_commit_write_response",
        )
        commit_sha = _oid(payload.get("sha"), "invalid_commit_write_response")
        response_tree = _mapping(payload.get("tree"), "invalid_commit_write_response")
        if _oid(response_tree.get("sha"), "invalid_commit_write_response") != tree_sha:
            raise PublicationWriteError(
                "github_write_response",
                PublicationWriteDisposition.MAY_HAVE_BEEN_ACCEPTED,
            )
        parents = _list(payload.get("parents"), "invalid_commit_write_response")
        if len(parents) != 1 or _oid(parents[0].get("sha"), "invalid_commit_write_response") != parent_sha:
            raise PublicationWriteError(
                "github_write_response",
                PublicationWriteDisposition.MAY_HAVE_BEEN_ACCEPTED,
            )
        return PublishedCommit(commit_sha, tree_sha)

    def create_ref(
        self, *, repository: str, branch_name: str, commit_sha: str
    ) -> None:
        self._bound(repository)
        _ref(branch_name)
        _oid(commit_sha)
        payload = _mapping(
            self._client.write(
                self._client.url(repository, "git/refs"),
                {"ref": f"refs/heads/{branch_name}", "sha": commit_sha},
            ),
            "invalid_ref_write_response",
        )
        target = _mapping(payload.get("object"), "invalid_ref_write_response")
        if (
            payload.get("ref") != f"refs/heads/{branch_name}"
            or target.get("type") != "commit"
            or _oid(target.get("sha"), "invalid_ref_write_response") != commit_sha
        ):
            raise PublicationWriteError(
                "github_write_response",
                PublicationWriteDisposition.MAY_HAVE_BEEN_ACCEPTED,
            )

    def create_draft_pull_request(
        self,
        *,
        repository: str,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> int:
        self._bound(repository)
        _ref(head)
        _ref(base)
        if not _safe_text(title) or not _safe_text(body):
            raise ChangeGitHubError("invalid_pull_write")
        payload = self._client.write(
            self._client.url(repository, "pulls"),
            {"head": head, "base": base, "title": title, "body": body, "draft": True},
        )
        snapshot, _ = _published_pull(payload, repository)
        if (
            snapshot.state.casefold() != "open"
            or not snapshot.draft
            or snapshot.head_repository != repository
            or snapshot.head_ref != head
            or snapshot.base_ref != base
        ):
            raise PublicationWriteError(
                "github_write_response",
                PublicationWriteDisposition.MAY_HAVE_BEEN_ACCEPTED,
            )
        return snapshot.number

    def _bound(self, repository: str) -> None:
        if repository != self._repository:
            raise ChangeGitHubError("repository_binding_mismatch")


def _load_complete_tree(client: _Client, repository: str, commit_sha: str) -> _CompleteTree:
    commit = client.read(client.url(repository, f"commits/{commit_sha}"))
    root_tree_sha = _commit_tree(commit, commit_sha)
    recursive = client.read(
        client.url(repository, f"git/trees/{root_tree_sha}", {"recursive": "1"})
    )
    raw_entries, truncated = _tree_response(
        recursive, root_tree_sha, direct=False
    )
    if truncated:
        entries, declared_sizes = _walk_trees(client, repository, root_tree_sha)
    else:
        entries, declared_sizes = _validate_recursive_entries(raw_entries, root_tree_sha)
    blobs = _read_blobs(client, repository, entries, declared_sizes)
    rebuilt = _rebuild_tree_oid(entries, blobs, len(root_tree_sha))
    if rebuilt != root_tree_sha:
        raise ChangeGitHubError("tree_oid_mismatch")
    return _CompleteTree(commit_sha, root_tree_sha, entries, blobs)


def _walk_trees(
    client: _Client, repository: str, root_tree_sha: str
) -> tuple[tuple[SourceTreeEntry, ...], dict[str, int]]:
    pending: list[tuple[str, str, tuple[str, ...]]] = [("", root_tree_sha, ())]
    entries: list[SourceTreeEntry] = []
    sizes: dict[str, int] = {}
    seen: set[str] = set()
    requests = 0
    while pending:
        prefix, tree_sha, ancestors = pending.pop(0)
        requests += 1
        if requests > _MAX_TREE_REQUESTS:
            raise ChangeGitHubError("tree_budget_exceeded")
        payload = client.read(client.url(repository, f"git/trees/{tree_sha}"))
        raw_entries, truncated = _tree_response(payload, tree_sha, direct=True)
        if truncated:
            raise ChangeGitHubError("tree_incomplete")
        for raw in raw_entries:
            item, size = _tree_entry(raw, direct=True, oid_length=len(root_tree_sha))
            full_path = f"{prefix}/{item.path}" if prefix else item.path
            full_path = _tree_path(full_path)
            key = _path_key(full_path)
            if key in seen:
                raise ChangeGitHubError("tree_incomplete")
            seen.add(key)
            full = SourceTreeEntry(full_path, item.mode, item.kind, item.git_sha)
            entries.append(full)
            if len(entries) > _MAX_TREE_ENTRIES:
                raise ChangeGitHubError("tree_budget_exceeded")
            if item.kind == "blob":
                assert size is not None
                sizes[full_path] = size
            else:
                if item.git_sha == tree_sha or item.git_sha in ancestors:
                    raise ChangeGitHubError("tree_cycle")
                pending.append((full_path, item.git_sha, ancestors + (tree_sha,)))
    return tuple(sorted(entries, key=lambda item: item.path)), sizes


def _validate_recursive_entries(
    raw_entries: tuple[Mapping[str, object], ...], root_tree_sha: str
) -> tuple[tuple[SourceTreeEntry, ...], dict[str, int]]:
    if len(raw_entries) > _MAX_TREE_ENTRIES:
        raise ChangeGitHubError("tree_budget_exceeded")
    entries: list[SourceTreeEntry] = []
    sizes: dict[str, int] = {}
    seen: dict[str, SourceTreeEntry] = {}
    for raw in raw_entries:
        entry, size = _tree_entry(raw, direct=False, oid_length=len(root_tree_sha))
        key = _path_key(entry.path)
        if key in seen:
            raise ChangeGitHubError("tree_incomplete")
        seen[key] = entry
        entries.append(entry)
        if entry.kind == "blob":
            assert size is not None
            sizes[entry.path] = size
    for entry in entries:
        if entry.kind == "tree" and entry.git_sha == root_tree_sha:
            raise ChangeGitHubError("tree_cycle")
        parent = PurePosixPath(entry.path).parent
        if parent == PurePosixPath("."):
            continue
        parent_entry = seen.get(_path_key(parent.as_posix()))
        if parent_entry is None or parent_entry.kind != "tree" or parent_entry.path != parent.as_posix():
            raise ChangeGitHubError("tree_incomplete")
        if entry.kind == "tree":
            ancestor = parent
            ancestor_oids = {root_tree_sha}
            while ancestor != PurePosixPath("."):
                value = seen.get(_path_key(ancestor.as_posix()))
                if value is not None:
                    ancestor_oids.add(value.git_sha)
                ancestor = ancestor.parent
            if entry.git_sha in ancestor_oids:
                raise ChangeGitHubError("tree_cycle")
    return tuple(sorted(entries, key=lambda item: item.path)), sizes


def _tree_response(
    payload: object, expected_sha: str, *, direct: bool
) -> tuple[tuple[Mapping[str, object], ...], bool]:
    value = _mapping(payload, "invalid_tree_response")
    required = {"sha", "tree", "truncated"}
    if not required.issubset(value) or not set(value).issubset(required | {"url"}):
        raise ChangeGitHubError("invalid_tree_response")
    actual = _oid(value.get("sha"), "invalid_tree_response")
    if actual != expected_sha:
        raise ChangeGitHubError("tree_sha_mismatch")
    truncated = value.get("truncated")
    if type(truncated) is not bool:
        raise ChangeGitHubError("invalid_tree_response")
    items = _list(value.get("tree"), "invalid_tree_response")
    if direct and any("/" in _string(item.get("path"), "unsafe_tree_path") for item in items):
        raise ChangeGitHubError("unsafe_tree_path")
    return items, truncated


def _tree_entry(
    raw: Mapping[str, object], *, direct: bool, oid_length: int
) -> tuple[SourceTreeEntry, int | None]:
    required = {"path", "mode", "type", "sha"}
    allowed = required | {"size", "url"}
    if not required.issubset(raw) or not set(raw).issubset(allowed):
        raise ChangeGitHubError("invalid_tree_response")
    path = _tree_path(_string(raw.get("path"), "unsafe_tree_path"))
    if direct and "/" in path:
        raise ChangeGitHubError("unsafe_tree_path")
    mode = _string(raw.get("mode"), "unsupported_tree_entry")
    kind = _string(raw.get("type"), "unsupported_tree_entry")
    oid = _oid(raw.get("sha"), "invalid_tree_response")
    if len(oid) != oid_length:
        raise ChangeGitHubError("invalid_tree_response")
    size: int | None = None
    if kind == "blob" and mode in _BLOB_MODES:
        size = _nonnegative_int(raw.get("size"), "invalid_tree_response")
        if size > _MAX_BLOB_BYTES:
            raise ChangeGitHubError("tree_budget_exceeded")
    elif kind == "tree" and mode == _TREE_MODE:
        if "size" in raw and raw["size"] is not None:
            raise ChangeGitHubError("invalid_tree_response")
    else:
        raise ChangeGitHubError("unsupported_tree_entry")
    return SourceTreeEntry(path, mode, kind, oid), size


def _read_blobs(
    client: _Client,
    repository: str,
    entries: tuple[SourceTreeEntry, ...],
    sizes: Mapping[str, int],
) -> tuple[SourceBlob, ...]:
    total = sum(sizes.values())
    if total > _MAX_TOTAL_BLOB_BYTES:
        raise ChangeGitHubError("tree_budget_exceeded")
    blobs: list[SourceBlob] = []
    for entry in entries:
        if entry.kind != "blob":
            continue
        payload = _mapping(
            client.read(client.url(repository, f"git/blobs/{entry.git_sha}")),
            "invalid_blob_response",
        )
        required = {"sha", "size", "encoding", "content"}
        if not required.issubset(payload) or not set(payload).issubset(
            required | {"node_id", "url"}
        ):
            raise ChangeGitHubError("invalid_blob_response")
        if "node_id" in payload:
            _node_id(payload.get("node_id"), "invalid_blob_response")
        if _oid(payload.get("sha"), "invalid_blob_response") != entry.git_sha:
            raise ChangeGitHubError("blob_oid_mismatch")
        if payload.get("encoding") != "base64":
            raise ChangeGitHubError("invalid_blob_response")
        declared = _nonnegative_int(payload.get("size"), "invalid_blob_response")
        if declared != sizes[entry.path]:
            raise ChangeGitHubError("blob_size_mismatch")
        encoded = payload.get("content")
        if type(encoded) is not str:
            raise ChangeGitHubError("invalid_blob_response")
        if any(character.isspace() and character not in "\r\n" for character in encoded):
            raise ChangeGitHubError("invalid_blob_response")
        try:
            content = base64.b64decode(encoded.replace("\r", "").replace("\n", ""), validate=True)
        except (ValueError, TypeError):
            raise ChangeGitHubError("invalid_blob_response") from None
        if len(content) != declared:
            raise ChangeGitHubError("blob_size_mismatch")
        if _git_oid("blob", content, len(entry.git_sha)) != entry.git_sha:
            raise ChangeGitHubError("blob_oid_mismatch")
        hashlib.sha256(content).hexdigest()
        blobs.append(SourceBlob(entry.path, entry.mode, content, entry.git_sha))
    return tuple(blobs)


def _rebuild_tree_oid(
    entries: tuple[SourceTreeEntry, ...], blobs: tuple[SourceBlob, ...], oid_length: int
) -> str:
    blob_by_path = {blob.path: blob for blob in blobs}
    children: dict[str, list[SourceTreeEntry]] = {"": []}
    tree_entries: dict[str, SourceTreeEntry] = {}
    for entry in entries:
        if entry.kind == "tree":
            children[entry.path] = []
            tree_entries[entry.path] = entry
    for entry in entries:
        parent = entry.path.rpartition("/")[0]
        if parent not in children:
            raise ChangeGitHubError("tree_incomplete")
        children[parent].append(entry)
    rebuilt: dict[str, str] = {}
    directories = sorted(tree_entries, key=lambda value: (value.count("/"), value.encode("utf-8")), reverse=True)
    for directory in directories + [""]:
        items: list[tuple[str, str, str, str]] = []
        for entry in children[directory]:
            name = entry.path.rsplit("/", 1)[-1]
            if entry.kind == "blob":
                blob = blob_by_path.get(entry.path)
                if blob is None or blob.mode != entry.mode or blob.git_sha != entry.git_sha:
                    raise ChangeGitHubError("tree_incomplete")
                oid = blob.git_sha
            else:
                oid = rebuilt.get(entry.path)
                if oid is None or oid != entry.git_sha:
                    raise ChangeGitHubError("tree_oid_mismatch")
            items.append((name, entry.kind, entry.mode, oid))
        rebuilt[directory] = _git_tree_oid(tuple(items), oid_length)
    return rebuilt[""]


def _git_tree_oid(items: tuple[tuple[str, str, str, str], ...], length: int) -> str:
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
    return _git_oid("tree", payload, length)


def _git_oid(kind: str, content: bytes, length: int) -> str:
    encoded = kind.encode("ascii") + b" " + str(len(content)).encode("ascii") + b"\0" + content
    if length == 40:
        return hashlib.sha1(encoded).hexdigest()
    if length == 64:
        return hashlib.sha256(encoded).hexdigest()
    raise ChangeGitHubError("invalid_git_oid")


def _commit_tree(payload: object, expected_commit: str) -> str:
    value = _mapping(payload, "invalid_commit_response")
    actual = _oid(value.get("sha"), "invalid_commit_response")
    if actual != expected_commit:
        raise ChangeGitHubError("commit_sha_mismatch")
    commit = _mapping(value.get("commit"), "invalid_commit_response")
    tree = _mapping(commit.get("tree"), "invalid_commit_response")
    tree_sha = _oid(tree.get("sha"), "invalid_commit_response")
    if len(tree_sha) != len(actual):
        raise ChangeGitHubError("invalid_commit_response")
    return tree_sha


def _pull_data(payload: object, repository: str) -> dict[str, object]:
    value = _mapping(payload, "invalid_pull_response")
    head = _mapping(value.get("head"), "invalid_pull_response")
    base = _mapping(value.get("base"), "invalid_pull_response")
    head_repo = _mapping(head.get("repo"), "invalid_pull_response")
    base_repo = _mapping(base.get("repo"), "invalid_pull_response")
    result: dict[str, object] = {
        "number": _positive_int(value.get("number"), "invalid_pull_response"),
        "state": _string(value.get("state"), "invalid_pull_response"),
        "draft": _bool(value.get("draft"), "invalid_pull_response"),
        "title": _string(value.get("title", ""), "invalid_pull_response"),
        "body": _optional_string(value.get("body"), "invalid_pull_response") or "",
        "head_repository": _string(head_repo.get("full_name"), "invalid_pull_response"),
        "head_sha": _oid(head.get("sha"), "invalid_pull_response"),
        "head_ref": _string(head.get("ref"), "invalid_pull_response"),
        "base_repository": _string(base_repo.get("full_name"), "invalid_pull_response"),
        "base_sha": _oid(base.get("sha"), "invalid_pull_response"),
        "base_ref": _string(base.get("ref"), "invalid_pull_response"),
    }
    _repository(result["head_repository"])
    _repository(result["base_repository"])
    _ref(result["head_ref"])
    _ref(result["base_ref"])
    if result["base_repository"] != repository:
        raise ChangeGitHubError("pull_repository_mismatch")
    return result


def _published_pull(payload: object, repository: str) -> tuple[PublishedPullSnapshot, str]:
    value = _mapping(payload, "invalid_published_pull")
    head = _mapping(value.get("head"), "invalid_published_pull")
    base = _mapping(value.get("base"), "invalid_published_pull")
    user = _mapping(value.get("user"), "invalid_published_pull")
    head_repo = _mapping(head.get("repo"), "invalid_published_pull")
    base_repo = _mapping(base.get("repo"), "invalid_published_pull")
    head_repository = _string(head_repo.get("full_name"), "invalid_published_pull")
    base_repository = _string(base_repo.get("full_name"), "invalid_published_pull")
    _repository(head_repository)
    _repository(base_repository)
    if base_repository != repository:
        raise ChangeGitHubError("pull_repository_mismatch")
    head_ref = _string(head.get("ref"), "invalid_published_pull")
    base_ref = _string(base.get("ref"), "invalid_published_pull")
    _ref(head_ref)
    _ref(base_ref)
    body = _optional_string(value.get("body"), "invalid_published_pull") or ""
    return (
        PublishedPullSnapshot(
            _positive_int(value.get("number"), "invalid_published_pull"),
            _string(value.get("state"), "invalid_published_pull"),
            _bool(value.get("draft"), "invalid_published_pull"),
            _string(user.get("login"), "invalid_published_pull"),
            head_repository,
            head_ref,
            _oid(head.get("sha"), "invalid_published_pull"),
            base_ref,
            _oid(base.get("sha"), "invalid_published_pull"),
        ),
        body,
    )


def _marker_matches(
    body: str, repository: str, branch: str, base_ref: str, run_id: str
) -> bool:
    matches = re.findall(r"<!-- qykw-change:v1 (\{[^\r\n]*\}) -->", body)
    for encoded in matches:
        try:
            value = json.loads(encoded)
        except (ValueError, RecursionError):
            continue
        if type(value) is dict and all(
            value.get(key) == expected
            for key, expected in {
                "target_repository": repository,
                "branch": branch,
                "base_ref": base_ref,
                "run": run_id,
            }.items()
        ):
            return True
    return False


def _next_link(value: str | None) -> str | None:
    if value is None:
        return None
    matches: list[str] = []
    for part in value.split(","):
        match = re.fullmatch(r'\s*<([^<>]+)>\s*;\s*rel="([^"]+)"\s*', part)
        if match is None:
            raise ChangeGitHubError("unsafe_pagination")
        if match.group(2) == "next":
            matches.append(match.group(1))
    if len(matches) > 1:
        raise ChangeGitHubError("unsafe_pagination")
    return matches[0] if matches else None


def _origin(value: object) -> str:
    if type(value) is not str:
        raise ChangeGitHubError("invalid_api_origin")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ChangeGitHubError("invalid_api_origin")
    netloc = parsed.hostname if parsed.port is None else f"{parsed.hostname}:{parsed.port}"
    return urlunsplit(("https", netloc, "", "", ""))


def _repository(value: object) -> str:
    if type(value) is not str or _REPOSITORY_RE.fullmatch(value) is None:
        raise ChangeGitHubError("invalid_repository")
    return value


def _login(value: object) -> str:
    if type(value) is not str or _LOGIN_RE.fullmatch(value) is None:
        raise ChangeGitHubError("invalid_login")
    return value


def _oid(value: object, code: str = "invalid_git_oid") -> str:
    if type(value) is not str or len(value) not in {40, 64} or any(char not in _HEX for char in value):
        raise ChangeGitHubError(code)
    return value


def _node_id(value: object, code: str) -> str:
    if (
        type(value) is not str
        or len(value) > 256
        or re.fullmatch(r"[A-Za-z0-9_+/-]+={0,2}", value) is None
    ):
        raise ChangeGitHubError(code)
    return value


def _ref(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or len(value.encode("utf-8")) > 256
        or value.startswith("/")
        or value.endswith("/")
        or value.endswith(".")
        or ".." in value
        or "@{" in value
        or "//" in value
        or "\\" in value
        or any(ord(char) < 32 or char.isspace() or char in "~^:?*[" for char in value)
    ):
        raise ChangeGitHubError("invalid_ref")
    return value


def _run_id(value: object) -> str:
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) is None:
        raise ChangeGitHubError("invalid_run_id")
    return value


def _tree_path(value: object) -> str:
    if type(value) is not str or not value or "\\" in value or "\x00" in value:
        raise ChangeGitHubError("unsafe_tree_path")
    if unicodedata.normalize("NFC", value) != value:
        raise ChangeGitHubError("unsafe_tree_path")
    path = PurePosixPath(value)
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise ChangeGitHubError("unsafe_tree_path") from None
    if (
        path.is_absolute()
        or path.as_posix() != value
        or len(encoded) > _MAX_PATH_BYTES
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(part.casefold() == ".git" for part in path.parts)
        or any(ord(char) < 32 for char in value)
    ):
        raise ChangeGitHubError("unsafe_tree_path")
    return value


def _path_key(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def _mapping(value: object, code: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ChangeGitHubError(code)
    return value


def _list(value: object, code: str) -> tuple[Mapping[str, object], ...]:
    if type(value) is not list:
        raise ChangeGitHubError(code)
    return tuple(_mapping(item, code) for item in value)


def _string(value: object, code: str) -> str:
    if type(value) is not str or not value:
        raise ChangeGitHubError(code)
    return value


def _optional_string(value: object, code: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise ChangeGitHubError(code)
    return value


def _bool(value: object, code: str) -> bool:
    if type(value) is not bool:
        raise ChangeGitHubError(code)
    return value


def _positive_int(value: object, code: str) -> int:
    if type(value) is not int or value <= 0:
        raise ChangeGitHubError(code)
    return value


def _nonnegative_int(value: object, code: str) -> int:
    if type(value) is not int or value < 0:
        raise ChangeGitHubError(code)
    return value


def _safe_text(value: object) -> bool:
    if type(value) is not str or not value or "\x00" in value:
        return False
    try:
        return len(value.encode("utf-8")) <= _MAX_WRITE_TEXT_BYTES
    except UnicodeError:
        return False
