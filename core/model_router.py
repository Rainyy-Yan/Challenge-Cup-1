from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from threading import Lock
from typing import Callable, Iterable


STRONG_TASKS = frozenset({"make_item"})
AUTH = "auth"
REQUEST = "request"
MODEL_UNAVAILABLE = "model_unavailable"
RATE_LIMIT = "rate_limit"
PROVIDER = "provider"
NETWORK = "network"
INVALID_RESPONSE = "invalid_response"
BREAKER_ERRORS = frozenset({RATE_LIMIT, PROVIDER, NETWORK, INVALID_RESPONSE})


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    role: str
    timeout: int = 60
    supports_json_mode: bool = True
    price_in: float = 0.0
    price_out: float = 0.0


@dataclass(frozen=True)
class CallSample:
    ok: bool
    latency_ms: int


@dataclass
class ModelRuntime:
    state: str = "closed"
    consecutive_failures: int = 0
    opened_at: float | None = None
    half_open_in_flight: bool = False
    samples: deque[CallSample] = field(default_factory=lambda: deque(maxlen=20))
    attempts: int = 0
    successes: int = 0
    failures: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    fallback_in: int = 0
    fallback_out: int = 0
    json_downgrades: int = 0
    last_latency_ms: int = 0
    last_error_type: str = ""
    last_error_status: int | None = None


def _success_rate(state: ModelRuntime) -> float:
    if not state.samples:
        return 1.0
    return sum(1 for sample in state.samples if sample.ok) / len(state.samples)


def _average_latency(state: ModelRuntime) -> float:
    values = [sample.latency_ms for sample in state.samples if sample.ok]
    return sum(values) / len(values) if values else 0.0


def build_default_specs(
    default_model: str,
    strong_model: str,
    timeout: int,
    price_in: float,
    price_out: float,
) -> tuple[ModelSpec, ModelSpec]:
    default_model = default_model.strip()
    strong_model = strong_model.strip()
    if not default_model or not strong_model:
        raise ValueError("模型 ID 不能为空")
    if default_model == strong_model:
        raise ValueError("智能路由需要两个不同的模型")
    return (
        ModelSpec(strong_model, "strong", timeout, False, price_in, price_out),
        ModelSpec(default_model, "default", timeout, False, price_in, price_out),
    )


