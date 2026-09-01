"""Tests for staged qykw review and deterministic finding validation."""

from __future__ import annotations

import unittest

from tools.qykw.domain import (
    ChangedLine, CommandMode, CommandName, CommandRequest, ContextChunk, ContextPlan,
    CoverageReport, DiffSide, FileManifest, FindingCandidate, InferenceResponse,
    InferenceUsage, ProviderCapabilities, PullSnapshot, RunContext, Severity,
)


def run() -> RunContext:
    return RunContext("QY-PR7-A1B2", "event-7", 7, "owner/repo", 7, "pull_request", "opened", "fork/repo", "h" * 40, "b" * 40, "main", CommandRequest(CommandName.REVIEW, "", CommandMode.READ_ONLY), "alice")


def snapshot() -> PullSnapshot:
    return PullSnapshot(7, "open", False, "fork/repo", "h" * 40, "owner/repo", "b" * 40, "main", "title", "body", (), (), (), ())


def plan() -> ContextPlan:
    coverage = CoverageReport(2, 2, 2, 2, (), True)
    return ContextPlan("owner/repo", 7, "h" * 40, "QY-PR7-A1B2", FileManifest(("src/a.py", "src/b.py"), ("src/b.py", "src/a.py")),
        (ContextChunk("c-a", ("src/a.py",), "context-a", 2), ContextChunk("c-b", ("src/b.py",), "context-b", 2)), coverage,
        frozenset({ChangedLine("src/a.py", 3, DiffSide.RIGHT), ChangedLine("src/a.py", 2, DiffSide.LEFT), ChangedLine("src/b.py", 9, DiffSide.RIGHT)}), 1000, 20_000)


def candidate(*, path: str = "src/a.py", line: int = 3, side: DiffSide = DiffSide.RIGHT,
              severity: Severity = Severity.P2, failure_path: str = "new call reaches None",
              impact: str = "local request fails", evidence: str = "changed branch omits guard",
              suggestion: str = "add guard", verification: str = "test missing input") -> FindingCandidate:
    return FindingCandidate(path, line, side, severity, failure_path, impact, evidence, suggestion, verification)


def candidate_value(item: FindingCandidate) -> dict[str, object]:
    return {"path": item.path, "line": item.line, "side": item.side.value, "severity": item.severity.value,
            "failure_path": item.failure_path, "impact": item.impact, "evidence": item.evidence,
            "suggestion": item.suggestion, "verification": item.verification}


class RecordingProvider:
    def __init__(self, values: list[object]) -> None:
        self.values = values
        self.calls: list[object] = []
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(100_000, 8_000, True, frozenset({"maximum"}))
    def complete(self, request: object) -> InferenceResponse:
        self.calls.append(request)
        value = self.values.pop(0)
        if isinstance(value, BaseException): raise value
        return InferenceResponse("safe", value, InferenceUsage(1, 1))  # type: ignore[arg-type]


class TestFindingValidation(unittest.TestCase):
    def test_invalid_candidates_do_not_consume_finding_limit(self) -> None:
        from tools.qykw.review import validate_findings
        invalid = tuple(candidate(path=f"bad/{i}.py") for i in range(20))
        findings = validate_findings(invalid + (candidate(),), commentable_lines=plan().commentable_lines, max_findings=1)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].path, "src/a.py")

    def test_rejects_bool_line_missing_extra_and_vague_security_theater(self) -> None:
        from tools.qykw.review import parse_candidates, validate_findings
        raw = {"candidates": [dict(candidate_value(candidate()), line=True), {"path": "src/a.py"}]}
        self.assertEqual(parse_candidates(raw), ())
        vague = candidate(failure_path="security risk", impact="bad", evidence="might be unsafe", suggestion="harden", verification="review")
        self.assertEqual(validate_findings((vague,), commentable_lines=plan().commentable_lines, max_findings=20), ())

    def test_p0_requires_catastrophic_concrete_reproducible_path(self) -> None:
        from tools.qykw.review import validate_findings
        vague = candidate(severity=Severity.P0, failure_path="could fail", impact="serious", evidence="maybe", suggestion="fix", verification="test")
        concrete = candidate(severity=Severity.P0, failure_path="unauthenticated public delete endpoint deletes every tenant record", impact="irrecoverable data loss for every tenant", evidence="POST /delete bypasses authorization in changed handler", suggestion="require tenant authorization", verification="POST as anonymous then verify a second tenant remains")
        self.assertEqual(validate_findings((vague,), commentable_lines=plan().commentable_lines, max_findings=20), ())
        self.assertEqual(validate_findings((concrete,), commentable_lines=plan().commentable_lines, max_findings=20)[0].severity, Severity.P0)

    def test_dedupes_before_cap_with_stable_path_line_side_failure_order(self) -> None:
        from tools.qykw.review import validate_findings
        duplicate = candidate()
        left = candidate(line=2, side=DiffSide.LEFT, failure_path="old behavior breaks retry")
        other = candidate(path="src/b.py", line=9, severity=Severity.P1, failure_path="production request deterministically rejects all authenticated users", impact="primary login flow fails in production", evidence="changed condition always returns 403", suggestion="invert condition", verification="login integration test")
        findings = validate_findings((other, duplicate, duplicate, left), commentable_lines=plan().commentable_lines, max_findings=20)
        self.assertEqual([(item.path, item.line, item.side) for item in findings], [("src/b.py", 9, DiffSide.RIGHT), ("src/a.py", 2, DiffSide.LEFT), ("src/a.py", 3, DiffSide.RIGHT)])
        self.assertEqual(len({item.fingerprint for item in findings}), 3)


