"""Deterministic, comment-only publication for qykw reviews."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import html
import json
import logging
import re
from typing import TYPE_CHECKING

from tools.qykw.domain import (
    DiffSide, Finding, InlineComment, PublishResult, ReviewComment, ReviewResult,
    RunContext, RunRecord, RunStatus, Severity, TriggerRef,
)
from tools.qykw.github import GitHubGateway
from tools.qykw.state import render_state_marker

if TYPE_CHECKING:
    from tools.qykw.state import RunStateStore

_BOT_LOGIN = "qykw"
_MAX_FINDINGS = 20
_MAX_PUBLIC_TEXT = 900
_FINGERPRINT_PATTERN = re.compile(r"<!--\s*qykw-fingerprint:v1\s+(\{[^<>]{1,2048}\})\s*-->")
_HTML = re.compile(r"<[^>]{0,512}>")
_AUTOLINK = re.compile(r"<\s*(?:https?|mailto):[^>]{0,2048}>", re.IGNORECASE)
_IMAGE = re.compile(r"!\[([^\]]{0,512})\]\([^)]{0,2048}\)")
_LINK = re.compile(r"\[([^\]]{0,512})\]\([^)]{0,2048}\)")
_URI_SCHEME = re.compile(r"\b(?![A-Za-z]:[\\/])[A-Za-z][A-Za-z0-9+.\-]{0,63}:(?!\s)[^\s<>\[\]()`]{1,2048}", re.IGNORECASE)
_DANGEROUS_OBFUSCATED_SCHEME = re.compile(
    r"\b(?:j\s*a\s*v\s*a\s*s\s*c\s*r\s*i\s*p\s*t|v\s*b\s*s\s*c\s*r\s*i\s*p\s*t|"
    r"d\s*a\s*t\s*a|f\s*i\s*l\s*e|m\s*a\s*i\s*l\s*t\s*o|h\s*t\s*t\s*p\s*s?|f\s*t\s*p|s\s*s\s*h)\s*:\s*[^\s<>\[\]()`]{1,2048}",
    re.IGNORECASE,
)
_EXTERNAL_HOST = r"(?:localhost|(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,63}|(?:\d{1,3}\.){3}\d{1,3}|\[[0-9A-Fa-f:.]+\])"
_SCHEME_RELATIVE = re.compile(r"(?<![:\w])//(?=" + _EXTERNAL_HOST + r"(?::\d+)?(?:[/?#]|\s|$))[^\s<>\[\]()`]{1,2048}")
_WWW = re.compile(r"\bwww\.[^\s<>\[\]()`]{1,2048}", re.IGNORECASE)
_ENCODED_SCHEME = re.compile(r"\b[A-Za-z][A-Za-z0-9+.\-]{0,63}%3a(?!\s)[^\s<>\[\]()`]{1,2048}", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_MARKDOWN = re.compile(r"([`*_{}\[\]<>#+|])")
_SEVERITY_ORDER = {Severity.P0: 0, Severity.P1: 1, Severity.P2: 2}


def render_fingerprint_marker(run: RunContext, finding: Finding) -> str:
    payload = {"version": 1, "kind": "fingerprint", "head_sha": run.source_head_sha,
        "path": finding.path, "line": finding.line, "side": finding.side.value,
        "fingerprint": finding.fingerprint}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    encoded = encoded.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return "<!-- qykw-fingerprint:v1 " + encoded + " -->"


class ReviewPublisher:
    """Publish sanitized review output in a fixed summary-before-lines order."""

    def __init__(self, gateway: GitHubGateway, state_store: RunStateStore | None = None, *,
                 max_findings: int = _MAX_FINDINGS, logger: logging.Logger | None = None) -> None:
        if not isinstance(max_findings, int) or max_findings <= 0 or max_findings > _MAX_FINDINGS:
            raise ValueError("invalid_max_findings")
        self.gateway = gateway
        self._state = state_store
        self._max_findings = max_findings
        self._logger = logger or logging.getLogger(__name__)

    def acknowledge(self, run: RunContext) -> int:
        if run.trigger_comment_id is not None and run.trigger_comment_kind is not None:
            kind = "issue_comment" if run.trigger_comment_kind.value == "issue" else "review_comment"
            try:
                self.gateway.try_add_reaction(TriggerRef(kind, run.trigger_comment_id), "laugh")
            except Exception:
                self._safe_log("ack_reaction_failed", run.pr_number)
        return self.gateway.create_issue_comment(run.pr_number, "qykw 已收到请求，正在处理。")

    def publish_status(self, record: RunRecord, *, write_guard: Callable[[], bool] | None = None) -> None:
        current = self._current_record(record.context)
        if current is None or current.prompt_version != record.prompt_version:
            self._safe_log("status_state_unavailable", record.context.pr_number)
            return
        if not _write_allowed(write_guard):
            self._safe_log("status_write_blocked", record.context.pr_number)
            return
        body = f"qykw 运行状态：{current.status.value}；阶段：{current.stage.value}。\n\n{render_state_marker(current)}"
        self.gateway.update_issue_comment(current.summary_comment_id, body)  # type: ignore[arg-type]
        self._safe_log("status_published", current.context.pr_number)

    def publish_review(self, run: RunContext, result: ReviewResult, *,
                       write_guard: Callable[[], bool] | None = None) -> PublishResult:
        current = self._current_record(run)
        summary = _summary_body(result)
        if current is None:
            self._safe_log("summary_state_unavailable", run.pr_number)
            return PublishResult(RunStatus.FAILED, 0, summary, None, (), ("state_unavailable",))
        if any(isinstance(item, Finding) and _api_path(item.path) is None for item in result.findings):
            self._safe_log("unsafe_inline_path", run.pr_number)
            return PublishResult(RunStatus.FAILED, 0, summary, None, (), ("unsafe_inline_path",))
        if not _write_allowed(write_guard):
            self._safe_log("summary_write_blocked", run.pr_number)
            return PublishResult(RunStatus.FAILED, 0, summary, None, (), ("write_blocked",))
        try:
            summary_id = self._publish_summary(current, summary, write_guard=write_guard)
        except Exception:
            self._safe_log("summary_publish_failed", run.pr_number)
            return PublishResult(RunStatus.FAILED, 0, summary, None, (), ("summary_publish_failed",))
        candidates = _limited_sorted(result.findings, self._max_findings)
        existing = _existing_fingerprints(self.gateway.list_review_comments(run.pr_number), run)
        pending = tuple(item for item in candidates if _fingerprint_key(run, item) not in existing)
        if not pending:
            return PublishResult(RunStatus.COMPLETED, summary_id, summary, None, (), ())
        if not _write_allowed(write_guard):
            return PublishResult(RunStatus.FAILED, summary_id, summary, None, (), ("write_blocked",))
        try:
            review_id = self.gateway.create_review(run.pr_number, head_sha=run.source_head_sha,
                body="qykw 行评。", comments=tuple(_inline(run, item) for item in pending))
            return PublishResult(RunStatus.COMPLETED, summary_id, summary, review_id,
                tuple(item.fingerprint for item in pending), ())
        except Exception:
            return self._publish_individually(run, summary_id, summary, pending, write_guard=write_guard)

    def _publish_summary(self, record: RunRecord, body: str, *, write_guard: Callable[[], bool] | None = None) -> int:
        comment_id = record.summary_comment_id
        if comment_id is None:
            raise ValueError("state_comment_unavailable")
        if not _write_allowed(write_guard):
            raise RuntimeError("write_blocked")
        public_body = body + "\n\n" + render_state_marker(record)
        self.gateway.update_issue_comment(comment_id, public_body)
        return comment_id

    def _current_record(self, run: RunContext) -> RunRecord | None:
        if self._state is None:
            return None
        stored = self._state.get(run.pr_number, run.run_id)
        if stored is None or stored.context != run or stored.summary_comment_id is None:
            return None
        return stored

    def _state_comment_id(self, run: RunContext) -> int | None:
        if self._state is not None:
            stored = self._state.get(run.pr_number, run.run_id)
            if stored is not None and stored.context == run and stored.summary_comment_id is not None:
                return stored.summary_comment_id
        return None

    def _publish_individually(self, run: RunContext, summary_id: int, summary: str,
                              pending: tuple[Finding, ...], *,
                              write_guard: Callable[[], bool] | None = None) -> PublishResult:
        published: list[str] = []
        warning = False
        review_id: int | None = None
        existing = _existing_fingerprints(self.gateway.list_review_comments(run.pr_number), run)
        for finding in pending:
            key = _fingerprint_key(run, finding)
            if key in existing:
                continue
            if not _write_allowed(write_guard):
                warning = True
                break
            try:
                review_id = self.gateway.create_review(run.pr_number, head_sha=run.source_head_sha,
                    body="qykw 行评。", comments=(_inline(run, finding),))
                published.append(finding.fingerprint)
                existing.add(key)
            except Exception:
                warning = True
                self._safe_log("inline_publish_failed", run.pr_number)
        return PublishResult(RunStatus.PARTIAL if warning else RunStatus.COMPLETED, summary_id, summary,
            review_id, tuple(published), ("inline_publish_failed",) if warning else ())

    def _safe_log(self, event: str, pr_number: int) -> None:
        self._logger.info("qykw_%s pr=%d", event, pr_number)


def _summary_body(result: ReviewResult) -> str:
    counts = {severity: sum(1 for item in result.findings if item.severity is severity) for severity in Severity}
    lines = ["## qykw 审查结果", f"结论：{_safe_text(result.conclusion)}", "", "### 问题统计",
        f"- P0：{counts[Severity.P0]}", f"- P1：{counts[Severity.P1]}", f"- P2：{counts[Severity.P2]}"]
    if not result.findings:
        lines.extend(("", "未发现有充分证据的问题"))
    else:
        lines.extend(("", "### 已验证问题"))
        for item in _limited_sorted(result.findings, _MAX_FINDINGS):
            lines.append(f"- {item.severity.value} `{_display_path(item.path)}`:{item.line}：{_safe_text(item.failure_path)}")
    coverage = result.coverage
    lines.extend(("", "### 覆盖情况"))
    if _valid_coverage(coverage):
        lines.extend((f"- 文件：{coverage.reviewed_files}/{coverage.total_files}",
            f"- 变更块：{coverage.reviewed_hunks}/{coverage.total_hunks}",
            f"- 覆盖说明：{'已完全覆盖' if coverage.explains_every_file else '未完全覆盖'}"))
        _append_reasons(lines, "覆盖遗漏", coverage.omissions)
    else:
        lines.append("- 覆盖信息不可用")
    if result.validation_notes:
        lines.extend(("", "### 验证说明"))
        lines.extend(f"- {_safe_text(note)}" for note in result.validation_notes[:20])
    if result.limitations:
        lines.extend(("", "### 限制"))
        _append_reasons(lines, "限制", result.limitations)
    return "\n".join(lines)


def _valid_coverage(coverage: object) -> bool:
    return (hasattr(coverage, "total_files") and all(isinstance(value, int) and not isinstance(value, bool)
        and 0 <= value <= 1_000_000 for value in (coverage.total_files, coverage.reviewed_files,
        coverage.total_hunks, coverage.reviewed_hunks)) and coverage.reviewed_files <= coverage.total_files
        and coverage.reviewed_hunks <= coverage.total_hunks and isinstance(coverage.explains_every_file, bool)
        and isinstance(coverage.omissions, tuple))


def _append_reasons(lines: list[str], label: str, values: object) -> None:
    if not isinstance(values, tuple):
        return
    bounded = values[:20]
    if not bounded:
        return
    count = min(len(values), 100)
    suffix = "+" if len(values) > 100 else ""
    lines.append(f"- {label}：{count}{suffix} 项")
    lines.extend(f"  - {_safe_text(value)}" for value in bounded)


def _inline(run: RunContext, finding: Finding) -> InlineComment:
    body = "\n".join((f"**{finding.severity.value}** {_safe_text(finding.failure_path)}",
        f"影响：{_safe_text(finding.impact)}", f"证据：{_safe_text(finding.evidence)}",
        f"建议：{_safe_text(finding.suggestion)}", f"验证：{_safe_text(finding.verification)}",
        render_fingerprint_marker(run, finding)))
    path = _api_path(finding.path)
    if path is None:
        raise ValueError("unsafe_inline_path")
    return InlineComment(path, finding.line, finding.side, body, finding.fingerprint)


def _limited_sorted(findings: Iterable[Finding], maximum: int) -> tuple[Finding, ...]:
    unique: dict[tuple[str, int, str, str], Finding] = {}
    for item in findings:
        if _valid_finding(item):
            unique.setdefault((item.path, item.line, item.side.value, item.fingerprint), item)
    return tuple(sorted(unique.values(), key=lambda item: (_SEVERITY_ORDER[item.severity], item.path,
        item.line, item.side.value, item.fingerprint))[:maximum])


def _existing_fingerprints(comments: Iterable[ReviewComment], run: RunContext) -> set[tuple[str, str, int, str, str]]:
    found: set[tuple[str, str, int, str, str]] = set()
    for comment in comments:
        if comment.author_login != _BOT_LOGIN:
            continue
        marker = _parse_fingerprint(comment)
        if marker is not None and marker[0] == run.source_head_sha and marker[1] == comment.path and marker[2] == comment.line and marker[3] == comment.side.value:
            found.add(marker)
    return found


def _parse_fingerprint(comment: ReviewComment) -> tuple[str, str, int, str, str] | None:
    match = _FINGERPRINT_PATTERN.search(comment.body)
    if match is None:
        return None
    try:
        payload = json.loads(match.group(1))
    except (TypeError, ValueError):
        return None
    expected = {"version", "kind", "head_sha", "path", "line", "side", "fingerprint"}
    if not isinstance(payload, Mapping) or set(payload) != expected or payload.get("version") != 1 or payload.get("kind") != "fingerprint":
        return None
    head, path, line, side, fingerprint = (payload.get(name) for name in ("head_sha", "path", "line", "side", "fingerprint"))
    if not all(isinstance(value, str) and value and len(value) <= 512 for value in (head, path, side, fingerprint)):
        return None
    if not isinstance(line, int) or isinstance(line, bool) or line <= 0 or side not in ("LEFT", "RIGHT"):
        return None
    return head, path, line, side, fingerprint


def _fingerprint_key(run: RunContext, finding: Finding) -> tuple[str, str, int, str, str]:
    return run.source_head_sha, finding.path, finding.line, finding.side.value, finding.fingerprint


def _valid_finding(item: Finding) -> bool:
    return (isinstance(item, Finding) and isinstance(item.side, DiffSide) and isinstance(item.severity, Severity)
        and isinstance(item.line, int) and not isinstance(item.line, bool) and item.line > 0 and bool(item.fingerprint))


def _safe_text(value: object) -> str:
    if not isinstance(value, str):
        return "信息不可用"
    text = html.unescape(html.unescape(value))
    text = _AUTOLINK.sub("", text)
    text = _IMAGE.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = _HTML.sub("", text)
    text = _URI_SCHEME.sub("", text)
    text = _DANGEROUS_OBFUSCATED_SCHEME.sub("", text)
    text = _SCHEME_RELATIVE.sub("", text)
    text = _WWW.sub("", text)
    text = _ENCODED_SCHEME.sub("", text)
    text = _CONTROL.sub(" ", text)
    text = " ".join(text.split()).replace("@", "＠")
    text = _MARKDOWN.sub(r"\\\1", text)
    return text[:_MAX_PUBLIC_TEXT] or "信息不可用"


def sanitize_public_text(value: object) -> str:
    """Render untrusted public fields through the one qykw sanitization boundary."""

    return _safe_text(value)


def _write_allowed(guard: Callable[[], bool] | None) -> bool:
    if guard is None:
        return True
    try:
        return guard() is True
    except Exception:
        return False


def _display_path(value: object) -> str:
    text = _safe_text(value)
    if _api_path(value) is None:
        return "未定位文件"
    return text[:512]


def _api_path(value: object) -> str | None:
    if (not isinstance(value, str) or not value or len(value) > 1024 or value.startswith("/")
            or "\\" in value or any(part in ("", ".", "..") for part in value.split("/"))
            or any(ord(char) < 32 for char in value)):
        return None
    return value
