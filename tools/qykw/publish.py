"""Deterministic, comment-only publication for qykw reviews."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
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

if TYPE_CHECKING:
    from tools.qykw.state import RunStateStore

_BOT_LOGIN = "qykw"
_MAX_FINDINGS = 20
_MAX_PUBLIC_TEXT = 900
_FINGERPRINT_PATTERN = re.compile(r"<!--\s*qykw-fingerprint:v1\s+(\{[^<>]{1,2048}\})\s*-->")
_HTML = re.compile(r"<[^>]{0,512}>")
_IMAGE = re.compile(r"!?\[[^\]]{0,512}\]\([^)]{0,2048}\)")
_LINK = re.compile(r"\[([^\]]{0,512})\]\([^)]{0,2048}\)")
_URL = re.compile(r"(?:https?|ftp)://[^\s<>]{1,2048}", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SEVERITY_ORDER = {Severity.P0: 0, Severity.P1: 1, Severity.P2: 2}


def render_fingerprint_marker(run: RunContext, finding: Finding) -> str:
    payload = {"version": 1, "kind": "fingerprint", "head_sha": run.source_head_sha,
        "path": finding.path, "line": finding.line, "side": finding.side.value,
        "fingerprint": finding.fingerprint}
    return "<!-- qykw-fingerprint:v1 " + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + " -->"


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

    def publish_status(self, record: RunRecord) -> None:
        comment_id = record.summary_comment_id or self._state_comment_id(record.context)
        body = f"qykw 运行状态：{record.status.value}；阶段：{record.stage.value}。"
        if comment_id is None:
            self.gateway.create_issue_comment(record.context.pr_number, body)
        else:
            self.gateway.update_issue_comment(comment_id, body)
        self._safe_log("status_published", record.context.pr_number)

    def publish_review(self, run: RunContext, result: ReviewResult) -> PublishResult:
        summary = _summary_body(result)
        try:
            summary_id = self._publish_summary(run, summary)
        except Exception:
            self._safe_log("summary_publish_failed", run.pr_number)
            return PublishResult(RunStatus.FAILED, 0, summary, None, (), ("summary_publish_failed",))
        candidates = _limited_sorted(result.findings, self._max_findings)
        existing = _existing_fingerprints(self.gateway.list_review_comments(run.pr_number), run)
        pending = tuple(item for item in candidates if _fingerprint_key(run, item) not in existing)
        if not pending:
            return PublishResult(RunStatus.COMPLETED, summary_id, summary, None, (), ())
        try:
            review_id = self.gateway.create_review(run.pr_number, head_sha=run.source_head_sha,
                body="qykw 行评。", comments=tuple(_inline(run, item) for item in pending))
            return PublishResult(RunStatus.COMPLETED, summary_id, summary, review_id,
                tuple(item.fingerprint for item in pending), ())
        except Exception:
            return self._publish_individually(run, summary_id, summary, pending)

    def _publish_summary(self, run: RunContext, body: str) -> int:
        comment_id = self._state_comment_id(run)
        public_body = self._summary_with_state_marker(run, body)
        if comment_id is None:
            return self.gateway.create_issue_comment(run.pr_number, public_body)
        self.gateway.update_issue_comment(comment_id, public_body)
        return comment_id

    def _summary_with_state_marker(self, run: RunContext, body: str) -> str:
        if self._state is None:
            return body
        stored = self._state.get(run.pr_number, run.run_id)
        if stored is None:
            return body
        from tools.qykw.state import render_state_marker
        return body + "\n\n" + render_state_marker(stored)

    def _state_comment_id(self, run: RunContext) -> int | None:
        if self._state is not None:
            stored = self._state.get(run.pr_number, run.run_id)
            if stored is not None and stored.summary_comment_id is not None:
                return stored.summary_comment_id
        comments = getattr(self.gateway, "list_issue_comments", None)
        if callable(comments):
            # Never guess from a generic bot reply or a cancel marker: only an
            # existing state marker identifies a safe summary-update target.
            own = [item for item in comments(run.pr_number)
                   if item.author_login == _BOT_LOGIN and "<!-- qykw-state:" in item.body]
            if own:
                return max(own, key=lambda item: (item.updated_at, item.comment_id)).comment_id
        return None

    def _publish_individually(self, run: RunContext, summary_id: int, summary: str,
                              pending: tuple[Finding, ...]) -> PublishResult:
        published: list[str] = []
        warning = False
        review_id: int | None = None
        existing = _existing_fingerprints(self.gateway.list_review_comments(run.pr_number), run)
        for finding in pending:
            key = _fingerprint_key(run, finding)
            if key in existing:
                continue
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
            lines.append(f"- {item.severity.value} `{_safe_path(item.path)}`:{item.line}：{_safe_text(item.failure_path)}")
    if result.validation_notes:
        lines.extend(("", "### 验证说明"))
        lines.extend(f"- {_safe_text(note)}" for note in result.validation_notes[:20])
    if result.limitations:
        lines.extend(("", "### 限制"))
        lines.extend(f"- {_safe_text(note)}" for note in result.limitations[:20])
    return "\n".join(lines)


def _inline(run: RunContext, finding: Finding) -> InlineComment:
    body = "\n".join((f"**{finding.severity.value}** {_safe_text(finding.failure_path)}",
        f"影响：{_safe_text(finding.impact)}", f"证据：{_safe_text(finding.evidence)}",
        f"建议：{_safe_text(finding.suggestion)}", f"验证：{_safe_text(finding.verification)}",
        render_fingerprint_marker(run, finding)))
    return InlineComment(_safe_path(finding.path), finding.line, finding.side, body, finding.fingerprint)


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
    text = html.unescape(value)
    text = _IMAGE.sub("", text)
    text = _LINK.sub(r"\1", text)
    text = _HTML.sub("", text)
    text = _URL.sub("", text)
    text = _CONTROL.sub(" ", text)
    text = " ".join(text.split()).replace("@", "＠")
    return text[:_MAX_PUBLIC_TEXT] or "信息不可用"


def _safe_path(value: object) -> str:
    text = _safe_text(value)
    if text == "信息不可用" or text.startswith("/") or ".." in text.split("/"):
        return "未定位文件"
    return text[:512]
