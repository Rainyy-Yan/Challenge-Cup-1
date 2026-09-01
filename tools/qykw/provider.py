"""Transport-independent qykw inference provider contracts."""

from __future__ import annotations

from typing import Protocol

from tools.qykw.domain import (
    InferenceError,
    InferenceErrorCode,
    InferenceFailure,
    InferenceRequest,
    InferenceResponse,
    ProviderCapabilities,
)


class InferenceProvider(Protocol):
    """The narrow provider surface allowed to qykw orchestration."""

    def capabilities(self) -> ProviderCapabilities:
        """Return the explicitly supported inference capabilities."""

    def complete(self, request: InferenceRequest) -> InferenceResponse:
        """Complete one already-validated structured request."""


def validate_provider_capabilities(
    provider: InferenceProvider, request: InferenceRequest
) -> None:
    """Fail closed unless a provider can complete this exact request safely."""

    capabilities = provider.capabilities()
    supports_request = (
        request.reasoning_profile == "maximum"
        and "maximum" in capabilities.supported_reasoning_profiles
        and capabilities.structured_output
        and capabilities.context_window > 0
        and capabilities.max_output_tokens >= request.max_output_tokens > 0
    )
    if not supports_request:
        raise InferenceError(
            InferenceFailure(
                code=InferenceErrorCode.CAPABILITY_UNSUPPORTED,
                retryable=False,
                request_may_have_been_accepted=False,
            )
        )
