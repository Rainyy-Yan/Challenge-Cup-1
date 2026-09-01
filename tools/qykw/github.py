"""Narrow, typed GitHub API gateway for qykw review work.

The gateway deliberately has no general URL-fetching surface.  All requests are
constructed from a configured GitHub API origin, one repository, and validated
PR/source references.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import re
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from tools.qykw.domain import (
    AuthenticatedUser,
    ChangedFile,
    CheckRun,
    DiffSide,
    InlineComment,
    IssueComment,
    PullRef,
    PullSnapshot,
    ReactionResult,
    RepositoryFile,
    RepositoryPermission,
    ReviewComment,
    RunContext,
    TriggerRef,
)


class GitHubError(RuntimeError):
    """Safe, typed gateway failure which contains no token or response body."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class GitHubGateway(Protocol):
    """The complete controller allowlist; no mutation outside review publishing."""

    def get_pull_ref(self, pr_number: int) -> PullRef: ...

    def get_pull_snapshot(self, pr_number: int, *, run: RunContext) -> PullSnapshot: ...

    def get_head_sha(self, pr_number: int) -> str: ...

    def get_actor_permission(self, login: str) -> RepositoryPermission: ...

    def get_authenticated_user(self) -> AuthenticatedUser: ...

    def assert_bot_identity(
        self, expected_login: str = "qykw"
    ) -> AuthenticatedUser: ...

    def try_add_reaction(
        self, trigger: TriggerRef, content: str = "laugh"
    ) -> ReactionResult: ...

    def list_issue_comments(self, pr_number: int) -> tuple[IssueComment, ...]: ...

    def list_review_comments(self, pr_number: int) -> tuple[ReviewComment, ...]: ...

    def list_changed_files(self, pr_number: int) -> tuple[ChangedFile, ...]: ...

    def list_check_runs(self, head_sha: str) -> tuple[CheckRun, ...]: ...

    def get_file_for_run(
        self, run: RunContext, path: str, side: DiffSide
    ) -> RepositoryFile | None: ...

    def get_default_branch_rules(self) -> tuple[RepositoryFile, ...]: ...

    def create_issue_comment(self, pr_number: int, body: str) -> int: ...

    def update_issue_comment(self, comment_id: int, body: str) -> None: ...

    def create_review(
        self,
        pr_number: int,
        *,
        head_sha: str,
        body: str,
        comments: tuple[InlineComment, ...],
    ) -> int: ...


Transport = Callable[[str, str, Mapping[str, str], bytes | None], tuple[int, Mapping[str, str], bytes]]

_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_PAGES = 1000
_TRUSTED_RULE_PATHS = ("AGENTS.md", ".github/qykw.toml")


@dataclass(frozen=True)
class GitHubPullSnapshot(PullSnapshot):
    """A fixed snapshot with immutable collection omissions for context coverage."""

    omissions: tuple[str, ...] = ()


