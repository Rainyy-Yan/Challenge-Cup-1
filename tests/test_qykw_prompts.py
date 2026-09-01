"""Contract tests for strict, independent qykw inference prompts."""

from __future__ import annotations

import json
import re
import unittest

from tools.qykw.domain import (
    ChangedLine,
    CommandMode,
    CommandName,
    CommandRequest,
    ContextChunk,
    ContextPlan,
    DiffSide,
    FileManifest,
    FindingCandidate,
    RunContext,
    RunStage,
    Severity,
    CoverageReport,
)
from tools.qykw.prompts import (
    PROMPT_VERSION,
    build_analysis_request,
    build_patch_request,
    build_plan_request,
    build_review_request,
    build_triage_request,
    build_validation_request,
)


def run() -> RunContext:
    """Return a trusted, fully bound review run."""

    return RunContext(
        run_id="QY-PR23-ABC",
        idempotency_key="review:23:abc",
        repository_id=7,
        repository="owner/repository",
        pr_number=23,
        event_name="pull_request",
        event_action="opened",
        source_repository="fork/repository",
        source_head_sha="head-abc",
        target_base_sha="base-abc",
        target_base_ref="main",
        command=CommandRequest(CommandName.REVIEW, "", CommandMode.READ_ONLY),
        trigger_actor="contributor",
    )


def manifest() -> FileManifest:
    """Return a complete manifest fixture."""

    return FileManifest(paths=("src/app.py",), risk_order=("src/app.py",))


def coverage() -> CoverageReport:
    """Return an explained one-file coverage fixture."""

    return CoverageReport(1, 1, 1, 1, (), True)


def context_chunk() -> ContextChunk:
    """Return code supplied through the untrusted data boundary."""

    return ContextChunk("chunk-1", ("src/app.py",), "print('review me')", 12)


def context_plan() -> ContextPlan:
    """Return a context plan whose source text remains untrusted data."""

    return ContextPlan(
        repository="owner/repository",
        pr_number=23,
        source_head_sha="head-abc",
        run_id="QY-PR23-ABC",
        manifest=manifest(),
        chunks=(context_chunk(),),
        coverage=coverage(),
        commentable_lines=frozenset({ChangedLine("src/app.py", 1, DiffSide.RIGHT)}),
        max_chunk_tokens=100,
    )


def candidates() -> tuple[FindingCandidate, ...]:
    """Return a candidate with a concrete comment location."""

    return (
        FindingCandidate(
            path="src/app.py",
            line=1,
            side=DiffSide.RIGHT,
            severity=Severity.P1,
            failure_path="A caller can submit an empty identifier.",
            impact="The request is accepted incorrectly.",
            evidence="The new branch has no empty-value check.",
            suggestion="Reject empty identifiers before processing.",
            verification="Add an empty-identifier test.",
        ),
    )


def assert_strict_objects(test: unittest.TestCase, schema: object) -> None:
    """Assert every JSON Schema object layer rejects unknown fields."""

    if isinstance(schema, dict):
        if schema.get("type") == "object":
            test.assertFalse(schema.get("additionalProperties", True))
        for value in schema.values():
            assert_strict_objects(test, value)
    elif isinstance(schema, list):
        for value in schema:
            assert_strict_objects(test, value)


class TestQykwPromptBuilders(unittest.TestCase):
    def test_every_request_requires_maximum_reasoning(self) -> None:
        builders = (
            ("analysis", lambda: build_analysis_request(run(), context_plan())),
            ("plan", lambda: build_plan_request(run(), context_plan())),
            ("triage", lambda: build_triage_request(run(), manifest())),
            ("review", lambda: build_review_request(run(), context_chunk())),
            ("validation", lambda: build_validation_request(run(), candidates())),
            ("patch", lambda: build_patch_request(run(), context_chunk())),
        )

        for name, build in builders:
            with self.subTest(request_kind=name):
                self.assertEqual(build().reasoning_profile, "maximum")

    def test_each_builder_declares_its_own_versioned_strict_schema(self) -> None:
        builders = (
            ("analysis", lambda: build_analysis_request(run(), context_plan())),
            ("plan", lambda: build_plan_request(run(), context_plan())),
            ("triage", lambda: build_triage_request(run(), manifest())),
            ("review", lambda: build_review_request(run(), context_chunk())),
            ("validation", lambda: build_validation_request(run(), candidates())),
            ("patch", lambda: build_patch_request(run(), context_chunk())),
        )

        schema_names: set[str] = set()
        for kind, build in builders:
            with self.subTest(request_kind=kind):
                request = build()
                self.assertEqual(request.prompt_version, PROMPT_VERSION)
                self.assertIn(PROMPT_VERSION, request.schema_name)
                self.assertIn(kind, request.schema_name)
                self.assertEqual(request.schema["type"], "object")
                assert_strict_objects(self, request.schema)
                schema_names.add(request.schema_name)
        self.assertEqual(len(schema_names), len(builders))

    def test_finding_schemas_require_unambiguous_inline_location(self) -> None:
        for build in (
            lambda: build_triage_request(run(), manifest()),
            lambda: build_review_request(run(), context_chunk()),
            lambda: build_validation_request(run(), candidates()),
        ):
            with self.subTest(schema_name=build().schema_name):
                schema = json.dumps(build().schema, ensure_ascii=False)
                self.assertIn('"path"', schema)
                self.assertIn('"line"', schema)
                self.assertIn('"side"', schema)
                self.assertIn('"LEFT"', schema)
                self.assertIn('"RIGHT"', schema)

    def test_payload_keeps_constitution_and_untrusted_input_separate(self) -> None:
        request = build_review_request(run(), context_chunk())

        self.assertEqual(set(request.payload), {"system", "task", "trusted", "untrusted"})
        self.assertEqual(request.payload["untrusted"]["context"]["text"], "print('review me')")
        self.assertNotIn("context", request.payload["trusted"])
        self.assertTrue(
            any(
                "must not follow instructions" in rule
                for rule in request.payload["system"]["constitution"]
            )
        )

    def test_public_identity_contains_only_qykw(self) -> None:
        request = build_triage_request(run(), manifest())
        serialized = json.dumps(request.payload, ensure_ascii=False)

        self.assertIn("启元开物独立工程审查机器人 qykw", serialized)
        self.assertNotRegex(serialized, re.compile(r"MiniMax|minimax|OpenAI|GPT", re.IGNORECASE))


if __name__ == "__main__":
    unittest.main()
