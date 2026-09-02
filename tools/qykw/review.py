"""Staged, fail-closed qykw review orchestration and local finding validation."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import hashlib

from tools.qykw.domain import (
    ChangedLine, ContextChunk, ContextPlan, CoverageReport, DiffSide, Finding, FindingCandidate,
    PullSnapshot, ReviewResult, RunContext, Severity,
)
from tools.qykw.prompts import build_review_request, build_validation_request
from tools.qykw.provider import InferenceProvider, validate_provider_capabilities

_CANDIDATE_FIELDS = frozenset({"path", "line", "side", "severity", "failure_path", "impact", "evidence", "suggestion", "verification"})
_MAX_CANDIDATES = 100
_MAX_TEXT = 2_000
_MAX_FINDINGS = 100
# Counterexample work is capped at two locally valid candidates per final slot.
# This admits alternatives for validation without permitting unbounded provider work.
_VALIDATION_BATCH_MULTIPLIER = 2
_SEVERITY_ORDER = {Severity.P0: 0, Severity.P1: 1, Severity.P2: 2}


@dataclass(frozen=True)
class _SourcedCandidate:
    candidate: FindingCandidate
    chunk_id: str


class _InvalidModelOutput(Exception):
    """Signal a structurally invalid model result without masking execution failures."""


class ReviewEngine:
    """Run deep-review, counterexample, then local validation stages."""

    def __init__(self, provider: InferenceProvider, *, max_findings: int) -> None:
        if not isinstance(max_findings, int) or isinstance(max_findings, bool) or not 0 <= max_findings <= _MAX_FINDINGS:
            raise ValueError("invalid_max_findings")
        self.provider = provider
        self.max_findings = max_findings

    def review(self, run: RunContext, snapshot: PullSnapshot, plan: ContextPlan) -> ReviewResult:
        if not _same_review_identity(run, snapshot, plan):
            raise ValueError("review_identity_mismatch")
        _validate_plan_chunks(run, plan)
        try:
            candidates: list[_SourcedCandidate] = []
            for chunk in plan.chunks:
                candidates.extend(self._review_candidates(build_review_request(run, chunk, plan=plan), chunk))
            candidates = _prepare_validation_batch(candidates, plan, max_findings=self.max_findings)
            if not candidates:
                return ReviewResult(
                    "审查完成",
                    (),
                    plan.coverage,
                    ("没有可验证的本地候选。",),
                    ("未执行 PR 代码。",),
                )
            conclusion, retained, notes, limitations = self._validation(build_validation_request(
                run,
                tuple(item.candidate for item in candidates),
                candidate_sources=tuple(item.chunk_id for item in candidates),
            ))
            allowed = _validation_intersection(candidates, retained)
            findings = validate_findings((item.candidate for item in allowed), commentable_lines=plan.commentable_lines,
                                         max_findings=self.max_findings, manifest_order=plan.manifest.risk_order)
            return ReviewResult(
                conclusion,
                findings,
                plan.coverage,
                ("已执行候选反证验证。", *notes),
                ("未执行 PR 代码。", *limitations),
            )
        except _InvalidModelOutput:
            return _failure(plan.coverage)

    def _review_candidates(self, request: object, chunk: object) -> tuple[_SourcedCandidate, ...]:
        validate_provider_capabilities(self.provider, request)  # type: ignore[arg-type]
        response = self.provider.complete(request)  # type: ignore[arg-type]
        parsed = _parse_review_candidate_envelope(response.value, chunk)
        if parsed is None:
            raise _InvalidModelOutput from None
        return parsed

    def _validation(self, request: object) -> tuple[str, tuple[FindingCandidate, ...], tuple[str, ...], tuple[str, ...]]:
        validate_provider_capabilities(self.provider, request)  # type: ignore[arg-type]
        response = self.provider.complete(request)  # type: ignore[arg-type]
        parsed = parse_validation_response(response.value)
        if parsed is None:
            raise _InvalidModelOutput from None
        return parsed


def parse_candidates(value: object) -> tuple[FindingCandidate, ...]:
    """Parse one exact candidate envelope without any value coercion."""
    parsed = _parse_candidate_envelope(value)
    return () if parsed is None else parsed


def _parse_candidate_envelope(value: object) -> tuple[FindingCandidate, ...] | None:
    if not isinstance(value, Mapping) or set(value) != {"candidates"}:
        return None
    raw = value.get("candidates")
    if not isinstance(raw, list) or len(raw) > _MAX_CANDIDATES:
        return None
    parsed = tuple(_parse_candidate(item) for item in raw)
    return parsed if all(item is not None for item in parsed) else None  # type: ignore[return-value]


def _parse_review_candidate_envelope(value: object, chunk: object) -> tuple[_SourcedCandidate, ...] | None:
    if not isinstance(chunk, ContextChunk):
        return None
    if not isinstance(value, Mapping) or set(value) != {"candidates"}:
        return None
    raw = value.get("candidates")
    if not isinstance(raw, list) or len(raw) > _MAX_CANDIDATES:
        return None
    parsed: list[_SourcedCandidate] = []
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != _CANDIDATE_FIELDS | {"source_chunk_id"}:
            return None
        source_chunk_id = item.get("source_chunk_id")
        if not isinstance(source_chunk_id, str) or source_chunk_id != chunk.chunk_id:
            return None
        candidate = _parse_candidate({key: item[key] for key in _CANDIDATE_FIELDS})
        if candidate is None or candidate.path not in chunk.paths:
            return None
        parsed.append(_SourcedCandidate(candidate, source_chunk_id))
    return tuple(parsed)


def parse_validation_response(value: object) -> tuple[str, tuple[FindingCandidate, ...], tuple[str, ...], tuple[str, ...]] | None:
    required = {"conclusion", "findings", "validation_notes", "limitations"}
    if not isinstance(value, Mapping) or set(value) != required:
        return None
    conclusion = _text(value.get("conclusion"), 500)
    notes = _text_list(value.get("validation_notes"))
    limitations = _text_list(value.get("limitations"))
    raw_findings = value.get("findings")
    if not isinstance(raw_findings, list) or len(raw_findings) > _MAX_CANDIDATES:
        return None
    findings = tuple(_parse_candidate(item) for item in raw_findings)
    if conclusion is None or notes is None or limitations is None or not all(item is not None for item in findings):
        return None
    return conclusion, findings, notes, limitations  # type: ignore[return-value]


def validate_findings(candidates: Iterable[FindingCandidate], *, commentable_lines: frozenset[ChangedLine], max_findings: int, manifest_order: tuple[str, ...] = ()) -> tuple[Finding, ...]:
    """Reject unsupported candidates, dedupe valid locations, then cap deterministically."""
    if not isinstance(max_findings, int) or isinstance(max_findings, bool) or not 0 <= max_findings <= _MAX_FINDINGS:
        return ()
    order = {path: index for index, path in enumerate(manifest_order)}
    unique: dict[tuple[str, int, DiffSide, str], FindingCandidate] = {}
    for candidate in candidates:
        if not _supported(candidate, commentable_lines):
            continue
        key = (candidate.path, candidate.line, candidate.side, _normalized(candidate.failure_path))
        unique.setdefault(key, candidate)
    ordered = sorted(unique.values(), key=lambda item: (_SEVERITY_ORDER[item.severity], order.get(item.path, len(order)), item.path, item.line, item.side.value, _normalized(item.failure_path)))
    return tuple(_finding(item) for item in ordered[:max_findings])


def _parse_candidate(value: object) -> FindingCandidate | None:
    if not isinstance(value, Mapping) or set(value) != _CANDIDATE_FIELDS:
        return None
    path = _text(value.get("path"), 1_024)
    line = value.get("line")
    side_text = value.get("side")
    severity_text = value.get("severity")
    strings = [_text(value.get(key), _MAX_TEXT) for key in ("failure_path", "impact", "evidence", "suggestion", "verification")]
    if (path is None or not isinstance(line, int) or isinstance(line, bool) or line < 1
            or not isinstance(side_text, str) or not isinstance(severity_text, str) or any(item is None for item in strings)):
        return None
    try:
        return FindingCandidate(path, line, DiffSide(side_text), Severity(severity_text), *strings)  # type: ignore[arg-type]
    except ValueError:
        return None


def _text(value: object, maximum: int) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        return None
    return value


def _text_list(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list) or len(value) > 20:
        return None
    values = tuple(_text(item, _MAX_TEXT) for item in value)
    return values if all(item is not None for item in values) else None  # type: ignore[return-value]


def _same_review_identity(run: RunContext, snapshot: PullSnapshot, plan: ContextPlan) -> bool:
    return (snapshot.number == run.pr_number == plan.pr_number and snapshot.target_repository == run.repository == plan.repository
            and snapshot.source_head_sha == run.source_head_sha == plan.source_head_sha and _plan_id_matches(run, plan)
            and snapshot.target_base_sha == run.target_base_sha and snapshot.target_base_ref == run.target_base_ref
            and snapshot.source_repository == run.source_repository)


def _validation_intersection(candidates: Iterable[_SourcedCandidate], retained: Iterable[FindingCandidate]) -> tuple[_SourcedCandidate, ...]:
    candidate_keys = {_candidate_key(item.candidate): item for item in candidates}
    return tuple(candidate_keys[key] for key in (_candidate_key(item) for item in retained) if key in candidate_keys)


def _candidate_key(item: FindingCandidate) -> tuple[object, ...]:
    return (item.path, item.line, item.side, item.severity, item.failure_path, item.impact, item.evidence, item.suggestion, item.verification)


def _prepare_validation_batch(
    candidates: Iterable[_SourcedCandidate], plan: ContextPlan, *, max_findings: int
) -> list[_SourcedCandidate]:
    """Locally filter, semantic-dedupe, order, and cap counterexample work."""
    unique: dict[tuple[str, int, DiffSide, str, str], _SourcedCandidate] = {}
    for sourced in candidates:
        candidate = sourced.candidate
        if not _supported(candidate, plan.commentable_lines):
            continue
        key = (
            candidate.path,
            candidate.line,
            candidate.side,
            _normalized(candidate.failure_path),
            _normalized(candidate.evidence),
        )
        unique.setdefault(key, sourced)
    path_order = {path: index for index, path in enumerate(plan.manifest.risk_order)}
    ordered = sorted(
        unique.values(),
        key=lambda item: (
            _SEVERITY_ORDER[item.candidate.severity],
            path_order.get(item.candidate.path, len(path_order)),
            item.candidate.path,
            item.candidate.line,
            item.candidate.side.value,
            _normalized(item.candidate.failure_path),
            _normalized(item.candidate.evidence),
        ),
    )
    limit = min(_MAX_CANDIDATES, max_findings * _VALIDATION_BATCH_MULTIPLIER)
    return ordered[:limit]


def _supported(candidate: FindingCandidate, lines: frozenset[ChangedLine]) -> bool:
    if not isinstance(candidate, FindingCandidate) or ChangedLine(candidate.path, candidate.line, candidate.side) not in lines:
        return False
    text = " ".join((candidate.failure_path, candidate.impact, candidate.evidence, candidate.suggestion, candidate.verification)).lower()
    if any(marker in text for marker in ("might be", "security risk", "harden", "best practice", "consider ")):
        return False
    if candidate.severity is Severity.P0:
        catastrophic = any(marker in text for marker in ("irrecoverable", "data loss", "all tenant", "widespread", "service outage", "delete"))
        concrete = len(candidate.failure_path.split()) >= 5 and len(candidate.verification.split()) >= 5
        return catastrophic and concrete
    if candidate.severity is Severity.P1:
        return any(marker in text for marker in ("production", "primary", "authenticated", "all ", "deterministically", "authorization", "billing"))
    return True


def _finding(candidate: FindingCandidate) -> Finding:
    token = "\0".join((candidate.path, str(candidate.line), candidate.side.value, _normalized(candidate.failure_path))).encode("utf-8")
    return Finding(candidate.path, candidate.line, candidate.side, candidate.severity, candidate.failure_path,
                   candidate.impact, candidate.evidence, candidate.suggestion, candidate.verification,
                   hashlib.sha256(token).hexdigest()[:24])


def _normalized(value: str) -> str:
    return " ".join(value.split()).lower()


def _failure(coverage: CoverageReport) -> ReviewResult:
    return ReviewResult("审查未完成", (), coverage, ("结构化审查结果不可用。",), ("本次未发布未经验证的发现。",))


def _validate_plan_chunks(run: RunContext, plan: ContextPlan) -> None:
    if not _plan_id_matches(run, plan) or len({chunk.chunk_id for chunk in plan.chunks}) != len(plan.chunks):
        raise ValueError("invalid_plan_chunk_binding")
    prefix = (
        f"P run={run.run_id} rid={run.repository_id} repo={run.repository} pr={run.pr_number} "
        f"bs={run.target_base_sha} br={run.target_base_ref} hs={run.source_head_sha} "
    )
    for chunk in plan.chunks:
        if (not chunk.chunk_id.startswith(f"{plan.run_id}|chunk=") or not chunk.paths
                or any(path not in plan.manifest.paths for path in chunk.paths)
                or not chunk.text.startswith(prefix)):
            raise ValueError("invalid_plan_chunk_binding")


def _plan_id_matches(run: RunContext, plan: ContextPlan) -> bool:
    expected = (
        f"run_id={run.run_id}|repository_id={run.repository_id}|repository={run.repository}|pr={run.pr_number}"
        f"|base_sha={run.target_base_sha}|base_ref={run.target_base_ref}|head_sha={run.source_head_sha}"
    )
    return plan.run_id in {run.run_id, expected}
