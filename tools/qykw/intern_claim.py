"""Pure, strict parsing boundary for qykw intern-claim events."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Protocol, TextIO
from urllib.parse import urlsplit
from urllib.parse import quote, urlunsplit
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from tools.qykw.domain import IssueComment


class InternCommand(str, Enum):
    ASSIGN = "/intern-assign"
    UNASSIGN = "/intern-unassign"
    STATUS = "/intern-status"


@dataclass(frozen=True)
class IssueCommentEvent:
    repository: str
    repository_id: int
    issue_number: int
    comment_id: int
    actor_login: str
    command: InternCommand
    created_at: str = ""


@dataclass(frozen=True)
class InternIssueComment(IssueComment):
    """Issue comment with immutable creation time for command eligibility."""

    created_at: str


@dataclass(frozen=True)
class PullLifecycleEvent:
    repository: str
    repository_id: int
    pull_number: int
    action: str


_COMMANDS = {command.value: command for command in InternCommand}
_PULL_ACTIONS = frozenset({"opened", "edited", "ready_for_review", "reopened", "closed"})
_CLOSING = re.compile(r"(?i)(?<![A-Za-z0-9_./-])closes[ \t]+#([1-9][0-9]*)(?![A-Za-z0-9_/-])")
_ISSUE_REF = re.compile(r"(?<![A-Za-z0-9_./-])#([0-9]+)(?![A-Za-z0-9_/-])")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_FENCE_END = re.compile(r"^ {0,3}(`+|~+)[ \t]*$")
_INTERN_LABELS = frozenset({"intern:claimable", "status:in-progress", "status:in-review", "status:blocked"})
_INTERN_OPERATIONS = frozenset({"assign", "unassign", "status", "pull"})
_INTERN_STAGES = frozenset({"pending", "reconciled", "failed", "conflict"})
_INTERN_MARKER = re.compile(r"<!--\s*qykw-intern:v1\s+(\{[^<>]{1,4096}\})\s*-->")
_LOGIN = re.compile(r"^[A-Za-z0-9-]{1,39}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
_MAX_INTERN_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_INTERN_WRITE_BYTES = 64 * 1024
_MAX_INTERN_PAGES = 1000
_MAX_EVENT_BYTES = 1024 * 1024
_MAX_OUTPUT_BYTES = 64 * 1024
_MAX_CLOSING_BODY_BYTES = 256 * 1024
_MAX_ISSUE_NUMBER = 999_999_999_999_999_999
_CLI_PHASES = frozenset({"issue-command", "resolve-pr", "reconcile-pr"})
_GITHUB_ACTION = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class InternError(RuntimeError):
    """A fixed-code failure at the intern GitHub boundary."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class IssueSnapshot:
    number: int
    state: str
    labels: tuple[str, ...]
    assignees: tuple[str, ...]
    is_pull: bool = False


@dataclass(frozen=True)
class PullSnapshot:
    number: int
    state: str
    merged: bool
    author_login: str
    body: str | None

    def __post_init__(self) -> None:
        if self.body is None:
            object.__setattr__(self, "body", "")
        elif not isinstance(self.body, str):
            raise InternError("invalid_pull")


@dataclass(frozen=True)
class InternRecord:
    """Version-one immutable operation identity plus its reconciliation stage."""

    repository_id: int
    repository: str
    issue_number: int
    trigger_comment_id: int
    actor_login: str
    operation: str
    claimant_login: str | None
    pull_number: int | None
    stage: str

    def __post_init__(self) -> None:
        _intern_positive(self.repository_id, "invalid_repository_id")
        _intern_repository(self.repository)
        _intern_positive(self.issue_number, "invalid_issue_number")
        _intern_positive(self.trigger_comment_id, "invalid_comment_id")
        _intern_login(self.actor_login)
        if self.operation not in _INTERN_OPERATIONS:
            raise InternError("invalid_operation")
        if self.claimant_login is not None:
            _intern_login(self.claimant_login)
        if self.pull_number is not None:
            _intern_positive(self.pull_number, "invalid_pull_number")
        if self.stage not in _INTERN_STAGES:
            raise InternError("invalid_stage")
        if self.operation == "assign" and self.claimant_login != self.actor_login:
            raise InternError("invalid_operation")
        if self.operation == "unassign" and self.claimant_login is None:
            raise InternError("invalid_operation")
        if self.operation == "pull" and self.pull_number is None:
            raise InternError("invalid_operation")

    @property
    def operation_key(self) -> tuple[int, int, int, str]:
        return (self.repository_id, self.issue_number, self.trigger_comment_id, self.operation)

    def marker(self) -> str:
        return encode_marker(self)


@dataclass(frozen=True)
class InternOutcome:
    """Bounded result of reconciling the Issue command queue."""

    issue_number: int
    processed_comment_ids: tuple[int, ...]
    status: str


class InternGateway(Protocol):
    repository: str

    def assert_bot_identity(self, expected_login: str = "qykw") -> None: ...
    def get_issue(self, issue_number: int) -> IssueSnapshot: ...
    def list_issue_comments(self, issue_number: int) -> tuple[IssueComment, ...]: ...
    def list_pull_comments(self, pull_number: int) -> tuple[IssueComment, ...]: ...
    def get_pull(self, pull_number: int) -> PullSnapshot: ...
    def has_reaction(self, comment_id: int, actor: str = "qykw", content: str = "laugh") -> bool: ...
    def add_reaction(self, comment_id: int) -> None: ...
    def add_assignee(self, issue_number: int, login: str) -> None: ...
    def remove_assignee(self, issue_number: int, login: str) -> None: ...
    def add_label(self, issue_number: int, label: str) -> None: ...
    def remove_label(self, issue_number: int, label: str) -> None: ...
    def create_comment(self, issue_number: int, body: str) -> int: ...
    def update_comment(self, comment_id: int, body: str) -> None: ...
    def close_issue(self, issue_number: int) -> None: ...


InternTransport = Callable[[str, str, Mapping[str, str], bytes | None], tuple[int, Mapping[str, str], bytes]]