class TestReviewEngine(unittest.TestCase):
    def test_staged_provider_sequence_and_validation_can_reject_candidates(self) -> None:
        from tools.qykw.review import ReviewEngine
        rejected = candidate_value(candidate())
        accepted = candidate_value(candidate(path="src/b.py", line=9, severity=Severity.P1,
            failure_path="production request deterministically rejects all authenticated users", impact="primary login flow fails in production", evidence="changed condition always returns 403", suggestion="invert condition", verification="login integration test"))
        provider = RecordingProvider([
            {"candidates": []}, {"candidates": [rejected]}, {"candidates": [accepted]},
            {"conclusion": "validated", "findings": [accepted], "validation_notes": ["counterexample checked"], "limitations": []},
        ])
        result = ReviewEngine(provider, max_findings=20).review(run(), snapshot(), plan())
        self.assertEqual([call.stage.value for call in provider.calls], ["analyzing", "analyzing", "analyzing", "validating"])
        self.assertEqual([call.schema_name.rsplit("-", 1)[-1] for call in provider.calls], ["triage", "review", "review", "validation"])
        self.assertEqual([(item.path, item.line) for item in result.findings], [("src/b.py", 9)])
        self.assertEqual(result.coverage, plan().coverage)
        self.assertIn("counterexample checked", result.validation_notes)

    def test_engine_fails_closed_for_raw_empty_and_cross_run_context(self) -> None:
        from tools.qykw.review import ReviewEngine
        for values in (({"candidates": []}, "raw"), ({"candidates": []}, {"candidates": []}, {"candidates": []}, {"conclusion": "x", "findings": [], "validation_notes": [], "limitations": [], "extra": 1})):
            with self.subTest(values=values):
                result = ReviewEngine(RecordingProvider(list(values)), max_findings=20).review(run(), snapshot(), plan())
                self.assertEqual(result.findings, ())
                self.assertEqual(result.conclusion, "审查未完成")
        wrong = ContextPlan("other/repo", 7, "h" * 40, "wrong", plan().manifest, plan().chunks, plan().coverage, plan().commentable_lines, 1000, 20_000)
        with self.assertRaises(ValueError): ReviewEngine(RecordingProvider([]), max_findings=20).review(run(), snapshot(), wrong)

    def test_malformed_triage_stops_before_more_provider_calls(self) -> None:
        from tools.qykw.review import ReviewEngine
        provider = RecordingProvider(["raw response", {"candidates": []}, {"candidates": []},
                                      {"conclusion": "safe", "findings": [], "validation_notes": [], "limitations": []}])
        result = ReviewEngine(provider, max_findings=20).review(run(), snapshot(), plan())
        self.assertEqual(result.conclusion, "审查未完成")
        self.assertEqual(len(provider.calls), 1)

    def test_success_has_deterministic_validation_and_limitation_notes(self) -> None:
        from tools.qykw.review import ReviewEngine
        provider = RecordingProvider([
            {"candidates": []}, {"candidates": []}, {"candidates": []},
            {"conclusion": "未发现", "findings": [], "validation_notes": [], "limitations": []},
        ])
        result = ReviewEngine(provider, max_findings=20).review(run(), snapshot(), plan())
        self.assertTrue(result.validation_notes)
        self.assertTrue(result.limitations)


if __name__ == "__main__":
    unittest.main()
