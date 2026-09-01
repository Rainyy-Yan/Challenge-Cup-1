"""Deterministic qykw mention parsing and command routing."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
import re

from tools.qykw.domain import CommandMode, CommandName, CommandRequest, CommandRoute


_COMMANDS = {command.value: command for command in CommandName}
_CHANGE_COMMANDS = frozenset({CommandName.FIX, CommandName.IMPLEMENT})
_FENCE_START = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_INLINE_CODE = re.compile(r"`+[^`]*`+")
_ZERO_WIDTH = "\u200b-\u200d\u2060\ufeff"
_ACCOUNT_LEFT = rf"A-Za-z0-9_.+\-{_ZERO_WIDTH}"
_ACCOUNT_RIGHT = rf"A-Za-z0-9_.\-{_ZERO_WIDTH}"


class CommandRouter:
    """Resolve command routes from an immutable, program-owned table."""

    ROUTES: Mapping[CommandName, CommandRoute] = MappingProxyType(
        {
            CommandName.HELP: CommandRoute.DETERMINISTIC,
            CommandName.STATUS: CommandRoute.DETERMINISTIC,
            CommandName.SUMMARY: CommandRoute.DETERMINISTIC,
            CommandName.STOP: CommandRoute.DETERMINISTIC,
            CommandName.ANALYZE: CommandRoute.ADVISORY,
            CommandName.PLAN: CommandRoute.ADVISORY,
            CommandName.REVIEW: CommandRoute.REVIEW,
            CommandName.REREVIEW: CommandRoute.REVIEW,
            CommandName.FIX: CommandRoute.CHANGE,
            CommandName.IMPLEMENT: CommandRoute.CHANGE,
        }
    )

    def resolve(self, command: CommandRequest) -> CommandRoute:
        """Return the fixed route for a parsed command."""

        return self.ROUTES[command.name]


def parse_command(body: str, bot_login: str = "qykw") -> CommandRequest | None:
    """Parse a valid first-paragraph bot mention into a safe command request."""

    if not bot_login:
        return None
    paragraph = _first_effective_paragraph(body)
    if not paragraph:
        return None

    mention = _mention_pattern(bot_login).search(paragraph)
    if mention is None:
        return None
    content = paragraph[mention.end() :].strip()
    if not content:
        return CommandRequest(CommandName.ANALYZE, "", CommandMode.READ_ONLY)

    parts = content.split(maxsplit=1)
    keyword = parts[0]
    argument = parts[1] if len(parts) == 2 else ""
    name = _COMMANDS.get(keyword, CommandName.ANALYZE)
    if name is CommandName.ANALYZE and keyword not in _COMMANDS:
        argument = content
    mode = CommandMode.CHANGE if name in _CHANGE_COMMANDS else CommandMode.READ_ONLY
    return CommandRequest(name, argument.strip(), mode)


def _first_effective_paragraph(body: str) -> str:
    visible = _remove_html_comments(body)
    lines: list[str] = []
    in_fence: str | None = None

    for raw_line in visible.splitlines():
        fence = _FENCE_START.match(raw_line)
        if fence is not None:
            marker = fence.group(1)
            if in_fence is None:
                in_fence = marker[0]
            elif marker[0] == in_fence:
                in_fence = None
            continue
        if in_fence is not None or _is_blockquote(raw_line):
            continue
        if not raw_line.strip():
            if lines:
                return "\n".join(lines)
            continue

        text = _INLINE_CODE.sub("", raw_line).strip()
        if text:
            lines.append(text)

    return "\n".join(lines)


def _remove_html_comments(body: str) -> str:
    return re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)


def _is_blockquote(line: str) -> bool:
    return bool(re.match(r"^\s{0,3}>", line))


def _mention_pattern(bot_login: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?<![{_ACCOUNT_LEFT}])@{re.escape(bot_login)}(?![{_ACCOUNT_RIGHT}])",
        re.IGNORECASE,
    )