@dataclass(frozen=True, repr=False)
class HttpInternGateway:
    """Fixed-path GitHub REST facade for intern-claim reconciliation only."""

    api_url: str
    repository: str
    token: str
    transport: InternTransport | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "api_url", _intern_origin(self.api_url))
        _intern_repository(self.repository)
        if not isinstance(self.token, str) or not self.token:
            raise InternError("invalid_token")
        if self.transport is None:
            object.__setattr__(self, "transport", _intern_stdlib_transport)

    def __repr__(self) -> str:
        return f"HttpInternGateway(api_url={self.api_url!r}, repository={self.repository!r})"

    def assert_bot_identity(self, expected_login: str = "qykw") -> None:
        _intern_login(expected_login)
        payload = _intern_mapping(self._read_json(f"{self.api_url}/user"), "invalid_authenticated_user")
        if _intern_string(payload.get("login"), "invalid_authenticated_user") != expected_login:
            raise InternError("bot_identity_mismatch")
        _intern_positive(payload.get("id"), "invalid_authenticated_user")

    def get_issue(self, issue_number: int) -> IssueSnapshot:
        number = _intern_positive(issue_number, "invalid_issue_number")
        payload = _intern_mapping(self._read_json(self._repo_path(f"issues/{number}")), "invalid_issue")
        if _intern_positive(payload.get("number"), "invalid_issue") != number:
            raise InternError("issue_number_mismatch")
        state = _intern_string(payload.get("state"), "invalid_issue")
        labels = _intern_names(payload.get("labels"), "invalid_issue")
        assignees = _intern_logins(payload.get("assignees"), "invalid_issue")
        return IssueSnapshot(number, state, labels, assignees, "pull_request" in payload)

    def list_issue_comments(self, issue_number: int) -> tuple[IssueComment, ...]:
        number = _intern_positive(issue_number, "invalid_issue_number")
        return self._comments(number)

    def list_pull_comments(self, pull_number: int) -> tuple[IssueComment, ...]:
        number = _intern_positive(pull_number, "invalid_pull_number")
        return self._comments(number)

    def get_pull(self, pull_number: int) -> PullSnapshot:
        number = _intern_positive(pull_number, "invalid_pull_number")
        payload = _intern_mapping(self._read_json(self._repo_path(f"pulls/{number}")), "invalid_pull")
        if _intern_positive(payload.get("number"), "invalid_pull") != number:
            raise InternError("pull_number_mismatch")
        base = _intern_mapping(payload.get("base"), "invalid_pull")
        repository = _intern_mapping(base.get("repo"), "invalid_pull").get("full_name")
        if repository != self.repository:
            raise InternError("pull_repository_mismatch")
        merged = payload.get("merged")
        if type(merged) is not bool:
            raise InternError("invalid_pull")
        body = payload.get("body")
        if body is not None and not isinstance(body, str):
            raise InternError("invalid_pull")
        return PullSnapshot(number, _intern_string(payload.get("state"), "invalid_pull"), merged,
                            _intern_login(_intern_mapping(payload.get("user"), "invalid_pull").get("login")),
                            body)

    def has_reaction(self, comment_id: int, actor: str = "qykw", content: str = "laugh") -> bool:
        comment_id = _intern_positive(comment_id, "invalid_comment_id")
        if _intern_login(actor) != "qykw":
            raise InternError("invalid_reaction_actor")
        if content != "laugh":
            raise InternError("invalid_reaction_content")
        payloads = self._paginate(
            self._repo_path(f"issues/comments/{comment_id}/reactions?per_page=100")
        )
        found = False
        for value in payloads:
            _intern_positive(value.get("id"), "invalid_reaction")
            login = _intern_login(_intern_mapping(value.get("user"), "invalid_reaction").get("login"))
            reaction_content = _intern_string(value.get("content"), "invalid_reaction")
            if login == actor and reaction_content == content:
                found = True
        return found

    def add_reaction(self, comment_id: int) -> None:
        comment_id = _intern_positive(comment_id, "invalid_comment_id")
        self.assert_bot_identity()
        self._write_json("POST", self._repo_path(f"issues/comments/{comment_id}/reactions"), {"content": "laugh"})

    def add_assignee(self, issue_number: int, login: str) -> None:
        issue_number = _intern_positive(issue_number, "invalid_issue_number")
        login = _intern_login(login)
        self.assert_bot_identity()
        self._write_json("POST", self._repo_path(f"issues/{issue_number}/assignees"), {"assignees": [login]})

    def remove_assignee(self, issue_number: int, login: str) -> None:
        issue_number = _intern_positive(issue_number, "invalid_issue_number")
        login = _intern_login(login)
        self.assert_bot_identity()
        self._write_json("DELETE", self._repo_path(f"issues/{issue_number}/assignees"), {"assignees": [login]})

    def add_label(self, issue_number: int, label: str) -> None:
        label = _intern_label(label)
        number = _intern_positive(issue_number, "invalid_issue_number")
        self.assert_bot_identity()
        self._write_json("POST", self._repo_path(f"issues/{number}/labels"), {"labels": [label]})

    def remove_label(self, issue_number: int, label: str) -> None:
        label = _intern_label(label)
        number = _intern_positive(issue_number, "invalid_issue_number")
        self.assert_bot_identity()
        self._write_json("DELETE", self._repo_path(f"issues/{number}/labels/{quote(label, safe='')}"), None)

    def create_comment(self, issue_number: int, body: str) -> int:
        issue_number = _intern_positive(issue_number, "invalid_issue_number")
        body = _intern_write_body(body)
        self.assert_bot_identity()
        payload = _intern_mapping(self._write_json("POST", self._repo_path(f"issues/{issue_number}/comments"), {"body": body}), "invalid_comment_response")
        comment_id = _intern_positive(payload.get("id"), "invalid_comment_response")
        _intern_comment_locator(payload.get("issue_url"), self.api_url, self.repository, issue_number)
        return comment_id

    def update_comment(self, comment_id: int, body: str) -> None:
        comment_id = _intern_positive(comment_id, "invalid_comment_id")
        body = _intern_write_body(body)
        self.assert_bot_identity()
        self._write_json("PATCH", self._repo_path(f"issues/comments/{comment_id}"), {"body": body})

    def close_issue(self, issue_number: int) -> None:
        issue_number = _intern_positive(issue_number, "invalid_issue_number")
        self.assert_bot_identity()
        self._write_json("PATCH", self._repo_path(f"issues/{issue_number}"), {"state": "closed"})

    def _comments(self, number: int) -> tuple[IssueComment, ...]:
        payloads = self._paginate(self._repo_path(f"issues/{number}/comments?per_page=100"))
        comments: list[IssueComment] = []
        for value in payloads:
            _intern_comment_locator(value.get("issue_url"), self.api_url, self.repository, number)
            comments.append(InternIssueComment(
                _intern_positive(value.get("id"), "invalid_issue_comment"),
                _intern_login(_intern_mapping(value.get("user"), "invalid_issue_comment").get("login")),
                _intern_optional_string(value.get("body"), "invalid_issue_comment") or "",
                _intern_string(value.get("updated_at"), "invalid_issue_comment"),
                _intern_string(value.get("created_at"), "invalid_issue_comment"),
            ))
        return tuple(comments)

    def _paginate(self, url: str) -> tuple[Mapping[str, object], ...]:
        initial = self._validate_url(url)
        expected_path = urlsplit(initial).path
        current_page = _intern_collection_page(initial)
        current = initial
        visited: set[str] = set()
        values: list[Mapping[str, object]] = []
        for _ in range(_MAX_INTERN_PAGES):
            if current in visited:
                raise InternError("unsafe_pagination")
            visited.add(current)
            payload, headers = self._request("GET", current, body=None)
            if not isinstance(payload, list):
                raise InternError("invalid_pagination")
            values.extend(_intern_mapping(value, "invalid_pagination") for value in payload)
            next_url = _intern_next_link(headers.get("link"))
            if next_url is None:
                return tuple(values)
            try:
                candidate = self._validate_url(next_url)
            except InternError:
                raise InternError("unsafe_pagination") from None
            if urlsplit(candidate).path != expected_path:
                raise InternError("unsafe_pagination")
            candidate_page = _intern_collection_page(candidate)
            if candidate_page != current_page + 1:
                raise InternError("unsafe_pagination")
            current = candidate
            current_page = candidate_page
        raise InternError("unsafe_pagination")

    def _read_json(self, url: str) -> object:
        payload, _ = self._request("GET", url, body=None)
        return payload

    def _write_json(self, method: str, url: str, value: Mapping[str, object] | None) -> object:
        body = None if value is None else json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        payload, _ = self._request(method, url, body=body)
        return payload

    def _request(self, method: str, url: str, *, body: bytes | None) -> tuple[object, Mapping[str, str]]:
        safe_url = self._validate_url(url)
        if body is not None and len(body) > _MAX_INTERN_WRITE_BYTES:
            raise InternError("request_too_large")
        _intern_allowed_route(method, safe_url, body, self.api_url, self.repository)
        headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {self.token}", "X-GitHub-Api-Version": "2022-11-28"}
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        try:
            assert self.transport is not None
            status, response_headers, response_body = self.transport(method, safe_url, headers, body)
        except Exception:
            raise InternError("transport_failed") from None
        if type(status) is not int or not isinstance(response_body, bytes) or len(response_body) > _MAX_INTERN_RESPONSE_BYTES:
            raise InternError("invalid_response")
        if not isinstance(response_headers, Mapping):
            raise InternError("invalid_response")
        normalized = {str(key).lower(): str(value) for key, value in response_headers.items()}
        if status < 200 or status >= 300:
            raise InternError("github_http_error")
        content_type = normalized.get("content-type", "")
        if not content_type.lower().startswith("application/json"):
            raise InternError("invalid_response")
        return _intern_json(response_body), normalized

    def _repo_path(self, suffix: str) -> str:
        if not isinstance(suffix, str) or suffix.startswith("/") or "#" in suffix:
            raise InternError("invalid_request")
        return f"{self.api_url}/repos/{self.repository}/{suffix}"

    def _validate_url(self, url: str) -> str:
        parsed = urlsplit(url)
        origin = urlsplit(self.api_url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.hostname != origin.hostname or parsed.port != origin.port or parsed.fragment:
            raise InternError("unsafe_url")
        if parsed.path == f"{origin.path}/user" and not parsed.query:
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        prefix = f"{origin.path}/repos/{self.repository}/"
        if not parsed.path.startswith(prefix):
            raise InternError("unsafe_url")
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def parse_intern_command(body: str) -> InternCommand | None:
    """Parse a command occupying the first visible Markdown line."""

    if not isinstance(body, str):
        return None
    for line in _visible_lines(body, preserve_inline_code=True):
        stripped = line.strip()
        if not stripped:
            continue
        if "`" in stripped or stripped not in _COMMANDS:
            return None
        return _COMMANDS[stripped]
    return None


def normalize_issue_comment_event(payload: Mapping[str, object]) -> IssueCommentEvent | None:
    """Normalize only a newly-created comment on a normal Issue."""

    if not isinstance(payload, Mapping) or payload.get("action") != "created":
        return None
    repository = _mapping(payload.get("repository"))
    issue = _mapping(payload.get("issue"))
    comment = _mapping(payload.get("comment"))
    sender = _mapping(payload.get("sender"))
    if not repository or not issue or not comment or not sender:
        return None
    if "pull_request" in issue:
        return None
    name = repository.get("full_name")
    repository_id = _positive_id(repository.get("id"))
    issue_number = _positive_id(issue.get("number"))
    comment_id = _positive_id(comment.get("id"))
    actor = comment.get("user", {})
    actor_login = _login(actor)
    sender_login = _login(sender)
    created_at = comment.get("created_at")
    updated_at = comment.get("updated_at")
    if not isinstance(name, str) or not name or not actor_login or actor_login.casefold() != sender_login.casefold():
        return None
    if (not isinstance(created_at, str) or not created_at
            or not isinstance(updated_at, str) or created_at != updated_at):
        return None
    issue_url = issue.get("repository_url")
    issue_repo = _issue_repo_from_url(issue_url)
    if issue_repo != name:
        return None
    command = parse_intern_command(comment.get("body"))
    if None in (repository_id, issue_number, comment_id) or command is None:
        return None
    return IssueCommentEvent(name, repository_id, issue_number, comment_id, actor_login, command, created_at)


def normalize_pull_event(payload: Mapping[str, object]) -> PullLifecycleEvent | None:
    """Normalize an allowed pull-request lifecycle event on this repository."""

    if not isinstance(payload, Mapping) or payload.get("action") not in _PULL_ACTIONS:
        return None
    repository = _mapping(payload.get("repository"))
    pull = _mapping(payload.get("pull_request"))
    if not repository or not pull:
        return None
    name = repository.get("full_name")
    repository_id = _positive_id(repository.get("id"))
    pull_number = _positive_id(pull.get("number"))
    base = _mapping(pull.get("base"))
    base_repo = _mapping(base.get("repo")) if base else None
    if not isinstance(name, str) or not name or repository_id is None or pull_number is None:
        return None
    if not base_repo or base_repo.get("full_name") != name:
        return None
    return PullLifecycleEvent(name, repository_id, pull_number, payload["action"])


def parse_closing_issue(body: str) -> int | None:
    """Return the sole visible canonical ``Closes #N`` target, if present."""

    if not isinstance(body, str) or len(body.encode("utf-8")) > _MAX_CLOSING_BODY_BYTES:
        return None
    visible_markdown = "\n".join(_visible_lines(body, preserve_inline_code=False))
    without_html_code = _strip_html_code(visible_markdown)
    if without_html_code is None:
        return None
    text = without_html_code
    matches = list(_CLOSING.finditer(text))
    references = list(_ISSUE_REF.finditer(text))
    if len(matches) != 1 or len(references) != 1:
        return None
    match = matches[0]
    reference = references[0]
    if reference.start() != match.start() + match.group(0).index("#"):
        return None
    if (match.start() and text[match.start() - 1] == "[") or text[match.end():].startswith("]("):
        return None
    return int(match.group(1))


def resolve_pull_issue_number(event: PullLifecycleEvent, gateway: InternGateway) -> int | None:
    """Resolve the Issue queue key without performing a GitHub mutation."""

    if not isinstance(event, PullLifecycleEvent):
        raise InternError("invalid_pull_event")
    if getattr(gateway, "repository", None) != event.repository:
        raise InternError("event_repository_mismatch")
    pull = gateway.get_pull(event.pull_number)
    if pull.number != event.pull_number:
        raise InternError("pull_number_mismatch")

    bindings: list[InternRecord] = []
    for comment in gateway.list_pull_comments(event.pull_number):
        if comment.author_login != "qykw":
            continue
        record = decode_marker(comment.body, repository=event.repository)
        if (record is not None and record.operation == "pull"
                and record.repository_id == event.repository_id
                and record.pull_number == event.pull_number
                and record.trigger_comment_id == event.pull_number):
            bindings.append(record)
    if bindings:
        identity = _intern_record_identity(bindings[0])
        if any(_intern_record_identity(record) != identity for record in bindings[1:]):
            raise InternError("record_conflict")
        return _bounded_issue_number(bindings[0].issue_number)

    issue_number = parse_closing_issue(pull.body)
    return _bounded_issue_number(issue_number) if issue_number is not None else None


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _positive_id(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _login(value: object) -> str:
    mapping = _mapping(value)
    login = mapping.get("login") if mapping else None
    return login if isinstance(login, str) and login else ""


def _issue_repo_from_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    prefix = "/repos/"
    if parsed.scheme != "https" or parsed.netloc != "api.github.com" or not parsed.path.startswith(prefix):
        return None
    rest = parsed.path[len(prefix):].strip("/")
    pieces = rest.split("/")
    return "/".join(pieces[:2]) if len(pieces) == 2 and all(pieces) else None


def _unchanged_created_comment(comment: object, required_created_at: str = "") -> bool:
    """Accept legacy in-memory comments only for legacy timestamp-free tests."""

    created_at = getattr(comment, "created_at", None)
    updated_at = getattr(comment, "updated_at", None)
    if created_at is None:
        return not required_created_at and isinstance(updated_at, str)
    if (not isinstance(created_at, str) or not created_at
            or not isinstance(updated_at, str) or created_at != updated_at):
        return False
    return not required_created_at or created_at == required_created_at


def _visible_lines(body: str, *, preserve_inline_code: bool) -> list[str]:
    body = re.sub(r"<!--[\s\S]*?-->", "", body)
    visible: list[str] = []
    fence: tuple[str, int] | None = None
    in_blockquote = False
    for raw in body.splitlines():
        if not raw.strip():
            in_blockquote = False
            visible.append(raw)
            continue
        if re.match(r"^\s{4,}", raw) or raw.startswith("\t"):
            continue
        if re.match(r"^\s{0,3}>", raw):
            in_blockquote = True
            continue
        if in_blockquote:
            continue
        if fence:
            closing = _FENCE_END.match(raw)
            if closing and closing.group(1)[0] == fence[0] and len(closing.group(1)) >= fence[1]:
                fence = None
            continue
        opening = _FENCE.match(raw)
        if opening:
            marker = opening.group(1)
            fence = (marker[0], len(marker))
            continue
        visible.append(raw)
    if preserve_inline_code:
        return visible
    return _strip_inline_code("\n".join(visible)).split("\n")


_HTML_CODE_TAG = re.compile(r"(?is)<\s*(/?)\s*(code|pre|kbd|samp)\b[^>]{0,1024}>")
_HTML_CODE_PREFIX = re.compile(r"(?is)<\s*/?\s*(code|pre|kbd|samp)\b")


def _strip_html_code(body: str) -> str | None:
    """Remove bounded raw-HTML code elements; reject malformed code markup."""

    output: list[str] = []
    stack: list[str] = []
    cursor = 0
    for match in _HTML_CODE_TAG.finditer(body):
        between = body[cursor:match.start()]
        if not stack:
            output.append(between)
        else:
            output.append("\n" * between.count("\n"))
        closing = bool(match.group(1))
        tag = match.group(2).casefold()
        if closing:
            if not stack or stack[-1] != tag:
                return None
            stack.pop()
        else:
            stack.append(tag)
        output.append("\n" * match.group(0).count("\n"))
        cursor = match.end()
    tail = body[cursor:]
    if stack:
        return None
    output.append(tail)
    result = "".join(output)
    if _HTML_CODE_PREFIX.search(result):
        return None
    return result


def _strip_inline_code(line: str) -> str:
    """Remove CommonMark-style backtick spans in one linear bounded pass."""

    output: list[str] = []
    cursor = 0
    length = len(line)
    while cursor < length:
        opening = line.find("`", cursor)
        if opening < 0:
            output.append(line[cursor:])
            break
        output.append(line[cursor:opening])
        opening_end = opening
        while opening_end < length and line[opening_end] == "`":
            opening_end += 1
        width = opening_end - opening
        scan = opening_end
        closing_end = -1
        while scan < length:
            candidate = line.find("`", scan)
            if candidate < 0:
                break
            candidate_end = candidate
            while candidate_end < length and line[candidate_end] == "`":
                candidate_end += 1
            if candidate_end - candidate == width:
                closing_end = candidate_end
                break
            scan = candidate_end
        if closing_end < 0:
            break
        cursor = closing_end
    return "".join(output)


def encode_marker(record: InternRecord) -> str:
    """Encode the only marker accepted by the intern controller."""

    if not isinstance(record, InternRecord):
        raise InternError("invalid_record")
    payload = {
        "actor_login": record.actor_login,
        "claimant_login": record.claimant_login,
        "issue_number": record.issue_number,
        "operation": record.operation,
        "pull_number": record.pull_number,
        "repository": record.repository,
        "repository_id": record.repository_id,
        "stage": record.stage,
        "trigger_comment_id": record.trigger_comment_id,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "<!-- qykw-intern:v1 " + encoded.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026") + " -->"


def decode_marker(body: str, *, repository: str | None = None) -> InternRecord | None:
    """Decode one exact v1 marker, optionally bound to the current repository."""

    if not isinstance(body, str) or len(body.encode("utf-8")) > 16 * 1024:
        return None
    matches = list(_INTERN_MARKER.finditer(body))
    if len(matches) != 1:
        return None
    payload = _intern_json_or_none(matches[0].group(1))
    expected = {
        "actor_login", "claimant_login", "issue_number", "operation", "pull_number",
        "repository", "repository_id", "stage", "trigger_comment_id",
    }
    if not isinstance(payload, Mapping) or set(payload) != expected:
        return None
    try:
        record = InternRecord(
            payload["repository_id"], payload["repository"], payload["issue_number"],
            payload["trigger_comment_id"], payload["actor_login"], payload["operation"],
            payload["claimant_login"], payload["pull_number"], payload["stage"],
        )
    except (InternError, KeyError, TypeError):
        return None
    if repository is not None:
        try:
            _intern_repository(repository)
        except InternError:
            return None
        if record.repository != repository:
            return None
    return record


def reduce_records(records: tuple[IssueComment, ...], *, repository_id: int, repository: str,
                   issue_number: int) -> tuple[InternRecord, ...]:
    """Reduce trusted, repository-bound records to one state per operation key."""

    repository_id = _intern_positive(repository_id, "invalid_repository_id")
    repository = _intern_repository(repository)
    issue_number = _intern_positive(issue_number, "invalid_issue_number")
    grouped: dict[tuple[int, int, int, str], list[InternRecord]] = {}
    for comment in sorted(records, key=lambda item: item.comment_id):
        if not isinstance(comment, IssueComment) or comment.author_login != "qykw":
            continue
        record = decode_marker(comment.body, repository=repository)
        if record is None or (record.repository_id, record.issue_number) != (repository_id, issue_number):
            continue
        grouped.setdefault(record.operation_key, []).append(record)
    reduced: list[InternRecord] = []
    for same_operation in grouped.values():
        first = same_operation[0]
        if any(_intern_record_identity(record) != _intern_record_identity(first) for record in same_operation[1:]):
            raise InternError("record_conflict")
        terminal = [record for record in same_operation if record.stage != "pending"]
        if len(terminal) > 1:
            raise InternError("record_conflict")
        reduced.append(terminal[0] if terminal else first)
    return tuple(reduced)


class InternClaimService:
    """Reconcile trusted Issue commands one externally visible write at a time."""

    _TERMINAL_STAGES = frozenset({"reconciled", "conflict"})
    _RECONCILIATION_STEPS = 7
    _TRANSIENT_ATTEMPTS = 2

    def __init__(self, gateway: InternGateway) -> None:
        self.gateway = gateway

    def handle_issue_event(self, event: IssueCommentEvent) -> InternOutcome:
        if not isinstance(event, IssueCommentEvent):
            raise InternError("invalid_issue_event")
        self.gateway.assert_bot_identity()
        comments = self.gateway.list_issue_comments(event.issue_number)
        triggers = [comment for comment in comments if comment.comment_id == event.comment_id]
        if len(triggers) != 1:
            return InternOutcome(event.issue_number, (), "noop")
        trigger = triggers[0]
        if (trigger.author_login.casefold() != event.actor_login.casefold()
                or not _unchanged_created_comment(trigger, event.created_at)
                or parse_intern_command(trigger.body) is not event.command):
            return InternOutcome(event.issue_number, (), "noop")
        try:
            records = reduce_records(
                comments,
                repository_id=event.repository_id,
                repository=event.repository,
                issue_number=event.issue_number,
            )
        except InternError:
            return InternOutcome(event.issue_number, (), "conflict")

        record_by_key = {record.operation_key: record for record in records}
        commands: list[tuple[IssueComment, InternCommand]] = []
        for comment in comments:
            if not _unchanged_created_comment(comment):
                continue
            command = parse_intern_command(comment.body)
            if command is not None:
                commands.append((comment, command))
        commands.sort(key=lambda item: item[0].comment_id)

        processed: list[int] = []
        statuses: list[str] = []
        for comment, command in commands:
            operation = self._operation(command)
            key = (event.repository_id, event.issue_number, comment.comment_id, operation)
            existing = record_by_key.get(key)
            if existing is not None and existing.stage in self._TERMINAL_STAGES:
                continue
            status = self._handle_command(event, comment, command, existing)
            processed.append(comment.comment_id)
            statuses.append(status)
            if status == "failed":
                break
            refreshed = self.gateway.list_issue_comments(event.issue_number)
            try:
                current = reduce_records(
                    refreshed,
                    repository_id=event.repository_id,
                    repository=event.repository,
                    issue_number=event.issue_number,
                )
            except InternError:
                return InternOutcome(event.issue_number, tuple(processed), "conflict")
            record_by_key = {record.operation_key: record for record in current}

        if "failed" in statuses:
            status = "failed"
        elif "conflict" in statuses:
            status = "conflict"
        elif statuses:
            status = "reconciled"
        else:
            status = "noop"
        return InternOutcome(event.issue_number, tuple(processed), status)

    def handle_pull_event(self, event: PullLifecycleEvent, *,
                          expected_issue_number: int | None = None) -> InternOutcome:
        """Reconcile one PR lifecycle event against its frozen Issue binding."""

        if not isinstance(event, PullLifecycleEvent) or event.action not in _PULL_ACTIONS:
            raise InternError("invalid_pull_event")
        if expected_issue_number is not None:
            expected_issue_number = _bounded_issue_number(expected_issue_number)
        gateway_repository = getattr(self.gateway, "repository", None)
        if gateway_repository != event.repository:
            return InternOutcome(0, (), "conflict")
        try:
            self.gateway.assert_bot_identity()
            binding = self._pull_binding(event)
        except InternError as error:
            status = "conflict" if error.code == "record_conflict" else "failed"
            return InternOutcome(0, (), status)

        if binding is None:
            if event.action in {"closed", "reopened"}:
                return InternOutcome(0, (), "conflict")
            return self._start_pull_binding(event, expected_issue_number)

        record, pull_comment_id = binding
        if expected_issue_number is not None and record.issue_number != expected_issue_number:
            return InternOutcome(expected_issue_number, (), "conflict")
        try:
            pull = self.gateway.get_pull(event.pull_number)
        except InternError as error:
            status = "conflict" if error.code == "pull_repository_mismatch" else "failed"
            return InternOutcome(record.issue_number, (), status)
        if (pull.number != event.pull_number
                or pull.author_login.casefold() != record.claimant_login.casefold()):
            return InternOutcome(record.issue_number, (), "conflict")
        if event.action == "closed":
            return self._reconcile_closed_pull(event, pull, record, pull_comment_id)
        return self._reconcile_active_pull(event, pull, record, pull_comment_id)

    def _start_pull_binding(self, event: PullLifecycleEvent,
                            expected_issue_number: int | None = None) -> InternOutcome:
        try:
            pull = self.gateway.get_pull(event.pull_number)
        except InternError as error:
            status = "conflict" if error.code == "pull_repository_mismatch" else "failed"
            return InternOutcome(0, (), status)
        issue_number = parse_closing_issue(pull.body)
        if expected_issue_number is not None and issue_number != expected_issue_number:
            return InternOutcome(expected_issue_number, (), "conflict")
        if issue_number is None or pull.state != "open":
            return InternOutcome(0, (), "conflict")
        try:
            issue = self.gateway.get_issue(issue_number)
            comments = self.gateway.list_issue_comments(issue_number)
            records = reduce_records(
                comments, repository_id=event.repository_id,
                repository=event.repository, issue_number=issue_number,
            )
        except InternError:
            return InternOutcome(issue_number, (), "failed")
        labels = set(issue.labels)
        if (issue.is_pull or issue.state != "open" or len(issue.assignees) != 1
                or issue.assignees[0].casefold() != pull.author_login.casefold()
                or "status:in-progress" not in labels
                or labels & {"intern:claimable", "status:in-review", "status:blocked"}
                or any(item.operation == "pull" and item.pull_number is not None for item in records)):
            return InternOutcome(issue_number, (), "conflict")

        record = InternRecord(
            event.repository_id, event.repository, issue_number, event.pull_number,
            pull.author_login, "pull", pull.author_login, event.pull_number, "pending",
        )
        pull_comment_id = self._ensure_pull_marker(event, record)
        if pull_comment_id is None:
            return InternOutcome(issue_number, (), "failed")
        return self._reconcile_active_pull(event, pull, record, pull_comment_id)

    def _reconcile_active_pull(self, event: PullLifecycleEvent, pull: PullSnapshot,
                               record: InternRecord, pull_comment_id: int) -> InternOutcome:
        if pull.state != "open":
            return InternOutcome(record.issue_number, (), "conflict")
        try:
            issue = self.gateway.get_issue(record.issue_number)
            comments = self.gateway.list_issue_comments(record.issue_number)
            issue_binding = self._issue_binding(event, record, comments)
        except InternError:
            self._mark_pull_failed(event, record, pull_comment_id, None)
            return InternOutcome(record.issue_number, (), "failed")
        labels = set(issue.labels)
        if (issue.is_pull or issue.state != "open" or len(issue.assignees) != 1
                or issue.assignees[0].casefold() != record.claimant_login.casefold()
                or labels & {"intern:claimable", "status:blocked"}
                or {"status:in-progress", "status:in-review"} <= labels):
            return InternOutcome(record.issue_number, (), "conflict")

        issue_comment_id = issue_binding
        if issue_comment_id is None:
            if not labels & {"status:in-progress", "status:in-review"}:
                return InternOutcome(record.issue_number, (), "conflict")
            issue_comment_id = self._ensure_issue_marker(event, record)
            if issue_comment_id is None:
                self._mark_pull_failed(event, record, pull_comment_id, None)
                return InternOutcome(record.issue_number, (), "failed")
            try:
                issue = self.gateway.get_issue(record.issue_number)
            except InternError:
                self._mark_pull_failed(event, record, pull_comment_id, issue_comment_id)
                return InternOutcome(record.issue_number, (), "failed")
            labels = set(issue.labels)

        if ("status:in-review" in labels and "status:in-progress" not in labels
                and record.stage == "reconciled"
                and self._comment_stage(event, issue_comment_id, record, pull=False) == "reconciled"):
            return InternOutcome(record.issue_number, (), "noop")

        if "status:in-progress" in labels:
            if not self._mutate_pull_issue(
                    event, record, pull_comment_id, issue_comment_id,
                    lambda: self.gateway.remove_label(record.issue_number, "status:in-progress"),
                    lambda snapshot: "status:in-progress" not in snapshot.labels):
                return InternOutcome(record.issue_number, (), "failed")
            try:
                issue = self.gateway.get_issue(record.issue_number)
            except InternError:
                self._mark_pull_failed(event, record, pull_comment_id, issue_comment_id)
                return InternOutcome(record.issue_number, (), "failed")
            labels = set(issue.labels)
        if "status:in-review" not in labels:
            if not self._mutate_pull_issue(
                    event, record, pull_comment_id, issue_comment_id,
                    lambda: self.gateway.add_label(record.issue_number, "status:in-review"),
                    lambda snapshot: "status:in-review" in snapshot.labels):
                return InternOutcome(record.issue_number, (), "failed")
        terminal = self._with_stage(record, "reconciled")
        if not self._set_marker_stage(event, issue_comment_id, terminal, pull=False):
            self._mark_pull_failed(event, record, pull_comment_id, issue_comment_id)
            return InternOutcome(record.issue_number, (), "failed")
        if not self._set_marker_stage(event, pull_comment_id, terminal, pull=True):
            self._mark_pull_failed(event, record, pull_comment_id, issue_comment_id)
            return InternOutcome(record.issue_number, (), "failed")
        return InternOutcome(record.issue_number, (event.pull_number,), "reconciled")

    def _reconcile_closed_pull(self, event: PullLifecycleEvent, pull: PullSnapshot,
                               record: InternRecord, pull_comment_id: int) -> InternOutcome:
        try:
            issue = self.gateway.get_issue(record.issue_number)
            comments = self.gateway.list_issue_comments(record.issue_number)
            issue_comment_id = self._issue_binding(event, record, comments)
        except InternError:
            self._mark_pull_failed(event, record, pull_comment_id, None)
            return InternOutcome(record.issue_number, (), "failed")
        if (issue.is_pull or len(issue.assignees) != 1
                or issue.assignees[0].casefold() != record.claimant_login.casefold()):
            return InternOutcome(record.issue_number, (), "conflict")
        labels = set(issue.labels)

        if issue_comment_id is None and (pull.merged or record.stage != "reconciled"):
            if (issue.state != "open"
                    or not labels & {"status:in-progress", "status:in-review"}
                    or labels & {"intern:claimable", "status:blocked"}
                    or {"status:in-progress", "status:in-review"} <= labels):
                return InternOutcome(record.issue_number, (), "conflict")
            issue_comment_id = self._ensure_issue_marker(event, record)
            if issue_comment_id is None:
                self._mark_pull_failed(event, record, pull_comment_id, None)
                return InternOutcome(record.issue_number, (), "failed")

        if pull.merged:
            progress_labels = labels & {"status:in-progress", "status:in-review"}
            if issue.state == "closed":
                if (issue_comment_id is None
                        or labels & {"intern:claimable", "status:blocked"}
                        or len(progress_labels) > 1):
                    return InternOutcome(record.issue_number, (), "conflict")
                for progress_label in progress_labels:
                    if not self._mutate_pull_issue(
                            event, record, pull_comment_id, issue_comment_id,
                            lambda label=progress_label: self.gateway.remove_label(
                                record.issue_number, label,
                            ),
                            lambda snapshot, label=progress_label: label not in snapshot.labels):
                        return InternOutcome(record.issue_number, (), "failed")
                terminal = self._with_stage(record, "reconciled")
                issue_stage = self._comment_stage(
                    event, issue_comment_id, record, pull=False,
                )
                if (not progress_labels and record.stage == "reconciled"
                        and issue_stage == "reconciled"):
                    return InternOutcome(record.issue_number, (), "noop")
                if not self._set_marker_stage(event, issue_comment_id, terminal, pull=False):
                    self._mark_pull_failed(event, record, pull_comment_id, issue_comment_id)
                    return InternOutcome(record.issue_number, (), "failed")
                if not self._set_marker_stage(event, pull_comment_id, terminal, pull=True):
                    self._mark_pull_failed(event, record, pull_comment_id, issue_comment_id)
                    return InternOutcome(record.issue_number, (), "failed")
                return InternOutcome(record.issue_number, (event.pull_number,), "reconciled")
            if issue.state != "open" or labels & {"intern:claimable", "status:blocked"}:
                return InternOutcome(record.issue_number, (), "conflict")
            if "status:in-progress" in labels:
                if not self._mutate_pull_issue(
                        event, record, pull_comment_id, issue_comment_id,
                        lambda: self.gateway.remove_label(record.issue_number, "status:in-progress"),
                        lambda snapshot: "status:in-progress" not in snapshot.labels):
                    return InternOutcome(record.issue_number, (), "failed")
                if not self._mutate_pull_issue(
                        event, record, pull_comment_id, issue_comment_id,
                        lambda: self.gateway.add_label(record.issue_number, "status:in-review"),
                        lambda snapshot: "status:in-review" in snapshot.labels):
                    return InternOutcome(record.issue_number, (), "failed")
                labels.discard("status:in-progress")
                labels.add("status:in-review")
            if "status:in-review" in labels:
                if not self._mutate_pull_issue(
                        event, record, pull_comment_id, issue_comment_id,
                        lambda: self.gateway.remove_label(record.issue_number, "status:in-review"),
                        lambda snapshot: "status:in-review" not in snapshot.labels):
                    return InternOutcome(record.issue_number, (), "failed")
            if not self._mutate_pull_issue(
                    event, record, pull_comment_id, issue_comment_id,
                    lambda: self.gateway.close_issue(record.issue_number),
                    lambda snapshot: snapshot.state == "closed"):
                return InternOutcome(record.issue_number, (), "failed")
            terminal = self._with_stage(record, "reconciled")
            if issue_comment_id is None or not self._set_marker_stage(
                    event, issue_comment_id, terminal, pull=False):
                self._mark_pull_failed(event, record, pull_comment_id, issue_comment_id)
                return InternOutcome(record.issue_number, (), "failed")
            if not self._set_marker_stage(event, pull_comment_id, terminal, pull=True):
                self._mark_pull_failed(event, record, pull_comment_id, issue_comment_id)
                return InternOutcome(record.issue_number, (), "failed")
            return InternOutcome(record.issue_number, (event.pull_number,), "reconciled")

        if issue.state != "open" or labels & {"intern:claimable", "status:blocked"}:
            return InternOutcome(record.issue_number, (), "conflict")
        if (issue_comment_id is None and "status:in-progress" in labels
                and "status:in-review" not in labels):
            return InternOutcome(record.issue_number, (), "noop")
        if "status:in-review" in labels:
            if not self._mutate_pull_issue(
                    event, record, pull_comment_id, issue_comment_id,
                    lambda: self.gateway.remove_label(record.issue_number, "status:in-review"),
                    lambda snapshot: "status:in-review" not in snapshot.labels):
                return InternOutcome(record.issue_number, (), "failed")
            try:
                issue = self.gateway.get_issue(record.issue_number)
            except InternError:
                self._mark_pull_failed(event, record, pull_comment_id, issue_comment_id)
                return InternOutcome(record.issue_number, (), "failed")
            labels = set(issue.labels)
        if "status:in-progress" not in labels:
            if not self._mutate_pull_issue(
                    event, record, pull_comment_id, issue_comment_id,
                    lambda: self.gateway.add_label(record.issue_number, "status:in-progress"),
                    lambda snapshot: "status:in-progress" in snapshot.labels):
                return InternOutcome(record.issue_number, (), "failed")
        if issue_comment_id is not None and not self._clear_issue_marker(event, record, issue_comment_id):
            self._mark_pull_failed(event, record, pull_comment_id, issue_comment_id)
            return InternOutcome(record.issue_number, (), "failed")
        terminal = self._with_stage(record, "reconciled")
        if not self._set_marker_stage(event, pull_comment_id, terminal, pull=True):
            return InternOutcome(record.issue_number, (), "failed")
        return InternOutcome(record.issue_number, (event.pull_number,), "reconciled")

    def _pull_binding(self, event: PullLifecycleEvent) -> tuple[InternRecord, int] | None:
        matches: list[tuple[InternRecord, int]] = []
        for comment in self.gateway.list_pull_comments(event.pull_number):
            if comment.author_login != "qykw":
                continue
            record = decode_marker(comment.body, repository=event.repository)
            if record is None or record.operation != "pull":
                continue
            if (record.repository_id != event.repository_id
                    or record.pull_number != event.pull_number
                    or record.trigger_comment_id != event.pull_number
                    or record.claimant_login is None
                    or record.actor_login.casefold() != record.claimant_login.casefold()):
                raise InternError("record_conflict")
            matches.append((record, comment.comment_id))
        if not matches:
            return None
        first = matches[0][0]
        if len(matches) != 1 or any(_intern_record_identity(item[0]) != _intern_record_identity(first)
                                   for item in matches[1:]):
            raise InternError("record_conflict")
        return matches[0]

    def _issue_binding(self, event: PullLifecycleEvent, record: InternRecord,
                       comments: tuple[IssueComment, ...]) -> int | None:
        matches: list[int] = []
        for comment in comments:
            if comment.author_login != "qykw":
                continue
            candidate = decode_marker(comment.body, repository=event.repository)
            if candidate is None or candidate.operation != "pull":
                continue
            if _intern_record_identity(candidate) != _intern_record_identity(record):
                raise InternError("record_conflict")
            matches.append(comment.comment_id)
        if len(matches) > 1:
            raise InternError("record_conflict")
        return matches[0] if matches else None

    def _ensure_pull_marker(self, event: PullLifecycleEvent, record: InternRecord) -> int | None:
        body = self._body("正在固定该 PR 的 Issue 关联。", record)
        try:
            self.gateway.create_comment(event.pull_number, body)
        except InternError:
            pass
        try:
            binding = self._pull_binding(event)
        except InternError:
            return None
        return binding[1] if binding is not None and _intern_record_identity(binding[0]) == _intern_record_identity(record) else None

    def _ensure_issue_marker(self, event: PullLifecycleEvent, record: InternRecord) -> int | None:
        try:
            self.gateway.create_comment(record.issue_number, self._body("正在关联该 Issue 与 PR。", record))
        except InternError:
            pass
        try:
            comments = self.gateway.list_issue_comments(record.issue_number)
            return self._issue_binding(event, record, comments)
        except InternError:
            return None

    def _mutate_pull_issue(self, event: PullLifecycleEvent, record: InternRecord,
                           pull_comment_id: int, issue_comment_id: int | None,
                           mutation: Callable[[], None],
                           verify: Callable[[IssueSnapshot], bool]) -> bool:
        try:
            mutation()
        except InternError:
            pass
        try:
            issue = self.gateway.get_issue(record.issue_number)
            comments = self.gateway.list_issue_comments(record.issue_number)
            current_comment_id = self._issue_binding(event, record, comments)
        except InternError:
            self._mark_pull_failed(event, record, pull_comment_id, issue_comment_id)
            return False
        if issue_comment_id is not None and current_comment_id != issue_comment_id:
            self._mark_pull_failed(event, record, pull_comment_id, issue_comment_id)
            return False
        if verify(issue):
            return True
        self._mark_pull_failed(event, record, pull_comment_id, issue_comment_id)
        return False

    def _set_marker_stage(self, event: PullLifecycleEvent, comment_id: int,
                          record: InternRecord, *, pull: bool) -> bool:
        current = self._comment_stage(event, comment_id, record, pull=pull)
        if current == record.stage:
            return True
        message = ("处理暂时失败，等待安全重放。" if record.stage == "failed"
                   else "Issue 与 PR 状态已同步。")
        try:
            self.gateway.update_comment(comment_id, self._body(message, record))
        except InternError:
            pass
        return self._comment_stage(event, comment_id, record, pull=pull) == record.stage

    def _comment_stage(self, event: PullLifecycleEvent, comment_id: int,
                       record: InternRecord, *, pull: bool) -> str | None:
        try:
            comments = (self.gateway.list_pull_comments(event.pull_number) if pull
                        else self.gateway.list_issue_comments(record.issue_number))
        except InternError:
            return None
        for comment in comments:
            candidate = decode_marker(comment.body, repository=event.repository)
            if (comment.comment_id == comment_id and candidate is not None
                    and _intern_record_identity(candidate) == _intern_record_identity(record)):
                return candidate.stage
        return None

    def _clear_issue_marker(self, event: PullLifecycleEvent, record: InternRecord,
                            issue_comment_id: int) -> bool:
        try:
            self.gateway.update_comment(
                issue_comment_id,
                f"PR #{event.pull_number} 未合并关闭；Issue 已恢复处理中。",
            )
        except InternError:
            pass
        try:
            comments = self.gateway.list_issue_comments(record.issue_number)
            return self._issue_binding(event, record, comments) is None
        except InternError:
            return False

    def _mark_pull_failed(self, event: PullLifecycleEvent, record: InternRecord,
                          pull_comment_id: int, issue_comment_id: int | None) -> None:
        failed = self._with_stage(record, "failed")
        if issue_comment_id is not None:
            self._set_marker_stage(event, issue_comment_id, failed, pull=False)
        self._set_marker_stage(event, pull_comment_id, failed, pull=True)

    def _handle_command(self, event: IssueCommentEvent, comment: IssueComment,
                        command: InternCommand, existing: InternRecord | None) -> str:
        operation = self._operation(command)
        record = existing or InternRecord(
            repository_id=event.repository_id,
            repository=event.repository,
            issue_number=event.issue_number,
            trigger_comment_id=comment.comment_id,
            actor_login=comment.author_login,
            operation=operation,
            claimant_login=("qykw" if command is InternCommand.UNASSIGN else
                            comment.author_login if command is InternCommand.ASSIGN else None),
            pull_number=None,
            stage="pending",
        )
        marker_comment_id = self._marker_comment_id(event, record.operation_key)

        if existing is None:
            if not self._ensure_reaction(comment.comment_id):
                return "failed"
            if command is InternCommand.UNASSIGN:
                try:
                    issue_before_marker = self.gateway.get_issue(event.issue_number)
                except InternError:
                    return "failed"
                frozen_claimant = (issue_before_marker.assignees[0]
                                   if len(issue_before_marker.assignees) == 1 else "qykw")
                record = InternRecord(
                    record.repository_id, record.repository, record.issue_number,
                    record.trigger_comment_id, record.actor_login, record.operation,
                    frozen_claimant, record.pull_number, record.stage,
                )
            marker_comment_id = self._ensure_command_marker(event, record)
            if marker_comment_id is None:
                return "failed"
        elif existing.stage == "failed":
            record = self._with_stage(existing, "pending")
            if marker_comment_id is None:
                return "conflict"
            if not self._update_command_marker(
                    marker_comment_id, record, "正在重试该命令。"):
                return "failed"

        if marker_comment_id is None:
            return "conflict"
        try:
            issue, records = self._read_state(event)
        except InternError:
            self._mark_failed(marker_comment_id, record)
            return "failed"
        current = next((item for item in records if item.operation_key == record.operation_key), None)
        if current is None:
            return "conflict"
        record = current

        if command is InternCommand.ASSIGN:
            return self._reconcile_assign(
                event, issue, record, marker_comment_id,
                allow_existing_actor_assignee=existing is not None,
            )
        if command is InternCommand.UNASSIGN:
            return self._reconcile_release(event, issue, records, record, marker_comment_id)
        return self._publish_status(event, issue, record, marker_comment_id)

    def _reconcile_assign(self, event: IssueCommentEvent, issue: IssueSnapshot,
                          record: InternRecord, marker_comment_id: int, *,
                          allow_existing_actor_assignee: bool) -> str:
        for _ in range(self._RECONCILIATION_STEPS):
            labels = set(issue.labels)
            assignees = issue.assignees
            if issue.state != "open":
                return self._finish(marker_comment_id, record, "Issue 已关闭，无法领取。", "reconciled")
            if "status:blocked" in labels:
                return self._finish(marker_comment_id, record, "Issue 已阻塞，暂不可领取。", "reconciled")
            progress_labels = labels & {"status:in-progress", "status:in-review"}
            if ("intern:claimable" in labels and progress_labels) or len(progress_labels) > 1:
                return self._finish(marker_comment_id, record, "Issue 的可领取与进度标签冲突，已停止写入。", "conflict")
            if len(assignees) == 1 and assignees[0].casefold() != record.actor_login.casefold():
                if "intern:claimable" not in labels and len(labels & {"status:in-progress", "status:in-review"}) == 1:
                    return self._finish(
                        marker_comment_id, record,
                        f"该任务已由 @{assignees[0]} 领取，请选择其他 Issue。", "reconciled",
                    )
                return self._finish(marker_comment_id, record, "Issue 的 Assignee 与领取状态冲突，已停止写入。", "conflict")
            if len(assignees) > 1:
                return self._finish(marker_comment_id, record, "Issue 存在多个 Assignee 冲突，已停止写入。", "conflict")
            if (len(assignees) == 1
                    and assignees[0].casefold() == record.actor_login.casefold()
                    and not allow_existing_actor_assignee):
                return self._finish(
                    marker_comment_id, record,
                    "Issue 已存在未经 qykw 领取流程确认的人工 Assignee，已停止写入。", "conflict",
                )
            if "status:in-review" in labels:
                if len(assignees) == 1:
                    return self._finish(
                        marker_comment_id, record,
                        f"该任务已由 @{assignees[0]} 领取，请选择其他 Issue。", "reconciled",
                    )
                return self._finish(marker_comment_id, record, "Issue 审查标签与 Assignee 冲突，已停止写入。", "conflict")
            if (assignees == (record.actor_login,) and "intern:claimable" not in labels
                    and "status:in-progress" in labels):
                return self._finish(
                    marker_comment_id, record, f"@{record.actor_login} 已成功领取该 Issue。", "reconciled"
                )
            if (not assignees and "intern:claimable" not in labels
                    and not (allow_existing_actor_assignee and not progress_labels)):
                return self._finish(marker_comment_id, record, "Issue 当前不可领取。", "reconciled")
            if assignees and assignees != (record.actor_login,):
                return self._finish(marker_comment_id, record, "Issue 的 Assignee 与领取状态冲突，已停止写入。", "conflict")

            adding_assignee = not assignees
            if (not assignees and "intern:claimable" not in labels
                    and allow_existing_actor_assignee and not progress_labels):
                mutation = lambda: self.gateway.add_label(
                    event.issue_number, "intern:claimable"
                )
                adding_assignee = False
            elif adding_assignee:
                mutation = lambda: self.gateway.add_assignee(event.issue_number, record.actor_login)
            elif "intern:claimable" in labels:
                mutation = lambda: self.gateway.remove_label(event.issue_number, "intern:claimable")
            elif "status:in-progress" not in labels:
                mutation = lambda: self.gateway.add_label(event.issue_number, "status:in-progress")
            else:
                return self._finish(marker_comment_id, record, "Issue 领取状态冲突，已停止写入。", "conflict")
            failed, issue = self._mutate_and_read(event, marker_comment_id, record, mutation)
            if failed:
                return "failed"
            if adding_assignee:
                allow_existing_actor_assignee = True
        return self._compensate_assign(event, issue, record, marker_comment_id)

    def _reconcile_release(self, event: IssueCommentEvent, issue: IssueSnapshot,
                           records: tuple[InternRecord, ...], record: InternRecord,
                           marker_comment_id: int) -> str:
        for _ in range(self._TRANSIENT_ATTEMPTS + 1):
            labels = set(issue.labels)
            if issue.state != "open":
                return self._finish(marker_comment_id, record, "Issue 已关闭，无法释放。", "reconciled")
            if "status:blocked" in labels:
                return self._finish(marker_comment_id, record, "Issue 已阻塞，已停止释放。", "reconciled")
            active_pull = any(item.operation == "pull" and item.pull_number is not None for item in records)
            if "status:in-review" in labels or active_pull:
                return self._finish(
                    marker_comment_id, record,
                    "Issue 存在活动 PR 或正在审查，不允许释放。", "reconciled",
                )
            if len(issue.assignees) > 1:
                return self._finish(marker_comment_id, record, "Issue 存在多个 Assignee 冲突，已停止写入。", "conflict")
            if not issue.assignees:
                if "intern:claimable" in labels and not labels & {"status:in-progress", "status:in-review"}:
                    return self._finish(marker_comment_id, record, "Issue 已处于可领取状态。", "reconciled")
                if record.claimant_login and record.claimant_login != "qykw":
                    return self._continue_partial_release(event, issue, record, marker_comment_id)
                return self._finish(marker_comment_id, record, "Issue 的释放状态冲突，已停止写入。", "conflict")

            claimant = issue.assignees[0]
            if record.claimant_login.casefold() != claimant.casefold():
                return self._finish(
                    marker_comment_id, record,
                    "Issue 的实时 Assignee 与已固定领取人冲突，已停止写入。", "conflict",
                )
            authorized = (record.actor_login.casefold() == claimant.casefold()
                          or record.actor_login == "xyh202131")
            if not authorized:
                return self._finish(marker_comment_id, record, f"@{record.actor_login} 无权释放 @{claimant} 领取的 Issue。", "reconciled")
            if "intern:claimable" in labels or "status:in-progress" not in labels:
                return self._finish(marker_comment_id, record, "Issue 的 Assignee 与进度标签冲突，已停止写入。", "conflict")
            failed, issue = self._mutate_and_read(
                event, marker_comment_id, record,
                lambda: self.gateway.remove_assignee(event.issue_number, claimant),
            )
            if failed:
                return "failed"
            try:
                issue, records = self._read_state(event)
            except InternError:
                self._mark_failed(marker_comment_id, record)
                return "failed"
            if not issue.assignees:
                return self._continue_partial_release(
                    event, issue, record, marker_comment_id
                )
        return self._compensate_release(event, issue, record, marker_comment_id)

    def _continue_partial_release(self, event: IssueCommentEvent, issue: IssueSnapshot,
                                  record: InternRecord, marker_comment_id: int) -> str:
        for _ in range(self._RECONCILIATION_STEPS):
            try:
                issue, records = self._read_state(event)
            except InternError:
                self._mark_failed(marker_comment_id, record)
                return "failed"
            labels = set(issue.labels)
            if issue.assignees:
                return self._finish(marker_comment_id, record, "Issue 的 Assignee 在释放时发生冲突。", "conflict")
            active_pull = any(item.operation == "pull" and item.pull_number is not None for item in records)
            if "status:in-review" in labels or active_pull:
                claimant = record.claimant_login
                if claimant is not None and claimant != "qykw":
                    restored = False
                    for _ in range(self._TRANSIENT_ATTEMPTS):
                        failed, issue = self._mutate_and_read(
                            event, marker_comment_id, record,
                            lambda: self.gateway.add_assignee(
                                event.issue_number, claimant,
                            ),
                        )
                        if failed:
                            return "failed"
                        if issue.assignees == (claimant,):
                            restored = True
                            break
                        if issue.assignees:
                            return self._finish(
                                marker_comment_id, record,
                                "Issue 的 Assignee 在释放时发生冲突。", "conflict",
                            )
                    if not restored:
                        self._mark_failed(marker_comment_id, record)
                        return "failed"
                return self._finish(
                    marker_comment_id, record,
                    "Issue 存在活动 PR 或正在审查，不允许释放。", "reconciled",
                )
            if "status:in-progress" in labels:
                mutation = lambda: self.gateway.remove_label(event.issue_number, "status:in-progress")
            elif "intern:claimable" not in labels:
                mutation = lambda: self.gateway.add_label(event.issue_number, "intern:claimable")
            else:
                return self._finish(marker_comment_id, record, "Issue 已成功释放。", "reconciled")
            failed, issue = self._mutate_and_read(event, marker_comment_id, record, mutation)
            if failed:
                return "failed"
        return self._compensate_release(event, issue, record, marker_comment_id)

    def _compensate_assign(self, event: IssueCommentEvent, issue: IssueSnapshot,
                           record: InternRecord, marker_comment_id: int) -> str:
        """Roll a partial claim back without replacing unrelated labels."""
        for _ in range(self._RECONCILIATION_STEPS):
            labels = set(issue.labels)
            progress = labels & {"status:in-progress", "status:in-review"}
            if (issue.state != "open" or "status:blocked" in labels
                    or "status:in-review" in labels or len(progress) > 1
                    or len(issue.assignees) > 1
                    or (issue.assignees
                        and issue.assignees != (record.actor_login,))):
                return self._finish(
                    marker_comment_id, record,
                    "Issue 的领取补偿遇到外部状态冲突，已停止写入。",
                    "conflict",
                )
            if (not issue.assignees and "intern:claimable" in labels
                    and not progress):
                self._mark_failed(marker_comment_id, record)
                return "failed"
            if issue.assignees == (record.actor_login,):
                mutation = lambda: self.gateway.remove_assignee(
                    event.issue_number, record.actor_login
                )
            elif "status:in-progress" in labels:
                mutation = lambda: self.gateway.remove_label(
                    event.issue_number, "status:in-progress"
                )
            elif "intern:claimable" not in labels:
                mutation = lambda: self.gateway.add_label(
                    event.issue_number, "intern:claimable"
                )
            else:
                break
            failed, issue = self._mutate_and_read(
                event, marker_comment_id, record, mutation
            )
            if failed:
                return "failed"
        self._mark_failed(marker_comment_id, record)
        return "failed"

    def _compensate_release(self, event: IssueCommentEvent,
                            issue: IssueSnapshot, record: InternRecord,
                            marker_comment_id: int) -> str:
        """Restore a partial release when possible, otherwise leave it replayable."""
        claimant = record.claimant_login
        if claimant is None or claimant == "qykw":
            self._mark_failed(marker_comment_id, record)
            return "failed"
        for _ in range(self._RECONCILIATION_STEPS):
            labels = set(issue.labels)
            if (issue.state != "open" or "status:blocked" in labels
                    or "status:in-review" in labels
                    or len(issue.assignees) > 1
                    or (issue.assignees and issue.assignees != (claimant,))):
                self._mark_failed(marker_comment_id, record)
                return "failed"
            if (not issue.assignees and "intern:claimable" in labels
                    and "status:in-progress" not in labels):
                return self._finish(
                    marker_comment_id, record,
                    "Issue 已成功释放。", "reconciled",
                )
            if (issue.assignees == (claimant,)
                    and "status:in-progress" in labels
                    and "intern:claimable" not in labels):
                self._mark_failed(marker_comment_id, record)
                return "failed"
            if issue.assignees == (claimant,):
                mutation = lambda: self.gateway.remove_assignee(
                    event.issue_number, claimant
                )
            elif "intern:claimable" in labels:
                mutation = lambda: self.gateway.remove_label(
                    event.issue_number, "intern:claimable"
                )
            elif "status:in-progress" not in labels:
                mutation = lambda: self.gateway.add_label(
                    event.issue_number, "status:in-progress"
                )
            else:
                mutation = lambda: self.gateway.add_assignee(
                    event.issue_number, claimant
                )
            failed, issue = self._mutate_and_read(
                event, marker_comment_id, record, mutation
            )
            if failed:
                return "failed"
        self._mark_failed(marker_comment_id, record)
        return "failed"

    def _publish_status(self, event: IssueCommentEvent, issue: IssueSnapshot,
                        record: InternRecord, marker_comment_id: int) -> str:
        labels = set(issue.labels)
        if issue.state != "open":
            message, stage = "Issue 已关闭。", "reconciled"
        elif len(issue.assignees) > 1:
            message, stage = "Issue 状态冲突：存在多个 Assignee。", "conflict"
        elif "status:blocked" in labels:
            message, stage = "Issue 当前已阻塞。", "reconciled"
        elif (not issue.assignees and "intern:claimable" in labels
              and not labels & {"status:in-progress", "status:in-review"}):
            message, stage = "Issue 当前可领取。", "reconciled"
        elif (len(issue.assignees) == 1 and "status:in-progress" in labels
              and "intern:claimable" not in labels and "status:in-review" not in labels):
            message, stage = f"Issue 由 @{issue.assignees[0]} 处理中。", "reconciled"
        elif (len(issue.assignees) == 1 and "status:in-review" in labels
              and "intern:claimable" not in labels and "status:in-progress" not in labels):
            message, stage = f"Issue 由 @{issue.assignees[0]} 提交，当前审查中。", "reconciled"
        else:
            message, stage = "Issue 状态冲突，请管理员核对 Assignee 和标签。", "conflict"
        return self._finish(marker_comment_id, record, message, stage)

    def _mutate_and_read(self, event: IssueCommentEvent, marker_comment_id: int,
                         record: InternRecord, mutation: Callable[[], None]) -> tuple[bool, IssueSnapshot]:
        try:
            mutation()
        except InternError:
            pass
        for _ in range(self._TRANSIENT_ATTEMPTS):
            try:
                issue, _ = self._read_state(event)
                return False, issue
            except InternError:
                pass
        self._mark_failed(marker_comment_id, record)
        return True, IssueSnapshot(event.issue_number, "unknown", (), ())

    def _read_state(self, event: IssueCommentEvent) -> tuple[IssueSnapshot, tuple[InternRecord, ...]]:
        issue = self.gateway.get_issue(event.issue_number)
        comments = self.gateway.list_issue_comments(event.issue_number)
        records = reduce_records(
            comments,
            repository_id=event.repository_id,
            repository=event.repository,
            issue_number=event.issue_number,
        )
        return issue, records

    def _finish(self, marker_comment_id: int, record: InternRecord, message: str, stage: str) -> str:
        terminal = self._with_stage(record, stage)
        if not self._update_command_marker(marker_comment_id, terminal, message):
            return "failed"
        return stage

    def _mark_failed(self, marker_comment_id: int, record: InternRecord) -> None:
        self._update_command_marker(
            marker_comment_id, self._with_stage(record, "failed"),
            "处理暂时失败，重放同一事件将自动重试。",
        )

    def _ensure_reaction(self, comment_id: int) -> bool:
        for _ in range(self._TRANSIENT_ATTEMPTS):
            try:
                if self.gateway.has_reaction(comment_id):
                    return True
            except InternError:
                continue
            try:
                self.gateway.add_reaction(comment_id)
            except InternError:
                pass
            try:
                if self.gateway.has_reaction(comment_id):
                    return True
            except InternError:
                pass
        return False

    def _ensure_command_marker(self, event: IssueCommentEvent,
                               record: InternRecord) -> int | None:
        for _ in range(self._TRANSIENT_ATTEMPTS):
            try:
                existing = self._marker_comment_id(event, record.operation_key)
            except InternError:
                continue
            if existing is not None:
                return existing
            try:
                return self.gateway.create_comment(
                    event.issue_number, self._body("正在处理该命令。", record)
                )
            except InternError:
                pass
        try:
            return self._marker_comment_id(event, record.operation_key)
        except InternError:
            return None

    def _update_command_marker(self, comment_id: int, record: InternRecord,
                               message: str) -> bool:
        body = self._body(message, record)
        for _ in range(self._TRANSIENT_ATTEMPTS):
            try:
                self.gateway.update_comment(comment_id, body)
                return True
            except InternError:
                pass
            try:
                for comment in self.gateway.list_issue_comments(record.issue_number):
                    candidate = decode_marker(comment.body, repository=record.repository)
                    if (comment.comment_id == comment_id and candidate is not None
                            and _intern_record_identity(candidate) == _intern_record_identity(record)
                            and candidate.stage == record.stage):
                        return True
            except InternError:
                pass
        return False

    def _marker_comment_id(self, event: IssueCommentEvent,
                           operation_key: tuple[int, int, int, str]) -> int | None:
        matches: list[int] = []
        for comment in self.gateway.list_issue_comments(event.issue_number):
            if comment.author_login != "qykw":
                continue
            record = decode_marker(comment.body, repository=event.repository)
            if record is not None and record.operation_key == operation_key:
                matches.append(comment.comment_id)
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _operation(command: InternCommand) -> str:
        return {
            InternCommand.ASSIGN: "assign",
            InternCommand.UNASSIGN: "unassign",
            InternCommand.STATUS: "status",
        }[command]

    @staticmethod
    def _with_stage(record: InternRecord, stage: str) -> InternRecord:
        return InternRecord(
            record.repository_id, record.repository, record.issue_number,
            record.trigger_comment_id, record.actor_login, record.operation,
            record.claimant_login, record.pull_number, stage,
        )

    @staticmethod
    def _body(message: str, record: InternRecord) -> str:
        return f"{message}\n\n{record.marker()}"


def _intern_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (not isinstance(value, str) or parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise InternError("invalid_api_origin")
    return urlunsplit(("https", parsed.netloc, "", "", ""))


def _intern_repository(value: object) -> str:
    if not isinstance(value, str) or not _REPOSITORY.fullmatch(value):
        raise InternError("invalid_repository")
    return value


def _intern_positive(value: object, code: str) -> int:
    if type(value) is not int or value <= 0:
        raise InternError(code)
    return value


def _intern_login(value: object) -> str:
    if not isinstance(value, str) or not _LOGIN.fullmatch(value):
        raise InternError("invalid_login")
    return value


def _intern_label(value: object) -> str:
    if value not in _INTERN_LABELS:
        raise InternError("invalid_label")
    return value  # type: ignore[return-value]


def _intern_mapping(value: object, code: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InternError(code)
    return value


def _intern_string(value: object, code: str) -> str:
    if not isinstance(value, str) or not value:
        raise InternError(code)
    return value


def _intern_optional_string(value: object, code: str) -> str | None:
    if value is None:
        return None
    return _intern_string(value, code)


def _intern_names(value: object, code: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise InternError(code)
    names: list[str] = []
    for item in value:
        name = _intern_string(_intern_mapping(item, code).get("name"), code)
        if name in names:
            raise InternError(code)
        names.append(name)
    return tuple(names)


def _intern_logins(value: object, code: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise InternError(code)
    logins: list[str] = []
    for item in value:
        login = _intern_login(_intern_mapping(item, code).get("login"))
        if login in logins:
            raise InternError(code)
        logins.append(login)
    return tuple(logins)


def _intern_write_body(value: object) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > _MAX_INTERN_WRITE_BYTES:
        raise InternError("invalid_write_body")
    return value


def _intern_comment_locator(value: object, api_url: str, repository: str, issue_number: int) -> None:
    expected = f"{api_url}/repos/{repository}/issues/{issue_number}"
    if value != expected:
        raise InternError("comment_repository_mismatch")


def _intern_collection_page(url: str) -> int:
    query = urlsplit(url).query
    if query == "per_page=100":
        return 1
    match = re.fullmatch(r"per_page=100&page=([2-9][0-9]*)", query)
    if match is None:
        raise InternError("unsafe_pagination")
    return int(match.group(1))


def _intern_record_identity(record: InternRecord) -> tuple[object, ...]:
    return (
        record.repository_id, record.repository, record.issue_number, record.trigger_comment_id,
        record.actor_login, record.operation, record.claimant_login, record.pull_number,
    )


def _intern_allowed_route(method: str, url: str, body: bytes | None, api_url: str, repository: str) -> None:
    parsed = urlsplit(url)
    if method != "GET" and parsed.query:
        raise InternError("invalid_request")
    if method == "GET" and body is None and url == f"{api_url}/user":
        return
    prefix = f"{api_url}/repos/{repository}/"
    if not url.startswith(prefix):
        raise InternError("invalid_request")
    path = parsed.path[len(urlsplit(prefix).path):]
    number = r"([1-9][0-9]*)"
    if method == "GET" and body is None:
        if re.fullmatch(rf"issues/{number}", path) or re.fullmatch(rf"pulls/{number}", path):
            if not parsed.query:
                return
        if re.fullmatch(rf"issues/{number}/comments", path):
            _intern_collection_page(url)
            return
        if re.fullmatch(rf"issues/comments/{number}/reactions", path):
            _intern_collection_page(url)
            return
        raise InternError("invalid_request")
    if method == "DELETE" and body is None:
        match = re.fullmatch(rf"issues/{number}/labels/([^/]+)", path)
        if match is not None and match.group(2) in {quote(label, safe="") for label in _INTERN_LABELS}:
            return
        raise InternError("invalid_request")
    payload = _intern_request_payload(body)
    if method in {"POST", "DELETE"} and re.fullmatch(rf"issues/{number}/assignees", path):
        if set(payload) == {"assignees"} and isinstance(payload["assignees"], list) and len(payload["assignees"]) == 1:
            _intern_login(payload["assignees"][0])
            return
    if method == "POST" and re.fullmatch(rf"issues/{number}/labels", path):
        if (set(payload) == {"labels"} and isinstance(payload["labels"], list)
                and len(payload["labels"]) == 1 and payload["labels"][0] in _INTERN_LABELS):
            return
    if method == "POST" and re.fullmatch(rf"issues/comments/{number}/reactions", path):
        if payload == {"content": "laugh"}:
            return
    if method == "POST" and re.fullmatch(rf"issues/{number}/comments", path):
        if set(payload) == {"body"}:
            _intern_write_body(payload["body"])
            return
    if method == "PATCH" and re.fullmatch(rf"issues/comments/{number}", path):
        if set(payload) == {"body"}:
            _intern_write_body(payload["body"])
            return
    if method == "PATCH" and re.fullmatch(rf"issues/{number}", path) and payload == {"state": "closed"}:
        return
    raise InternError("invalid_request")


def _intern_request_payload(body: bytes | None) -> Mapping[str, object]:
    if not isinstance(body, bytes):
        raise InternError("invalid_request")
    try:
        payload = json.loads(body.decode("utf-8"), object_pairs_hook=_intern_no_duplicates)
    except (UnicodeDecodeError, ValueError, TypeError):
        raise InternError("invalid_request") from None
    if not isinstance(payload, Mapping):
        raise InternError("invalid_request")
    return payload


def _intern_json(value: bytes) -> object:
    try:
        return json.loads(value.decode("utf-8"), object_pairs_hook=_intern_no_duplicates)
    except (UnicodeDecodeError, ValueError, TypeError):
        raise InternError("invalid_response") from None


def _intern_json_or_none(value: str) -> object | None:
    try:
        return json.loads(value, object_pairs_hook=_intern_no_duplicates)
    except (ValueError, TypeError):
        return None


def _intern_no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _intern_reject_constant(_: str) -> object:
    raise ValueError("invalid_constant")


def _intern_next_link(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InternError("unsafe_pagination")
    next_values: list[str] = []
    for item in value.split(","):
        match = re.fullmatch(r'\s*<([^<>\s]+)>\s*;\s*rel="([^"]+)"\s*', item)
        if match is None:
            raise InternError("unsafe_pagination")
        if match.group(2) == "next":
            next_values.append(match.group(1))
    if len(next_values) > 1:
        raise InternError("unsafe_pagination")
    return next_values[0] if next_values else None


class _InternNoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: object, code: int, msg: str, headers: object, newurl: str) -> None:
        return None


def _intern_stdlib_transport(method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> tuple[int, Mapping[str, str], bytes]:
    request = Request(url, data=body, headers=dict(headers), method=method)
    opener = build_opener(_InternNoRedirect())
    try:
        with opener.open(request, timeout=20) as response:
            return response.status, dict(response.headers.items()), response.read(_MAX_INTERN_RESPONSE_BYTES + 1)
    except HTTPError as error:
        return error.code, dict(error.headers.items()) if error.headers else {}, error.read(_MAX_INTERN_RESPONSE_BYTES + 1)
    except URLError:
        raise InternError("transport_failed") from None


def _bounded_issue_number(value: object) -> int:
    if type(value) is not int or value <= 0 or value > _MAX_ISSUE_NUMBER:
        raise InternError("invalid_issue_number")
    return value


def _cli_environment(environment: Mapping[str, str], phase: str) -> tuple[str, str, str]:
    repository = _intern_repository(environment.get("GITHUB_REPOSITORY"))
    api_url = _intern_origin(environment.get("GITHUB_API_URL", ""))
    action = environment.get("GITHUB_ACTION")
    if not isinstance(action, str) or _GITHUB_ACTION.fullmatch(action) is None:
        raise InternError("invalid_github_action")
    expected_event = "issue_comment" if phase == "issue-command" else "pull_request_target"
    if environment.get("GITHUB_EVENT_NAME") != expected_event:
        raise InternError("event_phase_mismatch")
    event_path = environment.get("GITHUB_EVENT_PATH")
    if not isinstance(event_path, str) or not event_path or len(event_path) > 4096 or "\x00" in event_path:
        raise InternError("invalid_event_file")
    return repository, api_url, event_path


def _load_cli_event(event_path: str, repository: str) -> Mapping[str, object]:
    path = Path(event_path)
    try:
        if not path.is_absolute() or path.is_symlink():
            raise InternError("invalid_event_file")
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise InternError("invalid_event_file")
        if metadata.st_size > _MAX_EVENT_BYTES:
            raise InternError("event_too_large")
        raw = path.read_bytes()
    except InternError:
        raise
    except OSError:
        raise InternError("invalid_event_file") from None
    if len(raw) > _MAX_EVENT_BYTES:
        raise InternError("event_too_large")
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_intern_no_duplicates,
            parse_constant=_intern_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, TypeError):
        raise InternError("invalid_event_json") from None
    if not isinstance(payload, Mapping):
        raise InternError("invalid_event_json")
    event_repository = payload.get("repository")
    if not isinstance(event_repository, Mapping):
        raise InternError("invalid_event")
    event_name = event_repository.get("full_name")
    if not isinstance(event_name, str):
        raise InternError("invalid_event")
    if event_name != repository:
        raise InternError("event_repository_mismatch")
    return payload


def _normalize_cli_issue_event(payload: Mapping[str, object]) -> IssueCommentEvent | None:
    comment = payload.get("comment")
    if not isinstance(comment, Mapping) or not isinstance(comment.get("body"), str):
        raise InternError("invalid_issue_event")
    validation_payload = dict(payload)
    validation_payload["comment"] = {**comment, "body": InternCommand.STATUS.value}
    validated = normalize_issue_comment_event(validation_payload)
    if validated is None:
        raise InternError("invalid_issue_event")
    command = parse_intern_command(comment["body"])
    if command is None:
        return None
    return IssueCommentEvent(
        validated.repository,
        validated.repository_id,
        validated.issue_number,
        validated.comment_id,
        validated.actor_login,
        command,
        validated.created_at,
    )


def _cli_token(environment: Mapping[str, str], name: str) -> str:
    token = environment.get(name)
    if not isinstance(token, str) or not token or len(token) > 4096 or "\n" in token or "\r" in token:
        raise InternError("invalid_token")
    return token


def _cli_resolved_issue_number(environment: Mapping[str, str]) -> int:
    value = environment.get("QYKW_RESOLVED_ISSUE_NUMBER")
    if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]{0,17}", value) is None:
        raise InternError("invalid_issue_number")
    return _bounded_issue_number(int(value))


def _write_issue_output(environment: Mapping[str, str], issue_number: object) -> None:
    number = _bounded_issue_number(issue_number)
    output_value = environment.get("GITHUB_OUTPUT")
    if not isinstance(output_value, str) or not output_value or len(output_value) > 4096 or "\x00" in output_value:
        raise InternError("invalid_output_file")
    path = Path(output_value)
    try:
        if not path.is_absolute() or path.is_symlink() or not path.parent.is_dir():
            raise InternError("invalid_output_file")
        if path.exists():
            metadata = path.stat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_OUTPUT_BYTES:
                raise InternError("invalid_output_file")
        with path.open("a", encoding="utf-8", newline="") as output:
            output.write(f"issue_number={number}\n")
    except InternError:
        raise
    except OSError:
        raise InternError("invalid_output_file") from None


def _cli_outcome_status(outcome: object) -> int:
    if not isinstance(outcome, InternOutcome) or outcome.status not in {
        "noop", "reconciled", "conflict", "failed",
    }:
        raise InternError("invalid_outcome")
    if outcome.status == "failed":
        raise InternError("reconcile_failed")
    return 0


def _run_cli(phase: str, environment: Mapping[str, str]) -> int:
    repository, api_url, event_path = _cli_environment(environment, phase)
    payload = _load_cli_event(event_path, repository)

    if phase == "issue-command":
        event = _normalize_cli_issue_event(payload)
        if event is None:
            return 0
        token = _cli_token(environment, "QYKW_INTERN_TOKEN")
        gateway = HttpInternGateway(api_url, repository, token)
        return _cli_outcome_status(InternClaimService(gateway).handle_issue_event(event))

    event = normalize_pull_event(payload)
    if event is None:
        raise InternError("invalid_pull_event")
    if phase == "resolve-pr":
        token = _cli_token(environment, "GITHUB_TOKEN")
        gateway = HttpInternGateway(api_url, repository, token)
        issue_number = resolve_pull_issue_number(event, gateway)
        if issue_number is not None:
            _write_issue_output(environment, issue_number)
        return 0

    expected_issue_number = _cli_resolved_issue_number(environment)
    token = _cli_token(environment, "QYKW_INTERN_TOKEN")
    gateway = HttpInternGateway(api_url, repository, token)
    outcome = InternClaimService(gateway).handle_pull_event(
        event, expected_issue_number=expected_issue_number,
    )
    return _cli_outcome_status(outcome)


def _emit_cli_error(error: InternError, stream: TextIO) -> None:
    code = error.code if isinstance(error.code, str) and _ERROR_CODE.fullmatch(error.code) else "internal_error"
    print(f"::error title=qykw intern::{code}", file=stream)


def main(argv: list[str] | None = None, *, environment: Mapping[str, str] | None = None) -> int:
    """Run one credential-separated intern controller phase."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    environment = os.environ if environment is None else environment
    try:
        if len(arguments) != 2 or arguments[0] != "--phase" or arguments[1] not in _CLI_PHASES:
            raise InternError("invalid_phase")
        return _run_cli(arguments[1], environment)
    except InternError as error:
        _emit_cli_error(error, sys.stderr)
        return 1
    except Exception:
        _emit_cli_error(InternError("internal_error"), sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