class SmartModelRouter:
    def __init__(
        self,
        specs: Iterable[ModelSpec],
        *,
        clock: Callable[[], float] = time.monotonic,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0,
    ) -> None:
        items = tuple(specs)
        if len(items) != 2 or len({item.model_id for item in items}) != 2:
            raise ValueError("模型注册表必须恰好包含两个不同的模型")
        self.specs = {item.model_id: item for item in items}
        self.runtime = {item.model_id: ModelRuntime() for item in items}
        self.clock = clock
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.lock = Lock()
        self.fallbacks = 0
        self.all_models_failed = 0

    def ordered_candidates(self, task: str) -> list[ModelSpec]:
        strong = next(item for item in self.specs.values() if item.role == "strong")
        default = next(item for item in self.specs.values() if item.role == "default")
        base = [strong, default] if task in STRONG_TASKS else [default, strong]
        return self._ordered_eligible(base)

    def _ordered_eligible(self, base: list[ModelSpec]) -> list[ModelSpec]:
        with self.lock:
            self._refresh_open_states()
            eligible = [item for item in base if self.runtime[item.model_id].state != "open"]
            if len(eligible) != 2:
                return eligible
            half_open = [
                item for item in eligible
                if self.runtime[item.model_id].state == "half_open"
            ]
            if half_open:
                return half_open + [item for item in eligible if item not in half_open]
            first, second = eligible
            a = self.runtime[first.model_id]
            b = self.runtime[second.model_id]
            if len(a.samples) < 5 or len(b.samples) < 5:
                return eligible
            rate_a, rate_b = _success_rate(a), _success_rate(b)
            if rate_b - rate_a >= 0.20:
                return [second, first]
            latency_a, latency_b = _average_latency(a), _average_latency(b)
            if (abs(rate_a - rate_b) < 0.05 and latency_b > 0
                    and latency_a >= latency_b * 2):
                return [second, first]
            return eligible

    def _refresh_open_states(self) -> None:
        now = self.clock()
        for state in self.runtime.values():
            if (
                state.state == "open"
                and state.opened_at is not None
                and now - state.opened_at >= self.cooldown_seconds
            ):
                state.state = "half_open"
                state.half_open_in_flight = False

    def begin_attempt(self, model_id: str) -> bool:
        with self.lock:
            self._refresh_open_states()
            state = self.runtime[model_id]
            if state.state == "open":
                return False
            if state.state == "half_open":
                if state.half_open_in_flight:
                    return False
                state.half_open_in_flight = True
            return True

    def release_attempt(self, model_id: str) -> None:
        """Release a reserved half-open probe when no outcome was recorded."""
        with self.lock:
            state = self.runtime[model_id]
            if state.state == "half_open":
                state.half_open_in_flight = False

    def record_success(
        self, model_id: str, latency_ms: int, tokens_in: int, tokens_out: int
    ) -> None:
        with self.lock:
            state = self.runtime[model_id]
            state.attempts += 1
            state.successes += 1
            state.tokens_in += tokens_in
            state.tokens_out += tokens_out
            state.last_latency_ms = latency_ms
            state.last_error_type = ""
            state.last_error_status = None
            state.samples.append(CallSample(True, latency_ms))
            state.state = "closed"
            state.consecutive_failures = 0
            state.opened_at = None
            state.half_open_in_flight = False

    def record_failure(
        self,
        model_id: str,
        error_type: str,
        status: int | None,
        latency_ms: int,
    ) -> None:
        with self.lock:
            state = self.runtime[model_id]
            state.attempts += 1
            state.failures += 1
            state.last_latency_ms = latency_ms
            state.last_error_type = error_type
            state.last_error_status = status
            state.half_open_in_flight = False
            if error_type in {AUTH, REQUEST}:
                return
            state.samples.append(CallSample(False, latency_ms))
            if error_type == MODEL_UNAVAILABLE:
                state.consecutive_failures = self.failure_threshold
            elif error_type in BREAKER_ERRORS:
                state.consecutive_failures += 1
            if state.consecutive_failures >= self.failure_threshold:
                state.state = "open"
                state.opened_at = self.clock()

    def record_json_downgrade(self, model_id: str) -> None:
        with self.lock:
            self.runtime[model_id].json_downgrades += 1

    def record_fallback(self, source_model: str, target_model: str) -> None:
        with self.lock:
            self.fallbacks += 1
            self.runtime[source_model].fallback_out += 1
            self.runtime[target_model].fallback_in += 1

    def record_all_models_failed(self) -> None:
        with self.lock:
            self.all_models_failed += 1

    def snapshot(self) -> dict:
        with self.lock:
            self._refresh_open_states()
            models = []
            for spec in sorted(self.specs.values(), key=lambda item: item.role != "strong"):
                state = self.runtime[spec.model_id]
                attempts = state.attempts
                cooldown_remaining = 0.0
                if state.state == "open" and state.opened_at is not None:
                    cooldown_remaining = max(
                        0.0,
                        self.cooldown_seconds - (self.clock() - state.opened_at),
                    )
                models.append({
                    "id": spec.model_id,
                    "role": spec.role,
                    "health": state.state,
                    "cooldown_remaining_seconds": round(cooldown_remaining, 1),
                    "attempts": attempts,
                    "successes": state.successes,
                    "failures": state.failures,
                    "success_rate": round(state.successes / attempts, 4) if attempts else None,
                    "avg_latency_ms": round(_average_latency(state)),
                    "last_latency_ms": state.last_latency_ms,
                    "tokens_in": state.tokens_in,
                    "tokens_out": state.tokens_out,
                    "fallback_in": state.fallback_in,
                    "fallback_out": state.fallback_out,
                    "json_downgrades": state.json_downgrades,
                    "consecutive_failures": state.consecutive_failures,
                    "last_error": ({
                        "type": state.last_error_type,
                        "status": state.last_error_status,
                    } if state.last_error_type else None),
                })
            return {
                "strategy": "task-aware-health-adaptive",
                "models": models,
                "fallbacks": self.fallbacks,
                "all_models_failed": self.all_models_failed,
            }
