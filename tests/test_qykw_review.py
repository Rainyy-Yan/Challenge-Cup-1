"""Tests for staged qykw review and deterministic finding validation."""

from __future__ import annotations

from dataclasses import replace
import unittest

from tools.qykw.domain import (
    ChangedLine, CommandMode, CommandName, CommandRequest, ContextChunk, ContextChunkKind, ContextPlan,
    CoverageReport, DiffSide, FileManifest, FindingCandidate, InferenceResponse,
    InferenceUsage, ProviderCapabilities, PullSnapshot, RepositoryFile, RunContext, Severity,
)


def run() -> RunContext:
    return RunContext("QY-PR7-A1B2", "event-7", 7, "owner/repo", 7, "pull_request", "opened", "fork/repo", "h" * 40, "b" * 40, "main", CommandRequest(CommandName.REVIEW, "", CommandMode.READ_ONLY), "alice")


def snapshot() -> PullSnapshot:
    return PullSnapshot(7, "open", False, "fork/repo", "h" * 40, "owner/repo", "b" * 40, "main", "title", "body", (), (), (), ())


def plan() -> ContextPlan:
    coverage = CoverageReport(2, 2, 2, 2, (), True)
    identity = f"run_id={run().run_id}|repository_id=7|repository=owner/repo|pr=7|base_sha={'b' * 40}|base_ref=main|head_sha={'h' * 40}"
    prefix = f"P run={run().run_id} rid=7 repo=owner/repo pr=7 bs={'b' * 40} br=main hs={'h' * 40}"
    return ContextPlan("owner/repo", 7, "h" * 40, identity, FileManifest(("src/a.py", "src/b.py"), ("src/b.py", "src/a.py")),
        (ContextChunk(f"{identity}|chunk=1", ("src/a.py",), f"{prefix} path=src/a.py prev=- side=RIGHT old=- new=3-3\ncontext-a", 2), ContextChunk(f"{identity}|chunk=2", ("src/b.py",), f"{prefix} path=src/b.py prev=- side=RIGHT old=- new=9-9\ncontext-b", 2)), coverage,
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


def review_candidate_value(item: FindingCandidate, chunk_id: str) -> dict[str, object]:
    return dict(candidate_value(item), source_chunk_id=chunk_id)


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
    def test_snapshot_bound_reference_chunks_are_not_candidate_sources(self) -> None:
        from tools.qykw.review import ReviewEngine

        rule = RepositoryFile("AGENTS.md", "b" * 40, "rule-sha", "review rules", "rules")
        bound_snapshot = replace(snapshot(), trusted_rules=(rule,))
        base_plan = plan()
        prefix = (
            f"P run={run().run_id} rid=7 repo=owner/repo pr=7 bs={'b' * 40} "
            f"br=main hs={'h' * 40}"
        )
        reference = ContextChunk(
            f"{base_plan.run_id}|chunk=3",
            ("AGENTS.md",),
            f"{prefix} path=AGENTS.md prev=- side=REFERENCE old=- new=1-1\n"
            "REFERENCE purpose=rules\nreview rules",
            3,
            ContextChunkKind.REFERENCE,
        )
        bound_plan = replace(
            base_plan,
            manifest=FileManifest(
                base_plan.manifest.paths + ("AGENTS.md",),
                base_plan.manifest.risk_order + ("AGENTS.md",),
            ),
            chunks=base_plan.chunks + (reference,),
            commentable_lines=base_plan.commentable_lines | frozenset({
                ChangedLine("AGENTS.md", 1, DiffSide.RIGHT)
            }),
        )
        provider = RecordingProvider([{"candidates": []}] * 2)

        result = ReviewEngine(provider, max_findings=20).review(run(), bound_snapshot, bound_plan)

        self.assertEqual(result.conclusion, "审查完成")
        self.assertEqual(len(provider.calls), 2)
        self.assertNotIn(reference.chunk_id, [
            request.payload["untrusted"]["context"]["chunk_id"] for request in provider.calls
        ])
        for request in provider.calls:
            self.assertEqual(request.payload["trusted"]["trusted_rules"][0]["path"], "AGENTS.md")

    def test_reference_chunk_not_bound_to_snapshot_is_rejected(self) -> None:
        from tools.qykw.review import ReviewEngine

        base_plan = plan()
        foreign = replace(base_plan.chunks[0], paths=("foreign.md",))
        with self.assertRaisesRegex(ValueError, "invalid_plan_chunk_binding"):
            ReviewEngine(RecordingProvider([]), max_findings=20).review(
                run(), snapshot(), replace(base_plan, chunks=(foreign,))
            )
        disguised = replace(
            base_plan.chunks[0],
            text=base_plan.chunks[0].text.replace(" side=RIGHT ", " side=REFERENCE ")
            + "\ncontent mentions side=RIGHT but cannot change its kind",
        )
        with self.assertRaisesRegex(ValueError, "invalid_plan_chunk_binding"):
            ReviewEngine(RecordingProvider([]), max_findings=20).review(
                run(), snapshot(), replace(base_plan, chunks=(disguised,))
            )

    def test_provider_capabilities_failure_propagates_without_public_result(self) -> None:
        from tools.qykw.review import ReviewEngine
        failure = RuntimeError("raw capability failure must stay internal")

        class FailingCapabilitiesProvider(RecordingProvider):
            def capabilities(self) -> ProviderCapabilities:
                raise failure

        provider = FailingCapabilitiesProvider([])
        with self.assertRaises(RuntimeError) as raised:
            ReviewEngine(provider, max_findings=20).review(run(), snapshot(), plan())
        self.assertIs(raised.exception, failure)
        self.assertEqual(provider.calls, [])

    def test_provider_complete_failure_propagates_without_public_result(self) -> None:
        from tools.qykw.review import ReviewEngine
        failure = RuntimeError("raw completion failure must stay internal")
        provider = RecordingProvider([failure])

        with self.assertRaises(RuntimeError) as raised:
            ReviewEngine(provider, max_findings=20).review(run(), snapshot(), plan())
        self.assertIs(raised.exception, failure)
        self.assertEqual(len(provider.calls), 1)

    def test_staged_provider_sequence_and_validation_can_reject_candidates(self) -> None:
        from tools.qykw.review import ReviewEngine
        rejected = candidate_value(candidate())
        accepted = candidate_value(candidate(path="src/b.py", line=9, severity=Severity.P1,
            failure_path="production request deterministically rejects all authenticated users", impact="primary login flow fails in production", evidence="changed condition always returns 403", suggestion="invert condition", verification="login integration test"))
        provider = RecordingProvider([
            {"candidates": [review_candidate_value(candidate(), plan().chunks[0].chunk_id)]}, {"candidates": [review_candidate_value(candidate(path="src/b.py", line=9, severity=Severity.P1,
            failure_path="production request deterministically rejects all authenticated users", impact="primary login flow fails in production", evidence="changed condition always returns 403", suggestion="invert condition", verification="login integration test"), plan().chunks[1].chunk_id)]},
            {"conclusion": "validated", "findings": [accepted], "validation_notes": ["counterexample checked"], "limitations": []},
        ])
        result = ReviewEngine(provider, max_findings=20).review(run(), snapshot(), plan())
        self.assertEqual([call.stage.value for call in provider.calls], ["analyzing", "analyzing", "validating"])
        self.assertEqual([call.schema_name.rsplit("-", 1)[-1] for call in provider.calls], ["review", "review", "validation"])
        self.assertEqual(
            [call.payload["untrusted"]["context"]["chunk_id"] for call in provider.calls[:2]],
            [chunk.chunk_id for chunk in plan().chunks],
        )
        self.assertEqual([(item.path, item.line) for item in result.findings], [("src/b.py", 9)])
        self.assertEqual(result.coverage, plan().coverage)
        self.assertIn("counterexample checked", result.validation_notes)

    def test_engine_fails_closed_for_raw_empty_and_cross_run_context(self) -> None:
        from tools.qykw.review import ReviewEngine
        valid = review_candidate_value(candidate(), plan().chunks[0].chunk_id)
        for values in (("raw",), ({"candidates": [valid]}, {"candidates": []}, {"conclusion": "x", "findings": [], "validation_notes": [], "limitations": [], "extra": 1})):
            with self.subTest(values=values):
                result = ReviewEngine(RecordingProvider(list(values)), max_findings=20).review(run(), snapshot(), plan())
                self.assertEqual(result.findings, ())
                self.assertEqual(result.conclusion, "审查未完成")
        wrong = ContextPlan("other/repo", 7, "h" * 40, "wrong", plan().manifest, plan().chunks, plan().coverage, plan().commentable_lines, 1000, 20_000)
        with self.assertRaises(ValueError): ReviewEngine(RecordingProvider([]), max_findings=20).review(run(), snapshot(), wrong)

    def test_success_has_deterministic_validation_and_limitation_notes(self) -> None:
        from tools.qykw.review import ReviewEngine
        provider = RecordingProvider([
            {"candidates": []}, {"candidates": []},
        ])
        result = ReviewEngine(provider, max_findings=20).review(run(), snapshot(), plan())
        self.assertEqual(len(provider.calls), 2)
        self.assertTrue(result.validation_notes)
        self.assertTrue(result.limitations)

    def test_wrong_lines_and_sides_never_call_validation(self) -> None:
        from tools.qykw.review import ReviewEngine
        wrong = [review_candidate_value(candidate(line=1000 + index), plan().chunks[0].chunk_id) for index in range(99)]
        wrong.append(review_candidate_value(candidate(side=DiffSide.LEFT), plan().chunks[0].chunk_id))
        provider = RecordingProvider([{"candidates": wrong}, {"candidates": []}])
        result = ReviewEngine(provider, max_findings=20).review(run(), snapshot(), plan())
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(result.findings, ())
        self.assertIn("没有可验证", result.validation_notes[0])

    def test_invalid_candidates_before_and_after_valid_only_send_valid_candidate(self) -> None:
        from tools.qykw.review import ReviewEngine
        wrong_before = [review_candidate_value(candidate(line=1000 + index), plan().chunks[0].chunk_id) for index in range(49)]
        valid = review_candidate_value(candidate(), plan().chunks[0].chunk_id)
        wrong_after = [review_candidate_value(candidate(line=2000 + index), plan().chunks[0].chunk_id) for index in range(50)]
        provider = RecordingProvider([
            {"candidates": wrong_before + [valid] + wrong_after}, {"candidates": []},
            {"conclusion": "validated", "findings": [candidate_value(candidate())], "validation_notes": [], "limitations": []},
        ])
        result = ReviewEngine(provider, max_findings=20).review(run(), snapshot(), plan())
        validation_input = provider.calls[-1].payload["untrusted"]["candidates"]
        self.assertEqual([item["line"] for item in validation_input], [3])
        self.assertEqual([(item.path, item.line) for item in result.findings], [("src/a.py", 3)])

    def test_duplicate_flood_is_deduped_before_validation(self) -> None:
        from tools.qykw.review import ReviewEngine
        duplicate = review_candidate_value(candidate(), plan().chunks[0].chunk_id)
        provider = RecordingProvider([
            {"candidates": [duplicate] * 100}, {"candidates": []},
            {"conclusion": "validated", "findings": [candidate_value(candidate())], "validation_notes": [], "limitations": []},
        ])
        result = ReviewEngine(provider, max_findings=20).review(run(), snapshot(), plan())
        self.assertEqual(len(provider.calls[-1].payload["untrusted"]["candidates"]), 1)
        self.assertEqual(len(result.findings), 1)

    def test_validation_batch_ceiling_uses_stable_severity_and_risk_order(self) -> None:
        from tools.qykw.review import ReviewEngine
        left = candidate(line=2, side=DiffSide.LEFT, failure_path="old behavior breaks retry")
        p1 = candidate(path="src/b.py", line=9, severity=Severity.P1,
            failure_path="production request deterministically rejects all authenticated users", impact="primary login flow fails in production", evidence="changed condition always returns 403", suggestion="invert condition", verification="login integration test")
        provider = RecordingProvider([
            {"candidates": [review_candidate_value(candidate(), plan().chunks[0].chunk_id), review_candidate_value(left, plan().chunks[0].chunk_id)]},
            {"candidates": [review_candidate_value(p1, plan().chunks[1].chunk_id)]},
            {"conclusion": "validated", "findings": [candidate_value(p1)], "validation_notes": [], "limitations": []},
        ])
        result = ReviewEngine(provider, max_findings=1).review(run(), snapshot(), plan())
        validation_input = provider.calls[-1].payload["untrusted"]["candidates"]
        self.assertEqual([(item["path"], item["line"]) for item in validation_input], [("src/b.py", 9), ("src/a.py", 2)])
        self.assertEqual([(item.path, item.line) for item in result.findings], [("src/b.py", 9)])

    def test_only_bound_deep_candidates_reach_validation_and_publication(self) -> None:
        from tools.qykw.review import ReviewEngine
        deep = candidate(path="src/b.py", line=9, severity=Severity.P1,
            failure_path="production request deterministically rejects all authenticated users", impact="primary login flow fails in production", evidence="changed condition always returns 403", suggestion="invert condition", verification="login integration test")
        bound = review_candidate_value(deep, plan().chunks[1].chunk_id)
        provider = RecordingProvider([
            {"candidates": []}, {"candidates": [bound]},
            {"conclusion": "validated", "findings": [candidate_value(deep)], "validation_notes": [], "limitations": []},
        ])
        result = ReviewEngine(provider, max_findings=20).review(run(), snapshot(), plan())
        validation_input = provider.calls[-1].payload["untrusted"]["candidates"]
        self.assertEqual([item["source_chunk_id"] for item in validation_input], [plan().chunks[1].chunk_id])
        self.assertEqual([(item.path, item.line) for item in result.findings], [("src/b.py", 9)])

    def test_candidate_for_another_chunk_is_rejected_before_validation(self) -> None:
        from tools.qykw.review import ReviewEngine
        guessed = review_candidate_value(candidate(path="src/b.py", line=9, severity=Severity.P1,
            failure_path="production request deterministically rejects all authenticated users", impact="primary login flow fails in production", evidence="changed condition always returns 403", suggestion="invert condition", verification="login integration test"), plan().chunks[0].chunk_id)
        provider = RecordingProvider([{"candidates": [guessed]}, {"candidates": []},
                                      {"conclusion": "validated", "findings": [], "validation_notes": [], "limitations": []}])
        result = ReviewEngine(provider, max_findings=20).review(run(), snapshot(), plan())
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(result.conclusion, "审查未完成")
        self.assertEqual(result.findings, ())


if __name__ == "__main__":
    unittest.main()
