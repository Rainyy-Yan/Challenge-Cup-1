"""Contract tests for the qykw inference provider boundary."""

from __future__ import annotations

import unittest

from tools.qykw.domain import (
    CommandMode,
    CommandName,
    CommandRequest,
    FileManifest,
    InferenceError,
    InferenceErrorCode,
    InferenceRequest,
    InferenceResponse,
    InferenceUsage,
    ProviderCapabilities,
    RunContext,
)
from tools.qykw.prompts import build_triage_request
from tools.qykw.provider import InferenceProvider, validate_provider_capabilities


def request() -> InferenceRequest:
    """Return a maximum-reasoning request with a strict output schema."""

    run = RunContext(
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
    return build_triage_request(run, FileManifest(("src/app.py",), ("src/app.py",)))


class RecordingProvider:
    """Small real protocol implementation without a transport."""

    def __init__(self, capabilities: ProviderCapabilities) -> None:
        self._capabilities = capabilities

    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    def complete(self, _request: InferenceRequest) -> InferenceResponse:
        return InferenceResponse(None, {}, InferenceUsage(None, None))


def capabilities(
    *, structured_output: bool = True, profiles: frozenset[str] | None = None
) -> ProviderCapabilities:
    """Return otherwise valid provider capabilities."""

    return ProviderCapabilities(
        context_window=32_000,
        max_output_tokens=8_000,
        structured_output=structured_output,
        supported_reasoning_profiles=profiles or frozenset({"maximum"}),
    )


class TestInferenceProviderBoundary(unittest.TestCase):
    def test_protocol_exposes_only_capabilities_and_completion(self) -> None:
        public_methods = {
            name
            for name, value in InferenceProvider.__dict__.items()
            if callable(value) and not name.startswith("_")
        }

        self.assertEqual(public_methods, {"capabilities", "complete"})

    def test_capabilities_accept_a_maximum_structured_request(self) -> None:
        provider = RecordingProvider(capabilities())

        validate_provider_capabilities(provider, request())

    def test_capabilities_fail_closed_without_maximum_reasoning(self) -> None:
        provider = RecordingProvider(capabilities(profiles=frozenset({"high"})))

        with self.assertRaises(InferenceError) as raised:
            validate_provider_capabilities(provider, request())

        self.assertEqual(raised.exception.failure.code, InferenceErrorCode.CAPABILITY_UNSUPPORTED)
        self.assertFalse(raised.exception.failure.retryable)

    def test_capabilities_fail_closed_without_strict_schema_support(self) -> None:
        provider = RecordingProvider(capabilities(structured_output=False))

        with self.assertRaises(InferenceError) as raised:
            validate_provider_capabilities(provider, request())

        self.assertEqual(raised.exception.failure.code, InferenceErrorCode.CAPABILITY_UNSUPPORTED)
        self.assertFalse(raised.exception.failure.request_may_have_been_accepted)


if __name__ == "__main__":
    unittest.main()
