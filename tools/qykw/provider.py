"""Transport-independent qykw inference provider contracts."""

from __future__ import annotations

import json
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
    required_context_window = request.max_output_tokens + estimate_request_input_tokens(request)
    supports_request = (
        request.reasoning_profile == "maximum"
        and "maximum" in capabilities.supported_reasoning_profiles
        and capabilities.structured_output
        and capabilities.context_window >= required_context_window
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


def estimate_request_input_tokens(request: InferenceRequest) -> int:
    """Conservatively bound input tokens by UTF-8 bytes in complete request JSON.

    A byte is an upper bound for token count for byte-oriented tokenizers.  This
    deliberately overestimates instead of accepting an unknown or zero input
    budget when the shared request contract has no explicit input estimate.
    The serialized envelope includes the payload, strict schema, and all
    request metadata a transport needs to preserve.
    """

    envelope = {
        "run_id": request.run_id,
        "stage": request.stage.value,
        "prompt_version": request.prompt_version,
        "reasoning_profile": request.reasoning_profile,
        "deadline_seconds": request.deadline_seconds,
        "max_output_tokens": request.max_output_tokens,
        "idempotency_key": request.idempotency_key,
        "schema_name": request.schema_name,
        "schema": request.schema,
        "payload": request.payload,
    }
    serialized = json.dumps(
        envelope,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return max(1, len(serialized.encode("utf-8")))