@dataclass(frozen=True, repr=False)
class HttpGitHubGateway:
    """GitHub REST gateway with an injectable, redirect-free transport."""

    api_url: str
    repository: str
    read_token: str
    review_token: str
    transport: Transport | None = None
    default_branch: str = "main"

    def __post_init__(self) -> None:
        origin = _validate_origin(self.api_url)
        _validate_repository(self.repository)
        _validate_ref(self.default_branch)
        object.__setattr__(self, "api_url", origin)
        if self.transport is None:
            object.__setattr__(self, "transport", _stdlib_transport)

    def __repr__(self) -> str:
        return f"HttpGitHubGateway(api_url={self.api_url!r}, repository={self.repository!r})"

    def get_pull_ref(self, pr_number: int) -> PullRef:
        payload = self._read_json(self._repo_path(f"pulls/{_pr_number(pr_number)}"))
        return self._parse_pull_ref(payload, pr_number)

    def get_pull_snapshot(self, pr_number: int, *, run: RunContext) -> PullSnapshot:
        if not isinstance(run, RunContext):
            raise GitHubError("invalid_run_context")
        pull_payload = self._read_json(self._repo_path(f"pulls/{_pr_number(pr_number)}"))
        pull_ref = self._parse_pull_ref(pull_payload, pr_number)
        self._assert_run_matches(run, pull_ref)
        changed = self._list_changed_file_payloads(pr_number)
        base_tree, base_tree_omissions = self._get_tree(
            pull_ref.target_repository, pull_ref.target_base_sha, "base"
        )
        head_tree, head_tree_omissions = self._get_tree(
            pull_ref.source_repository, pull_ref.source_head_sha, "head"
        )
        enriched, omissions = self._enrich_changed_files(
            changed, pull_ref, base_tree, head_tree
        )
        checks = self.list_check_runs(pull_ref.source_head_sha)
        rules, rule_omissions = self._get_default_branch_rules_with_omissions()
        payload = _mapping(pull_payload, "invalid_pull")
        return GitHubPullSnapshot(
            number=pull_ref.number,
            state=pull_ref.state,
            draft=pull_ref.draft,
            source_repository=pull_ref.source_repository,
            source_head_sha=pull_ref.source_head_sha,
            target_repository=pull_ref.target_repository,
            target_base_sha=pull_ref.target_base_sha,
            target_base_ref=pull_ref.target_base_ref,
            title=_string(payload.get("title"), "invalid_pull"),
            body=_optional_string(payload.get("body"), "invalid_pull") or "",
            changed_files=tuple(enriched),
            trusted_rules=rules,
            related_files=(),
            checks=checks,
            omissions=tuple(base_tree_omissions + head_tree_omissions + omissions + rule_omissions),
        )

    def get_head_sha(self, pr_number: int) -> str:
        return self.get_pull_ref(pr_number).source_head_sha

    def get_actor_permission(self, login: str) -> RepositoryPermission:
        _validate_login(login)
        payload = self._read_json(
            self._repo_path(f"collaborators/{quote(login, safe='')}/permission")
        )
        permission = _string(_mapping(payload, "invalid_permission").get("permission"), "invalid_permission")
        try:
            return RepositoryPermission(permission)
        except ValueError as error:
            raise GitHubError("invalid_permission") from error

    def get_authenticated_user(self) -> AuthenticatedUser:
        payload = _mapping(
            self._read_json(f"{self.api_url}/user", token="review"),
            "invalid_authenticated_user",
        )
        return AuthenticatedUser(
            login=_string(payload.get("login"), "invalid_authenticated_user"),
            database_id=_positive_int(payload.get("id"), "invalid_authenticated_user"),
        )

    def assert_bot_identity(self, expected_login: str = "qykw") -> AuthenticatedUser:
        _validate_login(expected_login)
        user = self.get_authenticated_user()
        if user.login != expected_login:
            raise GitHubError("bot_identity_mismatch")
        return user

    def try_add_reaction(
        self, trigger: TriggerRef, content: str = "laugh"
    ) -> ReactionResult:
        self.assert_bot_identity()
        _positive_int(trigger.node_id, "invalid_trigger")
        reaction_paths = {
            "issue_comment": f"issues/comments/{trigger.node_id}/reactions",
            "review_comment": f"pulls/comments/{trigger.node_id}/reactions",
        }
        path = reaction_paths.get(trigger.kind)
        if path is None or content != "laugh":
            raise GitHubError("invalid_trigger")
        try:
            self._write_json("POST", self._repo_path(path), {"content": content})
        except GitHubError:
            return ReactionResult(warning_code="reaction_failed")
        return ReactionResult(warning_code=None)

    def list_issue_comments(self, pr_number: int) -> tuple[IssueComment, ...]:
        payloads = self._paginate_list(self._repo_path(f"issues/{_pr_number(pr_number)}/comments?per_page=100"))
        return tuple(
            IssueComment(
                comment_id=_positive_int(item.get("id"), "invalid_issue_comment"),
                author_login=_string(_mapping(item.get("user"), "invalid_issue_comment").get("login"), "invalid_issue_comment"),
                body=_optional_string(item.get("body"), "invalid_issue_comment") or "",
                updated_at=_string(item.get("updated_at"), "invalid_issue_comment"),
            )
            for item in payloads
        )

    def list_review_comments(self, pr_number: int) -> tuple[ReviewComment, ...]:
        payloads = self._paginate_list(self._repo_path(f"pulls/{_pr_number(pr_number)}/comments?per_page=100"))
        parsed: list[ReviewComment] = []
        for item in payloads:
            side = _diff_side(item.get("side"))
            parsed.append(
                ReviewComment(
                    comment_id=_positive_int(item.get("id"), "invalid_review_comment"),
                    author_login=_string(_mapping(item.get("user"), "invalid_review_comment").get("login"), "invalid_review_comment"),
                    body=_optional_string(item.get("body"), "invalid_review_comment") or "",
                    updated_at=_string(item.get("updated_at"), "invalid_review_comment"),
                    path=_safe_path(_string(item.get("path"), "invalid_review_comment")),
                    line=_positive_int(item.get("line"), "invalid_review_comment"),
                    side=side,
                )
            )
        return tuple(parsed)

    def list_changed_files(self, pr_number: int) -> tuple[ChangedFile, ...]:
        return tuple(self._changed_file(item) for item in self._list_changed_file_payloads(pr_number))

    def list_check_runs(self, head_sha: str) -> tuple[CheckRun, ...]:
        _validate_ref(head_sha)
        url = self._repo_path(f"commits/{quote(head_sha, safe='')}/check-runs?per_page=100")
        payloads = self._paginate_list(url, list_key="check_runs")
        return tuple(
            CheckRun(
                name=_string(item.get("name"), "invalid_check_run"),
                status=_string(item.get("status"), "invalid_check_run"),
                conclusion=_optional_string(item.get("conclusion"), "invalid_check_run"),
            )
            for item in payloads
        )

    def get_file_for_run(
        self, run: RunContext, path: str, side: DiffSide
    ) -> RepositoryFile | None:
        if not isinstance(run, RunContext):
            raise GitHubError("invalid_run_context")
        path = _safe_path(path)
        pull_ref = self.get_pull_ref(run.pr_number)
        self._assert_run_matches(run, pull_ref)
        if side is DiffSide.LEFT:
            return self._get_file_at_ref(
                path, pull_ref.target_base_sha, repository=pull_ref.target_repository, purpose="base"
            )
        if side is DiffSide.RIGHT:
            return self._get_file_at_ref(
                path, pull_ref.source_head_sha, repository=pull_ref.source_repository, purpose="head"
            )
        raise GitHubError("invalid_diff_side")

    def get_default_branch_rules(self) -> tuple[RepositoryFile, ...]:
        rules, _ = self._get_default_branch_rules_with_omissions()
        return rules

    def _get_default_branch_rules_with_omissions(
        self,
    ) -> tuple[tuple[RepositoryFile, ...], list[str]]:
        rules: list[RepositoryFile] = []
        omissions: list[str] = []
        for path in _TRUSTED_RULE_PATHS:
            file = self._get_file_at_ref(
                path,
                self.default_branch,
                repository=self.repository,
                purpose="trusted_rule",
            )
            if file is not None:
                rules.append(file)
            else:
                omissions.append(f"trusted_rule_missing:{path}")
        return tuple(rules), omissions

    def create_issue_comment(self, pr_number: int, body: str) -> int:
        self.assert_bot_identity()
        payload = self._write_json(
            "POST",
            self._repo_path(f"issues/{_pr_number(pr_number)}/comments"),
            {"body": _write_body(body)},
        )
        return _positive_int(_mapping(payload, "invalid_comment_response").get("id"), "invalid_comment_response")

    def update_issue_comment(self, comment_id: int, body: str) -> None:
        self.assert_bot_identity()
        self._write_json(
            "PATCH",
            self._repo_path(f"issues/comments/{_positive_int(comment_id, 'invalid_comment_id')}"),
            {"body": _write_body(body)},
        )

    def create_review(
        self,
        pr_number: int,
        *,
        head_sha: str,
        body: str,
        comments: tuple[InlineComment, ...],
    ) -> int:
        _validate_ref(head_sha)
        serialized_comments = []
        for comment in comments:
            serialized_comments.append(
                {
                    "path": _safe_path(comment.path),
                    "line": _positive_int(comment.line, "invalid_inline_comment"),
                    "side": _diff_side(comment.side).value,
                    "body": _write_body(comment.body),
                }
            )
        current = self.get_pull_ref(pr_number)
        if current.source_head_sha != head_sha:
            raise GitHubError("stale_pull_ref")
        self.assert_bot_identity()
        payload = self._write_json(
            "POST",
            self._repo_path(f"pulls/{_pr_number(pr_number)}/reviews"),
            {
                "commit_id": head_sha,
                "body": _write_body(body),
                "event": "COMMENT",
                "comments": serialized_comments,
            },
        )
        return _positive_int(_mapping(payload, "invalid_review_response").get("id"), "invalid_review_response")

    def _parse_pull_ref(self, payload: object, expected_number: int) -> PullRef:
        value = _mapping(payload, "invalid_pull")
        if _positive_int(value.get("number"), "invalid_pull") != expected_number:
            raise GitHubError("pull_number_mismatch")
        state = _string(value.get("state"), "invalid_pull")
        if state != "open" or value.get("merged") is True:
            raise GitHubError("pull_not_open")
        head = _mapping(value.get("head"), "invalid_pull")
        base = _mapping(value.get("base"), "invalid_pull")
        source_repository = _repository_name(head.get("repo"), "invalid_pull")
        target_repository = _repository_name(base.get("repo"), "invalid_pull")
        if target_repository != self.repository:
            raise GitHubError("pull_repository_mismatch")
        source_head_sha = _string(head.get("sha"), "invalid_pull")
        source_head_ref = _string(head.get("ref"), "invalid_pull")
        target_base_sha = _string(base.get("sha"), "invalid_pull")
        target_base_ref = _string(base.get("ref"), "invalid_pull")
        _validate_ref(source_head_sha)
        _validate_ref(source_head_ref)
        _validate_ref(target_base_sha)
        _validate_ref(target_base_ref)
        return PullRef(
            number=expected_number,
            state=state,
            draft=_bool(value.get("draft"), "invalid_pull"),
            source_repository=source_repository,
            source_head_sha=source_head_sha,
            target_repository=target_repository,
            target_base_sha=target_base_sha,
            target_base_ref=target_base_ref,
        )

    def _assert_run_matches(self, run: RunContext, pull_ref: PullRef) -> None:
        if (
            run.repository != self.repository
            or run.pr_number != pull_ref.number
            or run.source_repository != pull_ref.source_repository
            or run.source_head_sha != pull_ref.source_head_sha
            or run.target_base_sha != pull_ref.target_base_sha
            or run.target_base_ref != pull_ref.target_base_ref
        ):
            raise GitHubError("stale_pull_ref")

    def _list_changed_file_payloads(self, pr_number: int) -> tuple[Mapping[str, object], ...]:
        return self._paginate_list(self._repo_path(f"pulls/{_pr_number(pr_number)}/files?per_page=100"))

    def _changed_file(self, item: Mapping[str, object]) -> ChangedFile:
        path = _safe_path(_string(item.get("filename"), "invalid_changed_file"))
        previous = item.get("previous_filename")
        previous_path = _safe_path(previous) if isinstance(previous, str) else None
        status = _string(item.get("status"), "invalid_changed_file")
        if status not in {"added", "modified", "removed", "renamed", "copied", "changed", "unchanged"}:
            raise GitHubError("invalid_changed_file")
        patch = _optional_string(item.get("patch"), "invalid_changed_file")
        return ChangedFile(
            path=path,
            previous_path=previous_path,
            status=status,
            base_sha=None,
            head_sha=_optional_string(item.get("sha"), "invalid_changed_file"),
            base_mode=_optional_string(item.get("base_mode"), "invalid_changed_file"),
            head_mode=_optional_string(item.get("mode"), "invalid_changed_file"),
            base_content=None,
            head_content=None,
            patch=patch,
            binary=patch is None,
            generated=bool(item.get("generated", False)),
            additions=_nonnegative_int(item.get("additions"), "invalid_changed_file"),
            deletions=_nonnegative_int(item.get("deletions"), "invalid_changed_file"),
        )

    def _enrich_changed_files(
        self,
        payloads: tuple[Mapping[str, object], ...],
        pull_ref: PullRef,
        base_tree: Mapping[str, tuple[str, str]],
        head_tree: Mapping[str, tuple[str, str]],
    ) -> tuple[list[ChangedFile], list[str]]:
        files: list[ChangedFile] = []
        omissions: list[str] = []
        for payload in payloads:
            file = self._changed_file(payload)
            base_path = file.previous_path or file.path
            base_metadata = base_tree.get(base_path)
            head_metadata = head_tree.get(file.path)
            base_file = None if file.status == "added" else self._get_file_at_ref(
                base_path, pull_ref.target_base_sha, repository=pull_ref.target_repository, purpose="base"
            )
            head_file = None if file.status == "removed" else self._get_file_at_ref(
                file.path, pull_ref.source_head_sha, repository=pull_ref.source_repository, purpose="head"
            )
            if file.status != "added" and base_file is None:
                omissions.append(f"base_content_missing:{base_path}")
            if file.status != "removed" and head_file is None:
                omissions.append(f"head_content_missing:{file.path}")
            if file.status != "added" and base_metadata is None:
                omissions.append(f"base_mode_missing:{base_path}")
            if file.status != "removed" and head_metadata is None:
                omissions.append(f"head_mode_missing:{file.path}")
            files.append(
                ChangedFile(
                    path=file.path,
                    previous_path=file.previous_path,
                    status=file.status,
                    base_sha=base_metadata[0] if base_metadata else (base_file.sha if base_file else None),
                    head_sha=head_metadata[0] if head_metadata else (head_file.sha if head_file else file.head_sha),
                    base_mode=base_metadata[1] if base_metadata else None,
                    head_mode=head_metadata[1] if head_metadata else None,
                    base_content=base_file.content if base_file else None,
                    head_content=head_file.content if head_file else None,
                    patch=file.patch,
                    binary=file.binary,
                    generated=file.generated,
                    additions=file.additions,
                    deletions=file.deletions,
                )
            )
        return files, omissions

    def _get_tree(
        self, repository: str, ref: str, label: str
    ) -> tuple[Mapping[str, tuple[str, str]], list[str]]:
        _validate_repository(repository)
        _validate_ref(ref)
        payload = _mapping(
            self._read_json(
                self._repo_path(
                    f"git/trees/{quote(ref, safe='')}?recursive=1", repository=repository
                )
            ),
            "invalid_tree",
        )
        entries = payload.get("tree")
        if not isinstance(entries, list):
            raise GitHubError("invalid_tree")
        tree: dict[str, tuple[str, str]] = {}
        for entry in entries:
            value = _mapping(entry, "invalid_tree")
            if value.get("type") != "blob":
                continue
            path = _safe_path(_string(value.get("path"), "invalid_tree"))
            if path in tree:
                raise GitHubError("invalid_tree")
            tree[path] = (
                _string(value.get("sha"), "invalid_tree"),
                _string(value.get("mode"), "invalid_tree"),
            )
        omissions = [f"{label}_tree_truncated"] if payload.get("truncated") is True else []
        return tree, omissions

    def _get_file_at_ref(
        self, path: str, ref: str, *, repository: str, purpose: str
    ) -> RepositoryFile | None:
        path = _safe_path(path)
        _validate_ref(ref)
        _validate_repository(repository)
        endpoint = self._repo_path(
            f"contents/{quote(path, safe='/')}?ref={quote(ref, safe='')}", repository=repository
        )
        payload = self._read_json(endpoint, allow_not_found=True)
        if payload is None:
            return None
        value = _mapping(payload, "invalid_repository_file")
        if value.get("type", "file") != "file":
            raise GitHubError("invalid_repository_file")
        encoded = _string(value.get("content"), "invalid_repository_file")
        if _string(value.get("encoding"), "invalid_repository_file") != "base64":
            raise GitHubError("invalid_repository_file")
        try:
            content = base64.b64decode(encoded.encode("ascii"), validate=True).decode("utf-8")
        except (UnicodeDecodeError, ValueError) as error:
            raise GitHubError("invalid_repository_file") from error
        return RepositoryFile(
            path=path,
            ref=ref,
            sha=_string(value.get("sha"), "invalid_repository_file"),
            content=content,
            purpose=purpose,
        )

    def _paginate_list(self, url: str, *, list_key: str | None = None) -> tuple[Mapping[str, object], ...]:
        initial = self._validate_url(url)
        expected_path = urlsplit(initial).path
        current = initial
        visited: set[str] = set()
        collected: list[Mapping[str, object]] = []
        for _ in range(_MAX_PAGES):
            if current in visited:
                raise GitHubError("unsafe_pagination")
            visited.add(current)
            payload, headers = self._read_json_with_headers(current)
            page = _list_payload(payload, list_key)
            collected.extend(page)
            next_url = _next_link(headers.get("link"))
            if next_url is None:
                return tuple(collected)
            try:
                candidate = self._validate_url(next_url)
            except GitHubError as error:
                raise GitHubError("unsafe_pagination") from error
            if urlsplit(candidate).path != expected_path:
                raise GitHubError("unsafe_pagination")
            current = candidate
        raise GitHubError("unsafe_pagination")

    def _read_json(self, url: str, *, token: str = "read", allow_not_found: bool = False) -> object | None:
        payload, _ = self._request("GET", url, token=token, allow_not_found=allow_not_found)
        return payload

    def _read_json_with_headers(self, url: str) -> tuple[object, Mapping[str, str]]:
        payload, headers = self._request("GET", url, token="read", allow_not_found=False)
        if payload is None:
            raise GitHubError("unexpected_not_found")
        return payload, headers

    def _write_json(self, method: str, url: str, value: Mapping[str, object]) -> object:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        payload, _ = self._request(method, url, token="review", body=body, allow_not_found=False)
        if payload is None:
            raise GitHubError("invalid_write_response")
        return payload

    def _request(
        self, method: str, url: str, *, token: str, body: bytes | None = None, allow_not_found: bool
    ) -> tuple[object | None, Mapping[str, str]]:
        if method not in {"GET", "POST", "PATCH"}:
            raise GitHubError("invalid_request")
        safe_url = self._validate_url(url)
        if body is not None and len(body) > _MAX_RESPONSE_BYTES:
            raise GitHubError("request_too_large")
        secret = self.read_token if token == "read" else self.review_token
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {secret}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        try:
            assert self.transport is not None
            status, response_headers, response_body = self.transport(method, safe_url, headers, body)
        except GitHubError:
            raise
        except Exception as error:
            raise GitHubError("transport_failed") from error
        if not isinstance(status, int) or not isinstance(response_body, bytes) or len(response_body) > _MAX_RESPONSE_BYTES:
            raise GitHubError("invalid_response")
        normalized_headers = {str(key).lower(): str(value) for key, value in response_headers.items()}
        if status == 404 and allow_not_found:
            return None, normalized_headers
        if status < 200 or status >= 300:
            raise GitHubError("github_http_error")
        try:
            return json.loads(response_body.decode("utf-8")), normalized_headers
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GitHubError("invalid_response") from error

    def _repo_path(self, suffix: str, *, repository: str | None = None) -> str:
        repo = self.repository if repository is None else repository
        _validate_repository(repo)
        if suffix.startswith("/") or "#" in suffix:
            raise GitHubError("invalid_request")
        return f"{self.api_url}/repos/{repo}/{suffix}"

    def _validate_url(self, url: str) -> str:
        parsed = urlsplit(url)
        origin = urlsplit(self.api_url)
        if (
            parsed.scheme == "https"
            and parsed.username is None
            and parsed.password is None
            and parsed.hostname == origin.hostname
            and parsed.port == origin.port
            and not parsed.fragment
            and parsed.path == f"{origin.path}/user"
            and not parsed.query
        ):
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.hostname != origin.hostname
            or parsed.port != origin.port
            or parsed.fragment
            or not parsed.path.startswith(f"{origin.path}/repos/")
        ):
            raise GitHubError("unsafe_url")
        expected_prefix = f"{origin.path}/repos/"
        relative = parsed.path[len(expected_prefix):]
        parts = relative.split("/")
        if len(parts) < 3 or parts[0] != self.repository.split("/")[0] or parts[1] != self.repository.split("/")[1]:
            # Source repositories are allowed only for contents reads created
            # internally from a PullRef; validate that they remain repo paths.
            if len(parts) < 4 or parts[2] not in {"contents", "git"}:
                raise GitHubError("unsafe_url")
            if parts[2] == "git" and (len(parts) != 5 or parts[3] != "trees"):
                raise GitHubError("unsafe_url")
            _validate_repository(f"{parts[0]}/{parts[1]}")
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def _validate_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise GitHubError("invalid_api_origin")
    return urlunsplit(("https", parsed.netloc, "", "", ""))


