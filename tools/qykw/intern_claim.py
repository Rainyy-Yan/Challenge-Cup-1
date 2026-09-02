"""Pure, strict parsing boundary for qykw intern-claim events."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
import json
import re
from typing import Protocol
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


@dataclass(frozen=True)
class PullSnapshot:
    number: int
    state: str
    merged: bool
    author_login: str
    body: str


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
    def assert_bot_identity(self, expected_login: str = "qykw") -> None: ...
    def get_issue(self, issue_number: int) -> IssueSnapshot: ...
    def list_issue_comments(self, issue_number: int) -> tuple[IssueComment, ...]: ...
    def list_pull_comments(self, pull_number: int) -> tuple[IssueComment, ...]: ...
    def get_pull(self, pull_number: int) -> PullSnapshot: ...
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
        return IssueSnapshot(number, state, labels, assignees)

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
        if body is None:
            body = ""
        return PullSnapshot(number, _intern_string(payload.get("state"), "invalid_pull"), merged,
                            _intern_login(_intern_mapping(payload.get("user"), "invalid_pull").get("login")),
                            _intern_string(body, "invalid_pull"))

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
            comments.append(IssueComment(
                _intern_positive(value.get("id"), "invalid_issue_comment"),
                _intern_login(_intern_mapping(value.get("user"), "invalid_issue_comment").get("login")),
                _intern_optional_string(value.get("body"), "invalid_issue_comment") or "",
                _intern_string(value.get("updated_at"), "invalid_issue_comment"),
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
    if not isinstance(name, str) or not name or not actor_login or actor_login.casefold() != sender_login.casefold():
        return None
    issue_url = issue.get("repository_url")
    issue_repo = _issue_repo_from_url(issue_url)
    if issue_repo != name:
        return None
    command = parse_intern_command(comment.get("body"))
    if None in (repository_id, issue_number, comment_id) or command is None:
        return None
    return IssueCommentEvent(name, repository_id, issue_number, comment_id, actor_login, command)


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

    if not isinstance(body, str):
        return None
    text = "\n".join(_visible_lines(body, preserve_inline_code=False))
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
        visible.append(raw if preserve_inline_code else re.sub(r"`[^`]*`", "", raw))
    return visible


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

    def __init__(self, gateway: InternGateway) -> None:
        self.gateway = gateway

    def handle_issue_event(self, event: IssueCommentEvent) -> InternOutcome:
        if not isinstance(event, IssueCommentEvent):
            raise InternError("invalid_issue_event")
        self.gateway.assert_bot_identity()
        comments = self.gateway.list_issue_comments(event.issue_number)
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
            try:
                self.gateway.add_reaction(comment.comment_id)
            except InternError:
                return "failed"
            try:
                marker_comment_id = self.gateway.create_comment(
                    event.issue_number, self._body("正在处理该命令。", record)
                )
            except InternError:
                return "failed"
        elif existing.stage == "failed":
            record = self._with_stage(existing, "pending")
            if marker_comment_id is None:
                return "conflict"
            try:
                self.gateway.update_comment(marker_comment_id, self._body("正在重试该命令。", record))
            except InternError:
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
            return self._reconcile_assign(event, issue, record, marker_comment_id)
        if command is InternCommand.UNASSIGN:
            return self._reconcile_release(event, issue, records, record, marker_comment_id)
        return self._publish_status(event, issue, record, marker_comment_id)

    def _reconcile_assign(self, event: IssueCommentEvent, issue: IssueSnapshot,
                          record: InternRecord, marker_comment_id: int) -> str:
        for _ in range(4):
            labels = set(issue.labels)
            assignees = issue.assignees
            if issue.state != "open":
                return self._finish(marker_comment_id, record, "Issue 已关闭，无法领取。", "reconciled")
            if "status:blocked" in labels:
                return self._finish(marker_comment_id, record, "Issue 已阻塞，暂不可领取。", "reconciled")
            if len(assignees) == 1 and assignees[0].casefold() != record.actor_login.casefold():
                if "intern:claimable" not in labels and len(labels & {"status:in-progress", "status:in-review"}) == 1:
                    return self._finish(
                        marker_comment_id, record,
                        f"该任务已由 @{assignees[0]} 领取，请选择其他 Issue。", "reconciled",
                    )
                return self._finish(marker_comment_id, record, "Issue 的 Assignee 与领取状态冲突，已停止写入。", "conflict")
            if len(assignees) > 1:
                return self._finish(marker_comment_id, record, "Issue 存在多个 Assignee 冲突，已停止写入。", "conflict")
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
            if not assignees and "intern:claimable" not in labels:
                return self._finish(marker_comment_id, record, "Issue 当前不可领取。", "reconciled")
            if assignees and assignees != (record.actor_login,):
                return self._finish(marker_comment_id, record, "Issue 的 Assignee 与领取状态冲突，已停止写入。", "conflict")

            if not assignees:
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
        return self._finish(marker_comment_id, record, "Issue 领取状态无法收敛。", "conflict")

    def _reconcile_release(self, event: IssueCommentEvent, issue: IssueSnapshot,
                           records: tuple[InternRecord, ...], record: InternRecord,
                           marker_comment_id: int) -> str:
        labels = set(issue.labels)
        if issue.state != "open":
            return self._finish(marker_comment_id, record, "Issue 已关闭，无法释放。", "reconciled")
        if "status:blocked" in labels:
            return self._finish(marker_comment_id, record, "Issue 已阻塞，已停止释放。", "reconciled")
        if len(issue.assignees) > 1:
            return self._finish(marker_comment_id, record, "Issue 存在多个 Assignee 冲突，已停止写入。", "conflict")
        if not issue.assignees:
            if "intern:claimable" in labels and not labels & {"status:in-progress", "status:in-review"}:
                return self._finish(marker_comment_id, record, "Issue 已处于可领取状态。", "reconciled")
            if record.claimant_login and record.claimant_login != "qykw":
                return self._continue_partial_release(event, issue, record, marker_comment_id)
            return self._finish(marker_comment_id, record, "Issue 的释放状态冲突，已停止写入。", "conflict")

        claimant = issue.assignees[0]
        authorized = (record.actor_login.casefold() == claimant.casefold() or record.actor_login == "xyh202131")
        if not authorized:
            return self._finish(marker_comment_id, record, f"@{record.actor_login} 无权释放 @{claimant} 领取的 Issue。", "reconciled")
        active_pull = any(item.operation == "pull" and item.pull_number is not None for item in records)
        if "status:in-review" in labels or active_pull:
            return self._finish(marker_comment_id, record, "Issue 存在活动 PR 或正在审查，不允许释放。", "reconciled")
        if "intern:claimable" in labels or "status:in-progress" not in labels:
            return self._finish(marker_comment_id, record, "Issue 的 Assignee 与进度标签冲突，已停止写入。", "conflict")
        if record.claimant_login != claimant:
            record = InternRecord(
                record.repository_id, record.repository, record.issue_number,
                record.trigger_comment_id, record.actor_login, record.operation,
                claimant, record.pull_number, "pending",
            )
            try:
                self.gateway.update_comment(marker_comment_id, self._body("正在处理释放命令。", record))
                issue, _ = self._read_state(event)
            except InternError:
                self._mark_failed(marker_comment_id, record)
                return "failed"
        failed, issue = self._mutate_and_read(
            event, marker_comment_id, record,
            lambda: self.gateway.remove_assignee(event.issue_number, claimant),
        )
        if failed:
            return "failed"
        return self._continue_partial_release(event, issue, record, marker_comment_id)

    def _continue_partial_release(self, event: IssueCommentEvent, issue: IssueSnapshot,
                                  record: InternRecord, marker_comment_id: int) -> str:
        for _ in range(3):
            labels = set(issue.labels)
            if issue.assignees:
                return self._finish(marker_comment_id, record, "Issue 的 Assignee 在释放时发生冲突。", "conflict")
            if "status:in-review" in labels:
                return self._finish(marker_comment_id, record, "Issue 正在审查，不允许释放。", "reconciled")
            if "status:in-progress" in labels:
                mutation = lambda: self.gateway.remove_label(event.issue_number, "status:in-progress")
            elif "intern:claimable" not in labels:
                mutation = lambda: self.gateway.add_label(event.issue_number, "intern:claimable")
            else:
                return self._finish(marker_comment_id, record, "Issue 已成功释放。", "reconciled")
            failed, issue = self._mutate_and_read(event, marker_comment_id, record, mutation)
            if failed:
                return "failed"
        return self._finish(marker_comment_id, record, "Issue 释放状态无法收敛。", "conflict")

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
            try:
                issue, _ = self._read_state(event)
            except InternError:
                issue = IssueSnapshot(event.issue_number, "unknown", (), ())
            self._mark_failed(marker_comment_id, record)
            return True, issue
        try:
            issue, _ = self._read_state(event)
            return False, issue
        except InternError:
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
        try:
            self.gateway.update_comment(marker_comment_id, self._body(message, terminal))
        except InternError:
            return "failed"
        return stage

    def _mark_failed(self, marker_comment_id: int, record: InternRecord) -> None:
        try:
            self.gateway.update_comment(
                marker_comment_id,
                self._body("处理暂时失败，重放同一事件将自动重试。", self._with_stage(record, "failed")),
            )
        except InternError:
            pass

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
