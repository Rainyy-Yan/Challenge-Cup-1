"""Contract tests for the qykw inference provider boundary."""

from __future__ import annotations

import unittest
import time
import threading
from dataclasses import replace
from unittest.mock import patch

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
from tools.qykw.provider import (
    InferenceProvider,
    ProviderError,
    ProviderErrorCode,
    ResponsesInferenceProvider,
    TransportFailure,
    TransportFailureKind,
    TransportResponse,
    estimate_request_input_tokens,
    validate_provider_capabilities,
)


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
    *,
    context_window: int = 32_000,
    max_output_tokens: int = 8_000,
    structured_output: bool = True,
    profiles: frozenset[str] | None = None,
) -> ProviderCapabilities:
    """Return otherwise valid provider capabilities."""

    return ProviderCapabilities(
        context_window=context_window,
        max_output_tokens=max_output_tokens,
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

    def test_capabilities_require_output_and_positive_input_budget(self) -> None:
        inference_request = request()
        input_tokens = estimate_request_input_tokens(inference_request)
        required_window = inference_request.max_output_tokens + input_tokens
        self.assertGreater(input_tokens, 0)

        for window in (
            inference_request.max_output_tokens - 1,
            inference_request.max_output_tokens,
            required_window - 1,
        ):
            with self.subTest(context_window=window):
                provider = RecordingProvider(capabilities(context_window=window))
                with self.assertRaises(InferenceError) as raised:
                    validate_provider_capabilities(provider, inference_request)
                self.assertEqual(
                    raised.exception.failure.code, InferenceErrorCode.CAPABILITY_UNSUPPORTED
                )

        validate_provider_capabilities(
            RecordingProvider(capabilities(context_window=required_window)), inference_request
        )

    def test_schema_growth_requires_more_context_than_payload_boundary(self) -> None:
        inference_request = request()
        payload_boundary = (
            inference_request.max_output_tokens + estimate_request_input_tokens(inference_request)
        )
        expanded_schema = {
            **inference_request.schema,
            "description": "strict schema detail " * 500,
        }
        expanded_request = replace(inference_request, schema=expanded_schema)

        self.assertEqual(expanded_request.payload, inference_request.payload)
        self.assertEqual(expanded_request.max_output_tokens, inference_request.max_output_tokens)
        self.assertGreater(
            estimate_request_input_tokens(expanded_request),
            estimate_request_input_tokens(inference_request),
        )
        with self.assertRaises(InferenceError):
            validate_provider_capabilities(
                RecordingProvider(capabilities(context_window=payload_boundary)), expanded_request
            )

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


class RecordingTransport:
    """Deterministic adapter fixture; it never contacts a network."""

    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls = 0
        self.requests: list[object] = []

    def send(self, transport_request: object) -> TransportResponse:
        self.calls += 1
        self.requests.append(transport_request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, TransportResponse)
        return outcome


def good_response(value: dict[str, object] | None = None) -> TransportResponse:
    import json

    return TransportResponse(
        200,
        {"content-type": "application/json"},
        json.dumps(
            {
                "id": "safe-request-id",
                "output": {"value": value or empty_response_value()},
                "usage": {"input_tokens": 10, "output_tokens": 3},
            }
        ).encode(),
    )


def empty_response_value() -> object:
    return _empty_schema_value(request().schema)


def _empty_schema_value(schema: object) -> object:
    """Produce the smallest valid result for the current strict request schema."""
    assert isinstance(schema, dict)
    if schema["type"] == "object":
        return {
            name: _empty_schema_value(schema["properties"][name])
            for name in schema["required"]
        }
    if schema["type"] == "array":
        return []
    if schema["type"] == "string":
        return "x" * max(1, schema.get("minLength", 0))
    if schema["type"] == "integer":
        return schema.get("minimum", 0)
    raise AssertionError("unsupported test schema")


def secure_provider(
    transport: RecordingTransport,
    *,
    base_url: str = "https://allowed.example/v1/responses",
    allowed_hosts: tuple[str, ...] = ("allowed.example",),
    dns: object | None = None,
    clock: object | None = None,
    sleep: object | None = None,
    timeout_seconds: int = 30,
    resolver_slots: object | None = None,
) -> ResponsesInferenceProvider:
    return ResponsesInferenceProvider(
        api_key="SENTINEL_API_KEY",
        base_url=base_url,
        model="configured-model",
        allowed_hosts=allowed_hosts,
        context_window=100_000,
        max_output_tokens=8_000,
        timeout_seconds=timeout_seconds,
        transport=transport,
        dns_resolver=dns or (lambda _host, _port, _remaining: ("93.184.216.34",)),
        clock=clock or (lambda: 0.0),
        sleep=sleep or (lambda _seconds: None),
        resolver_slots=resolver_slots,
    )


class TestResponsesInferenceProvider(unittest.TestCase):
    def test_supports_bounded_patch_schema_types_without_coercion(self) -> None:
        schema = {
            "title": "patch-v1",
            "type": "object",
            "additionalProperties": False,
            "required": ["flag", "digest", "items"],
            "properties": {
                "flag": {"type": "boolean"},
                "digest": {
                    "anyOf": [
                        {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                        {"type": "null"},
                    ]
                },
                "items": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 2,
                    "items": {"type": "string", "minLength": 1},
                },
            },
        }
        value = {"flag": True, "digest": None, "items": ["x"]}
        provider = secure_provider(RecordingTransport([good_response(value)]))

        response = provider.complete(replace(request(), schema=schema))

        self.assertEqual(response.value, value)

        invalid_values = (
            {"flag": 1, "digest": None, "items": ["x"]},
            {"flag": True, "digest": 12, "items": ["x"]},
            {"flag": True, "digest": "g" * 64, "items": ["x"]},
            {"flag": True, "digest": None, "items": []},
            {"flag": True, "digest": None, "items": ["x", "y", "z"]},
        )
        for invalid in invalid_values:
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ProviderError, "invalid_response"):
                    secure_provider(
                        RecordingTransport([good_response(invalid)])
                    ).complete(replace(request(), schema=schema))

    def test_rejects_unbounded_ambiguous_or_oversized_schema_extensions(self) -> None:
        base = {
            "title": "invalid-v1",
            "type": "object",
            "additionalProperties": False,
            "required": ["value"],
            "properties": {"value": {"type": "string"}},
        }
        invalid_children = (
            {"type": "string", "pattern": "["},
            {"anyOf": [{"type": "string"}, {"type": "string"}]},
            {"anyOf": [{"type": "string"}]},
            {
                "type": "array",
                "minItems": 2,
                "maxItems": 1,
                "items": {"type": "string"},
            },
            {
                "type": "array",
                "maxItems": 10001,
                "items": {"type": "string"},
            },
        )
        for child in invalid_children:
            schema = {
                **base,
                "properties": {"value": child},
            }
            transport = RecordingTransport([good_response()])
            with self.subTest(child=child), self.assertRaisesRegex(
                ProviderError, "invalid_config"
            ):
                secure_provider(transport).complete(replace(request(), schema=schema))
            self.assertEqual(transport.calls, 0)

        deep: dict[str, object] = {"type": "string"}
        for _ in range(18):
            deep = {"type": "array", "items": deep}
        wide_properties = {
            f"field_{index}": {"type": "string"} for index in range(1_001)
        }
        oversized_schemas = (
            {**base, "properties": {"value": deep}},
            {
                "title": "wide-v1",
                "type": "object",
                "additionalProperties": False,
                "required": list(wide_properties),
                "properties": wide_properties,
            },
        )
        for schema in oversized_schemas:
            transport = RecordingTransport([good_response()])
            with self.subTest(schema_title=schema["title"]), self.assertRaisesRegex(
                ProviderError, "invalid_config"
            ):
                secure_provider(transport).complete(replace(request(), schema=schema))
            self.assertEqual(transport.calls, 0)

    def test_rejects_insecure_endpoint_before_transport(self) -> None:
        transport = RecordingTransport([good_response()])
        provider = secure_provider(transport, base_url="http://allowed.example/v1")

        with self.assertRaisesRegex(ProviderError, "endpoint_invalid"):
            provider.complete(request())
        self.assertEqual(transport.calls, 0)

    def test_rejects_userinfo_query_fragment_and_nondefault_port(self) -> None:
        for url in (
            "https://user@allowed.example/v1",
            "https://allowed.example/v1?token=SENTINEL_API_KEY",
            "https://allowed.example/v1#fragment",
            "https://allowed.example:444/v1",
        ):
            with self.subTest(url=url):
                transport = RecordingTransport([good_response()])
                with self.assertRaisesRegex(ProviderError, "endpoint_invalid"):
                    secure_provider(transport, base_url=url).complete(request())
                self.assertEqual(transport.calls, 0)

    def test_rejects_private_literal_and_private_dns_before_transport(self) -> None:
        cases = (
            ("https://127.0.0.1/v1", lambda _h, _p, _r: ("127.0.0.1",)),
            ("https://allowed.example/v1", lambda _h, _p, _r: ("10.0.0.2",)),
            ("https://allowed.example/v1", lambda _h, _p, _r: ("::1",)),
        )
        for url, dns in cases:
            with self.subTest(url=url):
                transport = RecordingTransport([good_response()])
                with self.assertRaisesRegex(ProviderError, "endpoint_blocked"):
                    secure_provider(transport, base_url=url, dns=dns).complete(request())
                self.assertEqual(transport.calls, 0)

    def test_rejects_special_use_ip_literals_and_dns_results_before_transport(self) -> None:
        for literal in ("224.0.0.1", "255.255.255.255", "fe00::1"):
            with self.subTest(literal=literal):
                transport = RecordingTransport([good_response()])
                with self.assertRaisesRegex(ProviderError, "endpoint_blocked"):
                    secure_provider(transport, base_url=f"https://[{literal}]/v1" if ":" in literal else f"https://{literal}/v1").complete(request())
                self.assertEqual(transport.calls, 0)

        for resolved in ("224.0.0.1", "255.255.255.255", "fe00::1", "fec0::1", "192.0.2.1"):
            with self.subTest(resolved=resolved):
                transport = RecordingTransport([good_response()])
                with self.assertRaisesRegex(ProviderError, "endpoint_blocked"):
                    secure_provider(transport, dns=lambda _host, _port, _remaining: (resolved,)).complete(request())
                self.assertEqual(transport.calls, 0)

    def test_malformed_schema_is_rejected_before_resolver_or_transport(self) -> None:
        transport = RecordingTransport([good_response()])
        dns_calls: list[object] = []
        provider = secure_provider(
            transport,
            dns=lambda host, port, _remaining: (dns_calls.append((host, port)) or ("93.184.216.34",)),
        )
        for schema in (
            {"type": "object", "properties": {}, "required": []},
            {"type": "string", "minLength": 1},
        ):
            with self.subTest(schema=schema), self.assertRaisesRegex(ProviderError, "invalid_config"):
                provider.complete(replace(request(), schema=schema))
        self.assertEqual(dns_calls, [])
        self.assertEqual(transport.calls, 0)

    def test_exact_canonical_allowed_host_blocks_suffix_and_idna_spoofing(self) -> None:
        for url in (
            "https://allowed.example.attacker.test/v1",
            "https://\u0430llowed.example/v1",
        ):
            with self.subTest(url=url):
                with self.assertRaisesRegex(ProviderError, "endpoint_(not_allowed|invalid)"):
                    secure_provider(RecordingTransport([good_response()]), base_url=url).complete(request())
        canonical = secure_provider(RecordingTransport([good_response()]), base_url="https://allowed.example./v1").complete(request())
        self.assertEqual(canonical.value, empty_response_value())

    def test_capabilities_are_checked_before_dns_or_transport(self) -> None:
        transport = RecordingTransport([good_response()])
        dns_calls: list[object] = []
        provider = secure_provider(
            transport,
            dns=lambda host, port, _remaining: (dns_calls.append((host, port)) or ("93.184.216.34",)),
        )

        with self.assertRaises(InferenceError):
            provider.complete(replace(request(), reasoning_profile="high"))
        self.assertEqual(dns_calls, [])
        self.assertEqual(transport.calls, 0)

    def test_cross_origin_redirect_is_rejected(self) -> None:
        transport = RecordingTransport(
            [TransportResponse(302, {"location": "https://other.example/path"}, b"")]
        )
        with self.assertRaisesRegex(ProviderError, "endpoint_redirect_rejected"):
            secure_provider(transport).complete(request())
        self.assertEqual(transport.calls, 1)

    def test_read_timeout_is_not_retried(self) -> None:
        transport = RecordingTransport(
            [TransportFailure(TransportFailureKind.READ_TIMEOUT)]
        )
        with self.assertRaisesRegex(ProviderError, "read_timeout"):
            secure_provider(transport).complete(request())
        self.assertEqual(transport.calls, 1)

    def test_dns_tls_and_confirmed_presend_failure_are_retried_once(self) -> None:
        for failure in (
            TransportFailure(TransportFailureKind.DNS),
            TransportFailure(TransportFailureKind.TLS_HANDSHAKE),
            TransportFailure(TransportFailureKind.CONNECTION, pre_send=True),
        ):
            with self.subTest(failure=failure.kind):
                transport = RecordingTransport([failure, good_response()])
                sleeps: list[float] = []
                response = secure_provider(transport, sleep=sleeps.append).complete(request())
                self.assertEqual(response.value, empty_response_value())
                self.assertEqual(transport.calls, 2)
                self.assertEqual(sleeps, [0.1])

    def test_transport_timeout_is_capped_to_remaining_absolute_deadline(self) -> None:
        now = [0.0]

        class AdvancingTransport:
            def __init__(self) -> None:
                self.calls = 0
                self.requests: list[object] = []

            def send(self, transport_request: object) -> TransportResponse:
                self.calls += 1
                self.requests.append(transport_request)
                if self.calls == 1:
                    now[0] = 0.4
                    raise TransportFailure(TransportFailureKind.TLS_HANDSHAKE)
                return good_response()

        transport = AdvancingTransport()
        result = secure_provider(
            transport,
            timeout_seconds=3600,
            clock=lambda: now[0],
            sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
        ).complete(replace(request(), deadline_seconds=1))

        self.assertEqual(result.value, empty_response_value())
        self.assertEqual([item.timeout_seconds for item in transport.requests], [1.0, 0.5])

    def test_stalled_resolver_is_deadline_bounded_without_transport(self) -> None:
        def stalled_resolver(_host: str, _port: int, _remaining: float) -> tuple[str, ...]:
            time.sleep(10)
            return ("93.184.216.34",)

        transport = RecordingTransport([good_response()])
        started = time.monotonic()
        with self.assertRaisesRegex(ProviderError, "deadline_exceeded"):
            secure_provider(
                transport,
                dns=stalled_resolver,
                clock=time.monotonic,
            ).complete(replace(request(), deadline_seconds=0.05))
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(transport.calls, 0)

    def test_resolver_capacity_is_bounded_and_slots_recover_after_worker_exit(self) -> None:
        slots = threading.BoundedSemaphore(1)
        entered = threading.Event()
        release = threading.Event()
        active = [0]
        highest_active = [0]
        active_lock = threading.Lock()

        def blocked_resolver(_host: str, _port: int, _remaining: float) -> tuple[str, ...]:
            with active_lock:
                active[0] += 1
                highest_active[0] = max(highest_active[0], active[0])
            entered.set()
            release.wait()
            with active_lock:
                active[0] -= 1
            return ("93.184.216.34",)

        first_transport = RecordingTransport([good_response()])
        first = secure_provider(first_transport, dns=blocked_resolver, resolver_slots=slots)
        first_error: list[BaseException] = []
        first_thread = threading.Thread(target=lambda: self._complete_in_thread(first, first_error))
        first_thread.start()
        self.assertTrue(entered.wait(1))

        saturated_transport = RecordingTransport([good_response()])
        with self.assertRaisesRegex(ProviderError, "dns_error"):
            secure_provider(saturated_transport, resolver_slots=slots).complete(request())
        self.assertEqual(saturated_transport.calls, 0)
        self.assertEqual(highest_active[0], 1)

        release.set()
        first_thread.join(1)
        self.assertFalse(first_thread.is_alive())
        self.assertEqual(first_error, [])

        recovered_transport = RecordingTransport([good_response()])
        secure_provider(recovered_transport, resolver_slots=slots).complete(request())
        self.assertEqual(recovered_transport.calls, 1)

    @staticmethod
    def _complete_in_thread(provider: ResponsesInferenceProvider, errors: list[BaseException]) -> None:
        try:
            provider.complete(request())
        except BaseException as error:
            errors.append(error)

    def test_resolver_thread_start_failure_releases_slot(self) -> None:
        slots = threading.BoundedSemaphore(1)
        with patch("tools.qykw.provider.threading.Thread", side_effect=RuntimeError("SENTINEL")):
            with self.assertRaisesRegex(ProviderError, "dns_error"):
                secure_provider(RecordingTransport([good_response()]), resolver_slots=slots).complete(request())

        recovered_transport = RecordingTransport([good_response()])
        secure_provider(recovered_transport, resolver_slots=slots).complete(request())
        self.assertEqual(recovered_transport.calls, 1)

    def test_resolver_receives_decreasing_remaining_deadline_on_retry(self) -> None:
        now = [0.0]
        budgets: list[float] = []

        class RetryingTransport:
            def __init__(self) -> None:
                self.calls = 0

            def send(self, _transport_request: object) -> TransportResponse:
                self.calls += 1
                if self.calls == 1:
                    now[0] = 0.4
                    raise TransportFailure(TransportFailureKind.TLS_HANDSHAKE)
                return good_response()

        def resolver(_host: str, _port: int, remaining: float) -> tuple[str, ...]:
            budgets.append(remaining)
            return ("93.184.216.34",)

        secure_provider(
            RetryingTransport(),
            dns=resolver,
            clock=lambda: now[0],
            sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
        ).complete(replace(request(), deadline_seconds=1))
        self.assertEqual(budgets, [1.0, 0.5])

    def test_expired_deadline_does_not_start_a_retry(self) -> None:
        now = [0.0]

        class ExpiringTransport:
            def __init__(self) -> None:
                self.calls = 0

            def send(self, _transport_request: object) -> TransportResponse:
                self.calls += 1
                now[0] = 1.0
                raise TransportFailure(TransportFailureKind.TLS_HANDSHAKE)

        transport = ExpiringTransport()
        with self.assertRaisesRegex(ProviderError, "tls_error"):
            secure_provider(
                transport,
                timeout_seconds=3600,
                clock=lambda: now[0],
                sleep=lambda _seconds: self.fail("expired retry must not sleep"),
            ).complete(replace(request(), deadline_seconds=1))
        self.assertEqual(transport.calls, 1)

    def test_complete_request_envelope_is_serialized_before_dispatch(self) -> None:
        import json

        transport = RecordingTransport([good_response()])
        inference_request = request()
        secure_provider(transport).complete(inference_request)

        wire = json.loads(transport.requests[0].body.decode())
        self.assertEqual(
            {"run_id", "stage", "prompt_version", "deadline_seconds"},
            set(wire) & {"run_id", "stage", "prompt_version", "deadline_seconds"},
        )
        self.assertEqual(wire["run_id"], inference_request.run_id)
        self.assertEqual(wire["stage"], inference_request.stage.value)
        self.assertEqual(wire["prompt_version"], inference_request.prompt_version)
        self.assertEqual(wire["deadline_seconds"], inference_request.deadline_seconds)
        self.assertEqual(wire["reasoning_profile"], inference_request.reasoning_profile)
        self.assertEqual(wire["schema"], inference_request.schema)
        self.assertEqual(
            wire["payload"],
            json.loads(json.dumps(inference_request.payload, ensure_ascii=False)),
        )
        self.assertEqual(wire["max_output_tokens"], inference_request.max_output_tokens)

    def test_certificate_response_and_maybe_accepted_failures_are_not_retried(self) -> None:
        for failure in (
            TransportFailure(TransportFailureKind.CERTIFICATE),
            TransportFailure(TransportFailureKind.RESPONSE_INTERRUPTED),
            TransportFailure(TransportFailureKind.CONNECTION, pre_send=False),
        ):
            with self.subTest(failure=failure.kind):
                transport = RecordingTransport([failure, good_response()])
                with self.assertRaises(ProviderError):
                    secure_provider(transport).complete(request())
                self.assertEqual(transport.calls, 1)

    def test_valid_429_retry_after_retries_only_within_deadline(self) -> None:
        limited = TransportResponse(429, {"retry-after": "2"}, b"")
        transport = RecordingTransport([limited, good_response()])
        sleeps: list[float] = []
        result = secure_provider(transport, sleep=sleeps.append).complete(request())
        self.assertEqual(result.usage.input_tokens, 10)
        self.assertEqual(transport.calls, 2)
        self.assertEqual(sleeps, [2.0])

        late = RecordingTransport([limited, good_response()])
        late_ticks = iter((0.0, 0.0, 899.0, 899.0, 899.0))
        with self.assertRaisesRegex(ProviderError, "rate_limited"):
            secure_provider(late, clock=lambda: next(late_ticks)).complete(request())
        self.assertEqual(late.calls, 1)

    def test_strict_response_rejects_extra_data_body_and_content_type(self) -> None:
        import json

        malformed = TransportResponse(
            200,
            {"content-type": "application/json"},
            json.dumps({"id": "one", "output": {"value": empty_response_value()}, "extra": 1}).encode(),
        )
        with self.assertRaisesRegex(ProviderError, "invalid_response"):
            secure_provider(RecordingTransport([malformed])).complete(request())
        bad_type = TransportResponse(200, {"content-type": "text/plain"}, b"{}")
        with self.assertRaisesRegex(ProviderError, "invalid_response"):
            secure_provider(RecordingTransport([bad_type])).complete(request())

    def test_response_usage_cannot_exceed_request_or_context_limits(self) -> None:
        import json

        for usage in (
            {"input_tokens": 10, "output_tokens": 4097},
            {"input_tokens": -1, "output_tokens": 3},
            {"input_tokens": 100_000, "output_tokens": 1},
        ):
            with self.subTest(usage=usage):
                body = json.dumps(
                    {"id": "safe-request-id", "output": {"value": empty_response_value()}, "usage": usage}
                ).encode()
                response = TransportResponse(200, {"content-type": "application/json"}, body)
                with self.assertRaisesRegex(ProviderError, "invalid_response"):
                    secure_provider(RecordingTransport([response])).complete(request())

    def test_errors_logs_and_chains_never_disclose_secrets_or_prompt_data(self) -> None:
        secret_source = "SOURCE_CODE_SHOULD_NOT_LEAK"
        full_comment = "FULL_COMMENT_SHOULD_NOT_LEAK"
        response_body = "FULL_RESPONSE_SHOULD_NOT_LEAK"
        logged: list[dict[str, object]] = []
        failure = TransportFailure(
            TransportFailureKind.CONNECTION,
            detail=f"{secret_source} {full_comment} {response_body} SENTINEL_API_KEY",
            pre_send=False,
        )
        transport = RecordingTransport([failure])
        provider = secure_provider(transport)
        provider._logger = logged.append
        sensitive_request = replace(request(), payload={"comment": full_comment, "code": secret_source})

        with self.assertRaises(ProviderError) as raised:
            provider.complete(sensitive_request)
        error = raised.exception
        chain = (error, error.__cause__, error.__context__)
        serialized = repr(logged) + "".join(
            repr(item) + str(item) + repr(getattr(item, "args", ())) + repr(getattr(item, "__dict__", {}))
            for item in chain
            if item is not None
        )
        for forbidden in ("SENTINEL_API_KEY", secret_source, full_comment, response_body, "allowed.example"):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(set(logged[0]), {"run_id", "stage", "call_count", "error_code"})

    def test_from_env_requires_every_protected_setting_and_redacts_values(self) -> None:
        env = {
            "QYKW_INFERENCE_API_KEY": "SENTINEL_API_KEY",
            "QYKW_INFERENCE_BASE_URL": "https://allowed.example/v1/responses",
            "QYKW_INFERENCE_MODEL": "configured-model",
            "QYKW_INFERENCE_ALLOWED_HOSTS": "allowed.example",
            "QYKW_INFERENCE_CONTEXT_WINDOW": "100000",
            "QYKW_INFERENCE_MAX_OUTPUT_TOKENS": "8000",
            "QYKW_INFERENCE_TIMEOUT_SECONDS": "30",
        }
        with patch.dict("os.environ", env, clear=True):
            provider = ResponsesInferenceProvider.from_env()
        self.assertEqual(provider.capabilities().context_window, 100000)
        for missing in env:
            incomplete = dict(env)
            raw = incomplete.pop(missing)
            with self.subTest(missing=missing), patch.dict("os.environ", incomplete, clear=True):
                with self.assertRaises(ProviderError) as raised:
                    ResponsesInferenceProvider.from_env()
            self.assertNotIn(raw, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