def _validate_repository(value: str) -> None:
    if not isinstance(value, str) or not _REPOSITORY_RE.fullmatch(value):
        raise GitHubError("invalid_repository")


def _validate_ref(value: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 256 or ".." in value or any(char.isspace() or ord(char) < 32 for char in value):
        raise GitHubError("invalid_ref")


def _validate_login(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", value):
        raise GitHubError("invalid_login")


def _safe_path(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("/")
        or "\\" in value
        or any(part in ("", ".", "..") for part in value.split("/"))
        or any(ord(char) < 32 for char in value)
    ):
        raise GitHubError("unsafe_path")
    return value


def _pr_number(value: int) -> int:
    return _positive_int(value, "invalid_pr_number")


def _mapping(value: object, code: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise GitHubError(code)
    return value


def _list_payload(value: object, list_key: str | None) -> tuple[Mapping[str, object], ...]:
    if list_key is not None:
        value = _mapping(value, "invalid_pagination").get(list_key)
    if not isinstance(value, list):
        raise GitHubError("invalid_pagination")
    return tuple(_mapping(item, "invalid_pagination") for item in value)


def _string(value: object, code: str) -> str:
    if not isinstance(value, str) or not value:
        raise GitHubError(code)
    return value


def _optional_string(value: object, code: str) -> str | None:
    if value is None:
        return None
    return _string(value, code)


def _bool(value: object, code: str) -> bool:
    if type(value) is not bool:
        raise GitHubError(code)
    return value


def _positive_int(value: object, code: str) -> int:
    if type(value) is not int or value <= 0:
        raise GitHubError(code)
    return value


def _nonnegative_int(value: object, code: str) -> int:
    if type(value) is not int or value < 0:
        raise GitHubError(code)
    return value


def _repository_name(value: object, code: str) -> str:
    name = _string(_mapping(value, code).get("full_name"), code)
    _validate_repository(name)
    return name


def _diff_side(value: object) -> DiffSide:
    if isinstance(value, DiffSide):
        return value
    try:
        return DiffSide(_string(value, "invalid_diff_side"))
    except ValueError as error:
        raise GitHubError("invalid_diff_side") from error


def _write_body(value: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 64 * 1024:
        raise GitHubError("invalid_write_body")
    return value


def _next_link(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise GitHubError("unsafe_pagination")
    next_urls: list[str] = []
    for item in value.split(","):
        match = re.fullmatch(r'\s*<([^<>\s]+)>\s*;\s*rel="([^"]+)"\s*', item)
        if match is None:
            raise GitHubError("unsafe_pagination")
        if match.group(2) == "next":
            next_urls.append(match.group(1))
    if len(next_urls) > 1:
        raise GitHubError("unsafe_pagination")
    return next_urls[0] if next_urls else None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: object, code: int, msg: str, headers: object, newurl: str) -> None:
        return None


def _stdlib_transport(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None
) -> tuple[int, Mapping[str, str], bytes]:
    request = Request(url, data=body, headers=dict(headers), method=method)
    opener = build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=20) as response:
            return response.status, dict(response.headers.items()), response.read(_MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        return error.code, dict(error.headers.items()) if error.headers else {}, error.read(_MAX_RESPONSE_BYTES + 1)
    except URLError as error:
        raise GitHubError("transport_failed") from error
