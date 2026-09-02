"""Tests for qykw's deterministic and read-only advisory replies."""

from __future__ import annotations

from dataclasses import replace
import unittest

from tools.qykw.domain import (
    AdvisoryResult,
    CommandMode,
    CommandName,
    CommandRequest,
    ContextChunk,
    ContextPlan,
    CoverageReport,
    FileManifest,
    InferenceResponse,
    InferenceUsage,
    ProviderCapabilities,
    RunContext,
    RunRecord,
    RunStage,
    RunStatus,
)


class RecordingProvider:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[object] = []

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(100_000, 20_000, True, frozenset({"maximum"}))

    def complete(self, request: object) -> InferenceResponse:
        self.calls.append(request)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response  # type: ignore[return-value]


def run(name: CommandName) -> RunContext:
    return RunContext(
        run_id="QY-PR7-A1B2", idempotency_key="event-7", repository_id=7,
        repository="owner/repo", pr_number=7, event_name="issue_comment",
        event_action="created", source_repository="fork/repo", source_head_sha="h" * 40,
        target_base_sha="b" * 40, target_base_ref="main",
        command=CommandRequest(name, "", CommandMode.READ_ONLY), trigger_actor="alice",
    )


def plan() -> ContextPlan:
    coverage = CoverageReport(1, 1, 1, 1, (), True)
    return ContextPlan(
        repository="owner/repo", pr_number=7, source_head_sha="h" * 40,
        run_id="QY-PR7-A1B2", manifest=FileManifest(("src/a.py",), ("src/a.py",)),
        chunks=(ContextChunk("chunk-1", ("src/a.py",), "+x\n", 2),), coverage=coverage,
        commentable_lines=frozenset(), max_chunk_tokens=1000,
        effective_input_budget_tokens=20_000,
    )


def response(value: object) -> InferenceResponse:
    return InferenceResponse("safe-id", value, InferenceUsage(1, 1))  # type: ignore[arg-type]


class TestAdvisoryService(unittest.TestCase):
    def test_deterministic_commands_have_zero_provider_calls(self) -> None:
        from tools.qykw.advisory import AdvisoryService

        provider = RecordingProvider([])
        service = AdvisoryService(provider)
        record = RunRecord(run(CommandName.STATUS), RunStage.COMPLETED, RunStatus.COMPLETED,
                           "v1", None, True, plan().coverage, (), None, "now", "now")
        for name in (CommandName.HELP, CommandName.STATUS, CommandName.SUMMARY, CommandName.STOP):
            result = service.handle(run(name), plan(), record)
            self.assertIsInstance(result, AdvisoryResult)
        self.assertEqual(provider.calls, [])

    def test_deterministic_record_mismatch_is_safe_and_never_calls_provider(self) -> None:
        from tools.qykw.advisory import AdvisoryService

        provider = RecordingProvider([])
        result = AdvisoryService(provider).handle(run(CommandName.STATUS), None, None)
        self.assertIn("不可用", result.body)
        self.assertEqual(provider.calls, [])

    def test_analysis_and_plan_use_distinct_maximum_readonly_requests(self) -> None:
        from tools.qykw.advisory import AdvisoryService

        values = [{"advisory": {"title": "分析", "body": "只读结论", "evidence": ["证据"], "limitations": []}},
                  {"advisory": {"title": "计划", "body": "只读步骤", "evidence": [], "limitations": ["限制"]}}]
        provider = RecordingProvider([response(value) for value in values])
        service = AdvisoryService(provider)
        analysis = service.handle(run(CommandName.ANALYZE), plan())
        review_plan = service.handle(run(CommandName.PLAN), plan())
        self.assertEqual((analysis.title, review_plan.title), ("分析", "计划"))
        self.assertEqual([item.schema_name.rsplit("-", 1)[-1] for item in provider.calls], ["analysis", "plan"])
        self.assertTrue(all(item.reasoning_profile == "maximum" for item in provider.calls))
        self.assertTrue(all(item.stage is RunStage.ANALYZING for item in provider.calls))
        self.assertTrue(all("patch" not in repr(item.payload).lower() for item in provider.calls))

    def test_malformed_raw_or_provider_failure_yields_generic_safe_result(self) -> None:
        from tools.qykw.advisory import AdvisoryService

        for value in (response({"advisory": {"title": "x", "body": "y", "evidence": [], "limitations": [], "extra": "no"}}),
                      response("raw response block"), RuntimeError("provider secret and raw prompt")):
            with self.subTest(value=type(value).__name__):
                result = AdvisoryService(RecordingProvider([value])).handle(run(CommandName.ANALYZE), plan())
                self.assertEqual(result.evidence, ())
                self.assertNotIn("secret", repr(result))
                self.assertNotIn("prompt", repr(result))

    def test_advisory_parser_rejects_bool_counts_and_overlong_lists(self) -> None:
        from tools.qykw.advisory import parse_advisory_response

        invalid = {"advisory": {"title": "x", "body": "y", "evidence": [True], "limitations": []}}
        self.assertIsNone(parse_advisory_response(invalid))
        self.assertIsNone(parse_advisory_response({"advisory": {"title": "x" * 1000, "body": "y", "evidence": [], "limitations": []}}))


if __name__ == "__main__":
    unittest.main()
