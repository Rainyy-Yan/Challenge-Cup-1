"""Pure, strict parsing boundary for qykw intern-claim events."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
import re
from urllib.parse import urlsplit


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
_CLOSING = re.compile(r"(?i)(?<![A-Za-z0-9_./-])closes[ \t]+#([1-9][0-9]*)(?![0-9])")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_FENCE_END = re.compile(r"^ {0,3}(`+|~+)[ \t]*$")


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
    if len(matches) != 1:
        return None
    match = matches[0]
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
    for raw in body.splitlines():
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
        if re.match(r"^\s{0,3}>", raw):
            continue
        visible.append(raw if preserve_inline_code else re.sub(r"`[^`]*`", "", raw))
    return visible
