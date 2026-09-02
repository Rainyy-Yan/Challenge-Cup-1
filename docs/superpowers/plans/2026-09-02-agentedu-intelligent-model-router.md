# AgentEdu Intelligent Model Router Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an in-process, observable two-model router that selects between `deepseek-v4-pro` and `MiniMax-M3` by task, recent reliability, latency, and circuit-breaker state while preserving AgentEdu's existing `llm.run()` contract and rule-based fallback.

**Architecture:** Add a pure standard-library routing module that owns model registration, rolling metrics, deterministic candidate ordering, and circuit-breaker state. Keep HTTP protocol work in `core/llm.py`, let `RealLLM` coordinate retries and fallback through the router, and share one lazily-created client in `server.py` so health history survives across requests. Expose a sanitized read-only status snapshot through `/api/model-status`.

**Tech Stack:** Python standard library (`dataclasses`, `collections.deque`, `threading`, `time`, `urllib`, `unittest`), existing `http.server` application, OpenAI-compatible `/chat/completions` API.

**Spec:** `docs/superpowers/specs/2026-09-02-agentedu-intelligent-model-router-design.md`

## Global Constraints

- Register exactly two real model IDs: `deepseek-v4-pro` and `MiniMax-M3`.
- Keep `AGENTEDU_MODEL=MiniMax-M3` and `AGENTEDU_MODEL_STRONG=deepseek-v4-pro`; do not restore Qwen, `AGENTEDU_MODEL_MID`, or `AGENTEDU_MODEL_LIGHT`.
- Preserve the public call shape `llm.run(task, system, user, context=None, json_mode=False, temperature=None)`.
- Use only Python standard-library dependencies; do not add Spring AI, Redis, a database, Prometheus, or a separate gateway service.
- Keep `MockLLM` deterministic and prevent unit tests from reading a real key or sending paid requests.
- Never return API keys, Authorization headers, prompts, learner input, raw provider bodies, or model output from status APIs.
- HTTP 401 terminates the model chain. JSON-protocol incompatibility gets one same-model protocol downgrade. Retryable transport/provider errors may trigger model fallback.
- Use an injectable monotonic clock in router tests; no test may sleep for circuit-breaker cooldowns.
- Keep existing user changes in the dirty worktree. Stage only files named by the current task.
- Do not create a commit until the user explicitly authorizes that commit. Each commit step below is an authorization checkpoint.

---

### Task 1: Model registry and deterministic cold-start routing

**Files:**
- Create: `core/model_router.py`
- Create: `tests/test_model_router.py`

**Interfaces:**
- Produces: `ModelSpec`, `SmartModelRouter`, `SmartModelRouter.ordered_candidates(task: str) -> list[ModelSpec]`.
- Produces: `build_default_specs(default_model: str, strong_model: str, timeout: int, price_in: float, price_out: float) -> tuple[ModelSpec, ModelSpec]`.
- Consumes: Existing task names from `core.llm.TASK_TIER`; the router receives task strings and must not import `core.llm`, avoiding a circular import.

- [ ] **Step 1: Write failing registry and initial-order tests**

Create `tests/test_model_router.py` with these initial tests:

```python
import unittest

from core.model_router import ModelSpec, SmartModelRouter, build_default_specs


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestModelRegistry(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.specs = build_default_specs(
            default_model="MiniMax-M3",
            strong_model="deepseek-v4-pro",
            timeout=60,
            price_in=0.0,
            price_out=0.0,
        )
        self.router = SmartModelRouter(self.specs, clock=self.clock)

    def ids(self, task: str) -> list[str]:
        return [spec.model_id for spec in self.router.ordered_candidates(task)]

    def test_registry_contains_exactly_two_distinct_models(self) -> None:
        self.assertEqual(
            [spec.model_id for spec in self.specs],
            ["deepseek-v4-pro", "MiniMax-M3"],
        )

    def test_make_item_prefers_deepseek(self) -> None:
        self.assertEqual(
            self.ids("make_item"),
            ["deepseek-v4-pro", "MiniMax-M3"],
        )

    def test_normal_generation_prefers_minimax(self) -> None:
        for task in ("draft_claims", "verify", "quiz", "analyze_intake",
                     "synthesize", "diagnose_narrative", "simplify"):
            with self.subTest(task=task):
                self.assertEqual(
                    self.ids(task),
                    ["MiniMax-M3", "deepseek-v4-pro"],
                )

    def test_duplicate_model_ids_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "两个不同的模型"):
            build_default_specs("MiniMax-M3", "MiniMax-M3", 60, 0.0, 0.0)

    def test_empty_model_id_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "模型 ID"):
            build_default_specs("", "deepseek-v4-pro", 60, 0.0, 0.0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```powershell
py -3 -m unittest tests.test_model_router.TestModelRegistry -v
```

Expected: import failure for `core.model_router`.

- [ ] **Step 3: Implement the registry and cold-start policy**

Create `core/model_router.py` with the following public data model and validation:

```python
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from threading import Lock
from typing import Callable, Iterable


STRONG_TASKS = frozenset({"make_item"})


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
            return [item for item in base if self.runtime[item.model_id].state != "open"]

    def _refresh_open_states(self) -> None:
        now = self.clock()
        for state in self.runtime.values():
            if (state.state == "open" and state.opened_at is not None
                    and now - state.opened_at >= self.cooldown_seconds):
                state.state = "half_open"
                state.half_open_in_flight = False
```

The remaining health and scoring methods are added in Task 2. Do not add HTTP imports to this module.

- [ ] **Step 4: Run Task 1 tests and verify GREEN**

Run:

```powershell
py -3 -m unittest tests.test_model_router.TestModelRegistry -v
```

Expected: 5 tests pass.

- [ ] **Step 5: Review the Task 1 diff and request commit authorization**

Run:

```powershell
git diff --check -- core/model_router.py tests/test_model_router.py
git diff -- core/model_router.py tests/test_model_router.py
```

Stop and ask the user to authorize this commit. After explicit authorization only:

```powershell
git add -- core/model_router.py tests/test_model_router.py
git commit -m "feat: add two-model routing registry"
```

---

### Task 2: Rolling health metrics, dynamic ordering, and circuit breaker

**Files:**
- Modify: `core/model_router.py`
- Modify: `tests/test_model_router.py`

**Interfaces:**
- Consumes: `ModelSpec`, `ModelRuntime`, and `SmartModelRouter` from Task 1.
- Produces: `SmartModelRouter.begin_attempt(model_id: str) -> bool`.
- Produces: `record_success`, `record_failure`, `record_json_downgrade`, `record_fallback`, `record_all_models_failed`, and `snapshot`.
- Produces error-kind constants: `AUTH`, `REQUEST`, `MODEL_UNAVAILABLE`, `RATE_LIMIT`, `PROVIDER`, `NETWORK`, `INVALID_RESPONSE`.

- [ ] **Step 1: Add failing dynamic-route and breaker tests**

Append these tests to `tests/test_model_router.py`:

```python
class TestDynamicRouting(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        specs = build_default_specs(
            "MiniMax-M3", "deepseek-v4-pro", 60, 0.0, 0.0)
        self.router = SmartModelRouter(
            specs,
            clock=self.clock,
            failure_threshold=99,
            cooldown_seconds=60.0,
        )

    def ids(self, task: str) -> list[str]:
        return [spec.model_id for spec in self.router.ordered_candidates(task)]

    def test_low_success_rate_overrides_static_preference_after_five_samples(self) -> None:
        for _ in range(5):
            self.router.record_failure(
                "deepseek-v4-pro", "provider", 503, 100,
            )
            self.router.record_success("MiniMax-M3", 120, 10, 5)
        self.assertEqual(self.ids("make_item")[0], "MiniMax-M3")

    def test_two_times_slower_model_loses_when_success_rates_are_close(self) -> None:
        for _ in range(5):
            self.router.record_success("deepseek-v4-pro", 2400, 10, 5)
            self.router.record_success("MiniMax-M3", 800, 10, 5)
        self.assertEqual(self.ids("make_item")[0], "MiniMax-M3")

    def test_fewer_than_five_samples_keeps_static_order(self) -> None:
        for _ in range(4):
            self.router.record_failure(
                "deepseek-v4-pro", "provider", 503, 100,
            )
            self.router.record_success("MiniMax-M3", 100, 10, 5)
        self.assertEqual(self.ids("make_item")[0], "deepseek-v4-pro")


class TestCircuitBreaker(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        specs = build_default_specs(
            "MiniMax-M3", "deepseek-v4-pro", 60, 0.0, 0.0)
        self.router = SmartModelRouter(
            specs,
            clock=self.clock,
            failure_threshold=3,
            cooldown_seconds=60.0,
        )

    def test_three_retryable_failures_open_the_model(self) -> None:
        for _ in range(3):
            self.router.record_failure("deepseek-v4-pro", "provider", 503, 100)
        self.assertEqual(
            [item.model_id for item in self.router.ordered_candidates("make_item")],
            ["MiniMax-M3"],
        )

    def test_cooldown_allows_one_half_open_probe(self) -> None:
        for _ in range(3):
            self.router.record_failure("deepseek-v4-pro", "network", None, 100)
        self.clock.advance(60.0)
        self.assertTrue(self.router.begin_attempt("deepseek-v4-pro"))
        self.assertFalse(self.router.begin_attempt("deepseek-v4-pro"))

    def test_half_open_success_closes_the_breaker(self) -> None:
        for _ in range(3):
            self.router.record_failure("deepseek-v4-pro", "network", None, 100)
        self.clock.advance(60.0)
        self.assertTrue(self.router.begin_attempt("deepseek-v4-pro"))
        self.router.record_success("deepseek-v4-pro", 100, 10, 5)
        self.assertEqual(self.router.snapshot()["models"][0]["health"], "closed")

    def test_model_unavailable_opens_immediately(self) -> None:
        self.router.record_failure(
            "deepseek-v4-pro", "model_unavailable", 404, 50)
        ids = [item.model_id for item in self.router.ordered_candidates("make_item")]
        self.assertEqual(ids, ["MiniMax-M3"])

    def test_auth_and_request_errors_do_not_poison_model_health(self) -> None:
        self.router.record_failure("deepseek-v4-pro", "auth", 401, 50)
        self.router.record_failure("deepseek-v4-pro", "request", 400, 50)
        model = self.router.snapshot()["models"][0]
        self.assertEqual(model["health"], "closed")
        self.assertEqual(model["consecutive_failures"], 0)
        self.assertEqual(model["attempts"], 2)
        self.assertEqual(model["failures"], 2)

    def test_attempt_count_equals_recorded_outcomes(self) -> None:
        self.router.record_success("deepseek-v4-pro", 100, 10, 5)
        self.router.record_failure("deepseek-v4-pro", "network", None, 80)
        model = self.router.snapshot()["models"][0]
        self.assertEqual(model["attempts"], model["successes"] + model["failures"])
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```powershell
py -3 -m unittest tests.test_model_router.TestDynamicRouting tests.test_model_router.TestCircuitBreaker -v
```

Expected: failures for missing `record_success`, `record_failure`, `begin_attempt`, and `snapshot`.

- [ ] **Step 3: Implement health transitions and dynamic ordering**

Add these constants and methods to `core/model_router.py`:

```python
AUTH = "auth"
REQUEST = "request"
MODEL_UNAVAILABLE = "model_unavailable"
RATE_LIMIT = "rate_limit"
PROVIDER = "provider"
NETWORK = "network"
INVALID_RESPONSE = "invalid_response"
BREAKER_ERRORS = frozenset({RATE_LIMIT, PROVIDER, NETWORK, INVALID_RESPONSE})


def _success_rate(state: ModelRuntime) -> float:
    if not state.samples:
        return 1.0
    return sum(1 for sample in state.samples if sample.ok) / len(state.samples)


def _average_latency(state: ModelRuntime) -> float:
    values = [sample.latency_ms for sample in state.samples if sample.ok]
    return sum(values) / len(values) if values else 0.0
```

Implement the following exact method behavior inside `SmartModelRouter`:

```python
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
```

Extend `_ordered_eligible` after filtering open models:

```python
eligible = [item for item in base if self.runtime[item.model_id].state != "open"]
if len(eligible) != 2:
    return eligible
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
```

Implement `snapshot()` with model order `(strong, default)` and only sanitized values:

```python
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
```

- [ ] **Step 4: Run all router tests and verify GREEN**

Run:

```powershell
py -3 -m unittest tests.test_model_router -v
```

Expected: all registry, dynamic-routing, and breaker tests pass without sleeping.

- [ ] **Step 5: Review the Task 2 diff and request commit authorization**

Run:

```powershell
git diff --check -- core/model_router.py tests/test_model_router.py
git diff -- core/model_router.py tests/test_model_router.py
```

Stop and ask the user to authorize this commit. After explicit authorization only:

```powershell
git add -- core/model_router.py tests/test_model_router.py
git commit -m "feat: add model health routing and circuit breaker"
```

---

### Task 3: Protocol adapter and provider error classification

**Files:**
- Modify: `core/llm.py`
- Modify: `tests/test_llm_client.py`

**Interfaces:**
- Consumes router error constants from `core.model_router`.
- Produces: `ModelCallError(kind: str, status: int | None, summary: str, latency_ms: int)`.
- Produces: `classify_http_error(status: int, body: str) -> str`.
- Produces: `OpenAIAdapter.post(payload: dict, timeout: int) -> tuple[dict, int]`, where the integer is elapsed milliseconds.

- [ ] **Step 1: Add failing error-classification tests**

Add this test class to `tests/test_llm_client.py` and import `classify_http_error`:

```python
class TestProviderErrorClassification(unittest.TestCase):
    def test_auth_is_terminal(self) -> None:
        self.assertEqual(classify_http_error(401, "unauthorized"), "auth")

    def test_retryable_provider_errors_are_classified(self) -> None:
        self.assertEqual(classify_http_error(429, "rate limit"), "rate_limit")
        for status in (500, 502, 503, 504):
            with self.subTest(status=status):
                self.assertEqual(classify_http_error(status, "upstream"), "provider")

    def test_model_specific_unavailability_can_arrive_as_400(self) -> None:
        bodies = (
            '{"error":{"code":"model_not_found"}}',
            '{"error":{"message":"model does not exist"}}',
            '{"error":{"message":"access denied for model"}}',
        )
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(classify_http_error(400, body), "model_unavailable")

    def test_plain_bad_request_is_not_a_model_failure(self) -> None:
        self.assertEqual(classify_http_error(400, "invalid messages"), "request")
```

- [ ] **Step 2: Run the classification tests and verify RED**

Run:

```powershell
py -3 -m unittest tests.test_llm_client.TestProviderErrorClassification -v
```

Expected: import or attribute failure for `classify_http_error`.

- [ ] **Step 3: Implement the adapter and classification boundary**

In `core/llm.py`, import the router error constants and add:

```python
from dataclasses import dataclass

from core.model_router import (
    AUTH, INVALID_RESPONSE, MODEL_UNAVAILABLE, NETWORK, PROVIDER,
    RATE_LIMIT, REQUEST, ModelSpec, SmartModelRouter, build_default_specs,
)


@dataclass
class ModelCallError(Exception):
    kind: str
    status: int | None
    summary: str
    latency_ms: int

    def __str__(self) -> str:
        return self.summary


@dataclass(frozen=True)
class ModelResult:
    content: str
    tokens_in: int
    tokens_out: int
    latency_ms: int


def classify_http_error(status: int, body: str) -> str:
    lower = body.lower()
    unavailable_markers = (
        "model_not_found",
        "model does not exist",
        "model not found",
        "access denied for model",
    )
    if status == 401:
        return AUTH
    if status in (403, 404) or (
            status == 400 and any(marker in lower for marker in unavailable_markers)):
        return MODEL_UNAVAILABLE
    if status == 429:
        return RATE_LIMIT
    if status in (500, 502, 503, 504):
        return PROVIDER
    return REQUEST


class OpenAIAdapter:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def post(self, payload: dict, timeout: int) -> tuple[dict, int]:
        started = time.perf_counter()
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            body = ""
            try:
                body = exc.read().decode("utf-8", "ignore")[:300]
            except Exception:
                body = ""
            raise ModelCallError(
                classify_http_error(exc.code, body),
                exc.code,
                f"HTTP {exc.code}: {body}",
                elapsed_ms,
            ) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            raise ModelCallError(
                INVALID_RESPONSE,
                None,
                "响应不是合法 JSON",
                elapsed_ms,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            raise ModelCallError(
                NETWORK, None, str(exc)[:300], elapsed_ms) from exc
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return data, elapsed_ms
```

Replace the old `RealLLM._post()` only after Task 4 switches `run()` to `OpenAIAdapter`; do not leave two active HTTP paths.

- [ ] **Step 4: Run classification and existing protocol tests**

Run:

```powershell
py -3 -m unittest tests.test_llm_client.TestProviderErrorClassification tests.test_llm_client.TestRealLLMResilience.test_json_mode_downgrades_once_and_remembers tests.test_llm_client.TestRealLLMResilience.test_auth_error_is_not_retried -v
```

Expected: classification tests pass; existing JSON and auth tests remain green.

- [ ] **Step 5: Review the Task 3 diff and request commit authorization**

Run:

```powershell
git diff --check -- core/llm.py tests/test_llm_client.py
git diff -- core/llm.py tests/test_llm_client.py
```

Stop and ask the user to authorize this commit. After explicit authorization only:

```powershell
git add -- core/llm.py tests/test_llm_client.py
git commit -m "refactor: isolate model protocol errors"
```

---

### Task 4: Integrate retries, dynamic candidates, fallback, cache, and attempt budget

**Files:**
- Modify: `core/llm.py`
- Modify: `tests/test_llm_client.py`

**Interfaces:**
- Consumes: `SmartModelRouter.ordered_candidates`, `begin_attempt`, result-recording methods, and `OpenAIAdapter.post`.
- Preserves: Existing `RealLLM.__init__` positional parameters and `run()` signature.
- Produces: `RealLLM.model_status() -> dict` and compatible `stats()` output.

- [ ] **Step 1: Add failing integration tests for routing semantics**

Extend the fake handler in `tests/test_llm_client.py` after the existing auth
branch with these exact model-aware modes; keep the existing `no_json`, `flaky`,
and success branches:

```python
def _json_response(self, status: int, payload: dict) -> None:
    raw = json.dumps(payload).encode("utf-8")
    self.send_response(status)
    self.send_header("Content-Type", "application/json")
    self.send_header("Content-Length", str(len(raw)))
    self.end_headers()
    self.wfile.write(raw)

def _error(self, status: int, payload: dict) -> None:
    self._json_response(status, payload)

if mode == "bad_request":
    return self._error(400, {"error": {"message": "invalid messages"}})
if (mode == "model_not_found"
        and body.get("model") == "deepseek-v4-pro"):
    return self._error(400, {"error": {"code": "model_not_found"}})
if (mode == "primary_unavailable"
        and body.get("model") == "deepseek-v4-pro"):
    return self._error(503, {"error": {"message": "upstream unavailable"}})
if (mode == "malformed_primary"
        and body.get("model") == "deepseek-v4-pro"):
    return self._json_response(200, {"choices": [], "usage": {}})
```

Use those helpers for the pre-existing error and success responses too, so every
response has a JSON body and `Content-Length`. Add tests with these exact
assertions:

```python
def test_plain_bad_request_does_not_fallback(self) -> None:
    STATE["mode"] = "bad_request"
    llm = self._llm(
        model="MiniMax-M3",
        models={"strong": "deepseek-v4-pro"},
        retries=3,
    )
    with self.assertRaises(LLMError):
        llm.run("make_item", "s", "u")
    self.assertEqual(STATE["models"], ["deepseek-v4-pro"])

def test_model_not_found_falls_back_without_retrying_primary(self) -> None:
    STATE["mode"] = "model_not_found"
    llm = self._llm(
        model="MiniMax-M3",
        models={"strong": "deepseek-v4-pro"},
        retries=3,
    )
    self.assertIn("ok", llm.run("make_item", "s", "u"))
    self.assertEqual(STATE["models"], ["deepseek-v4-pro", "MiniMax-M3"])

def test_failed_http_attempts_consume_budget(self) -> None:
    STATE["mode"] = "primary_unavailable"
    llm = self._llm(
        model="MiniMax-M3",
        models={"strong": "deepseek-v4-pro"},
        retries=3,
        budget_calls=2,
    )
    with self.assertRaisesRegex(LLMError, "调用上限"):
        llm.run("make_item", "s", "u")
    self.assertEqual(STATE["hits"], 2)
    self.assertTrue(llm.stats()["budget_hit"])

def test_cached_fallback_result_avoids_new_http_attempts(self) -> None:
    STATE["mode"] = "primary_unavailable"
    llm = self._llm(
        model="MiniMax-M3",
        models={"strong": "deepseek-v4-pro"},
        retries=1,
        cache=True,
    )
    first = llm.run("make_item", "s", "u")
    hits = STATE["hits"]
    second = llm.run("make_item", "s", "u")
    self.assertEqual(second, first)
    self.assertEqual(STATE["hits"], hits)
    self.assertEqual(llm.stats()["cache_hits"], 1)

def test_malformed_success_is_retried_then_falls_back(self) -> None:
    STATE["mode"] = "malformed_primary"
    llm = self._llm(
        model="MiniMax-M3",
        models={"strong": "deepseek-v4-pro"},
        retries=1,
    )
    self.assertIn("ok", llm.run("make_item", "s", "u"))
    self.assertEqual(STATE["models"], ["deepseek-v4-pro", "MiniMax-M3"])
```

Update the existing 401 test to assert no fallback model appears. Keep the
existing successful fallback test. Update the JSON downgrade tests to inject a
`SmartModelRouter` built from `ModelSpec("strong-model", "strong", 5, True)`
and `ModelSpec("default-model", "default", 5, True)`, assert
`llm._json_mode_ok["default-model"]` becomes false, and assert the retry's user
message contains the strict JSON instruction. This is the only test path that
starts with native JSON mode enabled.

- [ ] **Step 2: Run the new integration tests and verify RED**

Run:

```powershell
py -3 -m unittest tests.test_llm_client.TestRealLLMResilience tests.test_llm_client.TestFreeTierGuards -v
```

Expected: the plain 400, attempt-budget, and router-backed status assertions fail against the old `RealLLM.run()`.

- [ ] **Step 3: Assemble the router in `RealLLM.__init__`**

Preserve existing arguments and add keyword-only injection points at the end:

```python
def __init__(
    self,
    base_url: str,
    api_key: str,
    model: str,
    timeout: int = 60,
    models: dict | None = None,
    retries: int = 3,
    price_in: float = 0.0,
    price_out: float = 0.0,
    rpm: int = 0,
    budget_calls: int = 0,
    cache: bool = True,
    *,
    router: SmartModelRouter | None = None,
    adapter: OpenAIAdapter | None = None,
) -> None:
```

Build router specs from `model` and `models["strong"]`, assign `self.adapter`, and replace successful-call budget state with an HTTP-attempt counter:

```python
strong_model = (models or {}).get("strong", "").strip()
specs = build_default_specs(
    model,
    strong_model,
    timeout,
    price_in,
    price_out,
)
self.router = router or SmartModelRouter(specs)
self.adapter = adapter or OpenAIAdapter(base_url, api_key)
self._json_mode_ok = {
    spec.model_id: spec.supports_json_mode
    for spec in self.router.specs.values()
}
self.http_attempts = 0
```

Preserve `self.base_url`, `self.api_key`, `self.model`, `self.models`,
`self.timeout`, the existing counters, cache, RPM fields, and `cost()`.
Normalize `self.models` to `{"strong": strong_model}`. Replace `model_for()`
with:

```python
def model_for(self, task: str) -> str:
    candidates = self.router.ordered_candidates(task)
    return candidates[0].model_id if candidates else self.model
```

Make the existing tests obey the same two-distinct-model invariant as production:

- In `TestRealLLMResilience._llm`, keep `default-model` as the default and add
  `kw.setdefault("models", {"strong": "strong-model"})` before constructing
  `RealLLM`.
- Replace `test_task_routing_picks_tier_model` with assertions that `make_item`
  selects `strong-model` and `synthesize` selects `default-model`; delete the
  obsolete light-tier and missing-tier assertions.
- Add `TestFreeTierGuards._llm(**kw)`, constructing `RealLLM` with default
  `MiniMax-M3`, `models={"strong": "deepseek-v4-pro"}`, and `timeout=5`.
  Replace all seven direct `RealLLM(...)` constructions in that class with this
  helper.

Do not introduce a test-only duplicate-ID exception: production and test code
must both reject missing or duplicate model IDs.

- [ ] **Step 4: Replace `run()` candidate coordination**

Implement the following control order in `RealLLM.run()`:

```python
candidates = self.router.ordered_candidates(task)
if not candidates:
    self.router.record_all_models_failed()
    self.failures += 1
    raise LLMError(f"没有健康模型可执行任务 {task}")

first_model = candidates[0].model_id
cache_key = self._key(task, first_model, system, user, temp)
if self.cache_enabled and cache_key in self._cache:
    self.cache_hits += 1
    return self._cache[cache_key]

last_error: ModelCallError | None = None
previous_model = ""
for spec in candidates:
    candidate_started = False
    protocol_downgraded = False
    retry_index = 0
    while retry_index < self.retries:
        if self.budget_calls and self.http_attempts >= self.budget_calls:
            self.budget_hit = True
            raise LLMError(
                f"已达调用上限 {self.budget_calls} 次（AGENTEDU_BUDGET_CALLS）")
        if not self.router.begin_attempt(spec.model_id):
            break
        if not candidate_started:
            if previous_model:
                self.router.record_fallback(previous_model, spec.model_id)
            previous_model = spec.model_id
            candidate_started = True
        self._throttle()
        self.http_attempts += 1
        payload = self._payload(spec, system, user, temp, json_mode)
        try:
            data, latency_ms = self.adapter.post(payload, spec.timeout)
            result = self._parse_result(spec, data, latency_ms)
        except ModelCallError as exc:
            last_error = exc
            self.router.record_failure(
                spec.model_id, exc.kind, exc.status, exc.latency_ms)
            if (json_mode and not protocol_downgraded
                    and self._is_response_format_error(exc)):
                self._json_mode_ok[spec.model_id] = False
                self.router.record_json_downgrade(spec.model_id)
                protocol_downgraded = True
                continue
            if exc.kind in {AUTH, REQUEST}:
                self.failures += 1
                raise LLMError(f"调用模型失败（{spec.model_id}）: {exc}") from exc
            if exc.kind == MODEL_UNAVAILABLE:
                break
            retry_index += 1
            if retry_index < self.retries:
                time.sleep(min(8.0, 1.5 ** (retry_index - 1)))
                continue
            break
        self.router.record_success(
            spec.model_id,
            result.latency_ms,
            result.tokens_in,
            result.tokens_out,
        )
        self.calls += 1
        self.tokens_in += result.tokens_in
        self.tokens_out += result.tokens_out
        self._record_task_usage(task, result.tokens_in, result.tokens_out)
        if self.cache_enabled:
            self._cache[cache_key] = result.content
        return result.content

self.router.record_all_models_failed()
self.failures += 1
detail = str(last_error) if last_error else "所有候选均被熔断"
raise LLMError(f"调用模型失败（任务 {task}）: {detail}")
```

Extract `_payload`, `_parse_result`, `_is_response_format_error`, and
`_record_task_usage` as private methods so `run()` does not duplicate
serialization and accounting. `_parse_result` returns `ModelResult`; when
content is absent it must raise:

```python
raise ModelCallError(
    INVALID_RESPONSE,
    None,
    "响应缺少 choices[0].message.content",
    latency_ms,
)
```

`_payload` must consult `self._json_mode_ok[spec.model_id]`. When `json_mode`
is true and the value is true, include
`"response_format": {"type": "json_object"}`. When it is false, omit that
field and append `"请只输出合法 JSON，不要输出 Markdown 代码围栏或额外说明。"`
to the user message. `_is_response_format_error` returns true only for a 400
`REQUEST` error whose summary contains `response_format`.

Both configured models start with `supports_json_mode=False`, so normal
production JSON calls use the prompt constraint immediately. The one-time
protocol-downgrade branch remains covered with an injected test router whose
target spec has `supports_json_mode=True`. Its second HTTP request counts toward
`http_attempts` and `AGENTEDU_BUDGET_CALLS`, but does not increment
`retry_index`; any later request for that model remembers the downgrade.

- [ ] **Step 5: Merge router metrics into compatible stats**

Keep all existing top-level keys and add the router snapshot:

```python
def model_status(self) -> dict:
    snap = self.router.snapshot()
    return {
        "mode": "real",
        "strategy": snap["strategy"],
        "models": snap["models"],
        "router": {
            "fallbacks": snap["fallbacks"],
            "all_models_failed": snap["all_models_failed"],
        },
    }

def stats(self) -> dict:
    status = self.model_status()
    return {
        "calls": self.calls,
        "http_attempts": self.http_attempts,
        "failures": self.failures,
        "tokens_in": self.tokens_in,
        "tokens_out": self.tokens_out,
        "cost_cny": self.cost(),
        "by_task": self.by_task,
        "by_model": {
            item["id"]: {
                "calls": item["successes"],
                "in": item["tokens_in"],
                "out": item["tokens_out"],
            }
            for item in status["models"]
        },
        "fallbacks": status["router"]["fallbacks"],
        "cache_hits": self.cache_hits,
        "rpm_limit": self.rpm,
        "budget_calls": self.budget_calls,
        "budget_hit": self.budget_hit,
        "json_mode": all(self._json_mode_ok.values()),
        "router": status["router"],
        "models": status["models"],
    }
```

- [ ] **Step 6: Run all model client tests and verify GREEN**

Run:

```powershell
py -3 -m unittest tests.test_model_router tests.test_llm_client tests.test_dotenv -v
```

Expected: all tests pass; no command contacts an external model endpoint.

- [ ] **Step 7: Review the Task 4 diff and request commit authorization**

Run:

```powershell
git diff --check -- core/model_router.py core/llm.py tests/test_model_router.py tests/test_llm_client.py tests/test_dotenv.py
git diff -- core/model_router.py core/llm.py tests/test_model_router.py tests/test_llm_client.py tests/test_dotenv.py
```

Stop and ask the user to authorize this commit. After explicit authorization only:

```powershell
git add -- core/model_router.py core/llm.py tests/test_model_router.py tests/test_llm_client.py tests/test_dotenv.py
git commit -m "feat: route and fallback model calls intelligently"
```

---

### Task 5: Shared server client and sanitized model-status API

**Files:**
- Modify: `server.py`
- Create: `tests/test_model_status_api.py`

**Interfaces:**
- Consumes: `build_llm()`, `RealLLM.model_status()`, and `MockLLM`.
- Produces: `get_model_client()` singleton accessor and `model_status_payload()`.
- Produces: `GET /api/model-status`.

- [ ] **Step 1: Write failing singleton and HTTP endpoint tests**

Create `tests/test_model_status_api.py`:

```python
import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import server
from core.llm import MockLLM


class FakeRealLLM:
    def model_status(self) -> dict:
        return {
            "mode": "real",
            "strategy": "task-aware-health-adaptive",
            "models": [
                {
                    "id": "deepseek-v4-pro", "role": "strong",
                    "health": "closed", "cooldown_remaining_seconds": 0,
                    "attempts": 1, "successes": 1,
                    "failures": 0, "success_rate": 1.0,
                    "avg_latency_ms": 120, "last_latency_ms": 120,
                    "tokens_in": 10, "tokens_out": 5,
                    "fallback_in": 0, "fallback_out": 0,
                    "json_downgrades": 0, "consecutive_failures": 0,
                    "last_error": None,
                },
                {
                    "id": "MiniMax-M3", "role": "default",
                    "health": "closed", "cooldown_remaining_seconds": 0,
                    "attempts": 0, "successes": 0,
                    "failures": 0, "success_rate": None,
                    "avg_latency_ms": 0, "last_latency_ms": 0,
                    "tokens_in": 0, "tokens_out": 0,
                    "fallback_in": 0, "fallback_out": 0,
                    "json_downgrades": 0, "consecutive_failures": 0,
                    "last_error": None,
                },
            ],
            "router": {"fallbacks": 0, "all_models_failed": 0},
        }


class TestSharedModelClient(unittest.TestCase):
    def tearDown(self) -> None:
        server._MODEL_CLIENT = None

    def test_client_is_built_once(self) -> None:
        fake = MockLLM()
        with patch("server.build_llm", return_value=fake) as build:
            self.assertIs(server.get_model_client(), fake)
            self.assertIs(server.get_model_client(), fake)
        build.assert_called_once_with()

    def test_offline_status_has_stable_shape(self) -> None:
        server._MODEL_CLIENT = MockLLM()
        self.assertEqual(server.model_status_payload(), {
            "mode": "offline",
            "strategy": "deterministic-rules",
            "models": [],
            "router": {"fallbacks": 0, "all_models_failed": 0},
        })


class TestModelStatusEndpoint(unittest.TestCase):
    def setUp(self) -> None:
        server._MODEL_CLIENT = FakeRealLLM()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        server._MODEL_CLIENT = None

    def test_get_model_status_returns_sanitized_snapshot(self) -> None:
        port = self.httpd.server_address[1]
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/model-status", timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(response.status, 200)
        self.assertEqual(len(payload["models"]), 2)
        self.assertEqual(payload["models"][0]["id"], "deepseek-v4-pro")
        serialized = json.dumps(payload).lower()
        for forbidden in ("api_key", "authorization", "prompt", "base_url"):
            self.assertNotIn(forbidden, serialized)
```

- [ ] **Step 2: Run endpoint tests and verify RED**

Run:

```powershell
py -3 -m unittest tests.test_model_status_api -v
```

Expected: missing `get_model_client`, `model_status_payload`, and `_MODEL_CLIENT` failures.

- [ ] **Step 3: Add the lazy shared client**

In `server.py`, import `Lock` and `MockLLM`, then add module state:

```python
from threading import Lock

from core.llm import MockLLM, build_llm

_MODEL_CLIENT = None
_MODEL_CLIENT_LOCK = Lock()


def get_model_client():
    global _MODEL_CLIENT
    if _MODEL_CLIENT is None:
        with _MODEL_CLIENT_LOCK:
            if _MODEL_CLIENT is None:
                _MODEL_CLIENT = build_llm()
    return _MODEL_CLIENT


def model_status_payload() -> dict:
    client = get_model_client()
    if isinstance(client, MockLLM):
        return {
            "mode": "offline",
            "strategy": "deterministic-rules",
            "models": [],
            "router": {"fallbacks": 0, "all_models_failed": 0},
        }
    return client.model_status()
```

Pass the same client into every server-created model consumer:

```python
def _examiner():
    if not config.EXAMINER_ENABLED:
        return None
    return ExaminerAgent(
        get_model_client(), Retriever.from_jsonl(config.KB_PATH), _KP_INDEX)
```

Change both `Orchestrator()` call sites to `Orchestrator(llm=get_model_client())`, and change `IntakeAgent(build_llm())` to `IntakeAgent(get_model_client())`.

- [ ] **Step 4: Add the read-only endpoint**

Insert this branch before profile/session routing in `Handler.do_GET`:

```python
if path == "/api/model-status":
    return self._json(model_status_payload())
```

- [ ] **Step 5: Run endpoint and pipeline tests**

Run:

```powershell
py -3 -m unittest tests.test_model_status_api tests.test_pipeline tests.test_state_machine -v
```

Expected: all tests pass and the status response contains no sensitive field names.

- [ ] **Step 6: Review the Task 5 diff and request commit authorization**

Run:

```powershell
git diff --check -- server.py tests/test_model_status_api.py
git diff -- server.py tests/test_model_status_api.py
```

Stop and ask the user to authorize this commit. After explicit authorization only:

```powershell
git add -- server.py tests/test_model_status_api.py
git commit -m "feat: expose sanitized model router status"
```

---

### Task 6: Doctor output, configuration validation, and documentation

**Files:**
- Modify: `evalkit/doctor.py`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `docs/接入大模型.md`
- Modify: `docs/book/chapter/chapter08.tex`
- Modify: `tests/test_dotenv.py`
- Create: `tests/test_doctor.py`

**Interfaces:**
- Consumes: `RealLLM.model_status()` and the existing `.env` loader.
- Produces: `format_model_status(status: dict) -> list[str]` and an
  operator-visible two-model route, health, fallback, attempt, and Token summary.

- [ ] **Step 1: Add failing `.env` contract tests**

Extend `tests/test_dotenv.py` with a helper that reads only model keys from `.env.example` and assert:

```python
def test_example_enables_only_the_two_router_models(self) -> None:
    example = (Path(config.ROOT) / ".env.example").read_text(encoding="utf-8")
    model_lines = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in example.splitlines()
        if line.startswith("AGENTEDU_MODEL")
    }
    self.assertEqual(model_lines, {
        "AGENTEDU_MODEL": "MiniMax-M3",
        "AGENTEDU_MODEL_STRONG": "deepseek-v4-pro",
    })
    self.assertNotIn("qwen", example.lower())
    self.assertNotIn("AGENTEDU_MODEL_MID", example)
    self.assertNotIn("AGENTEDU_MODEL_LIGHT", example)
```

Create `tests/test_doctor.py` with a focused formatter test:

```python
import unittest

from evalkit.doctor import format_model_status


class TestDoctorModelStatus(unittest.TestCase):
    def test_formats_both_models_and_omits_secrets(self) -> None:
        status = {
            "mode": "real",
            "strategy": "task-aware-health-adaptive",
            "models": [
                {
                    "id": "deepseek-v4-pro", "role": "strong",
                    "health": "closed", "attempts": 3,
                    "success_rate": 2 / 3, "avg_latency_ms": 900,
                },
                {
                    "id": "MiniMax-M3", "role": "default",
                    "health": "closed", "attempts": 4,
                    "success_rate": 1.0, "avg_latency_ms": 300,
                },
            ],
            "router": {"fallbacks": 2, "all_models_failed": 0},
        }
        text = "\n".join(format_model_status(status))
        self.assertIn("deepseek-v4-pro", text)
        self.assertIn("MiniMax-M3", text)
        self.assertIn("自动降级 2 次", text)
        for forbidden in ("api_key", "authorization", "prompt", "base_url"):
            self.assertNotIn(forbidden, text.lower())


if __name__ == "__main__":
    unittest.main()
```

The test fixture itself must not contain a credential. A JSON serialization
check remains in the status API tests, where the full payload contract belongs.

- [ ] **Step 2: Run configuration and formatter tests and verify RED**

Run:

```powershell
py -3 -m unittest tests.test_dotenv tests.test_doctor -v
```

Expected: the `.env.example` contract stays green and `tests.test_doctor` fails
to import the not-yet-created `format_model_status`.

- [ ] **Step 3: Update doctor output**

Add this pure formatter to `evalkit/doctor.py`:

```python
def format_model_status(status: dict) -> list[str]:
    lines = ["", f"路由策略 {status['strategy']}"]
    for model in status["models"]:
        rate = model["success_rate"]
        rate_text = "暂无样本" if rate is None else f"{rate:.0%}"
        lines.append(
            f"  {model['id']} [{model['role']}] "
            f"{model['health']}，尝试 {model['attempts']}，"
            f"成功率 {rate_text}，平均 {model['avg_latency_ms']} ms"
        )
    lines.append(
        f"自动降级 {status['router']['fallbacks']} 次，"
        f"全部模型失败 {status['router']['all_models_failed']} 次"
    )
    return lines
```

Replace the old STRONG/MID/LIGHT display and one-model probe loop with a direct
probe of both configured IDs. Each probe constructs a valid two-model client,
puts the target in the default role, disables cache, and inspects per-model
success metrics so a successful fallback cannot hide a bad model ID:

```python
model_ids = [llm.model, llm.models["strong"]]
print(f"模型   默认 {model_ids[0]}　强模型 {model_ids[1]}")
print()

for name in model_ids:
    other = next(item for item in model_ids if item != name)
    probe = RealLLM(
        llm.base_url,
        llm.api_key,
        name,
        timeout=llm.timeout,
        models={"strong": other},
        retries=1,
        cache=False,
    )
    try:
        probe.run(task="simplify", system="测试", user="回复：ok")
        target = next(
            item for item in probe.model_status()["models"]
            if item["id"] == name
        )
        if target["successes"] == 1:
            _line(f"模型 {name}", OK, "可调用")
        else:
            _line(f"模型 {name}", BAD, "目标调用失败，结果来自自动降级")
    except LLMError as exc:
        _line(f"模型 {name}", BAD, str(exc)[:100])
```

Because the fixed two-model registry marks native structured output unsupported,
replace the old Boolean `_json_mode_ok` check with
`all(llm._json_mode_ok.values())`; report prompt-constrained JSON when false.

After the existing six checks, emit the formatted status:

```python
for line in format_model_status(llm.model_status()):
    print(line)
```

Do not print the API key, Authorization header, prompt text, response text, or
raw provider body.

- [ ] **Step 4: Update configuration and operator documentation**

Keep exactly these model lines in `.env.example`:

```dotenv
AGENTEDU_MODEL=MiniMax-M3
AGENTEDU_MODEL_STRONG=deepseek-v4-pro
```

Document:

- cold-start task routing;
- five-sample threshold before dynamic reordering;
- 20-point success-rate and 2-times latency switch thresholds;
- three-failure circuit opening and 60-second half-open probe;
- 401 terminal behavior;
- failed HTTP attempts consuming `AGENTEDU_BUDGET_CALLS`;
- `/api/model-status` being local, read-only, and sanitized;
- unit tests never using the real key.

Do not reintroduce alternative model examples.

- [ ] **Step 5: Run docs/config residue and targeted tests**

Run:

```powershell
py -3 -m unittest tests.test_dotenv tests.test_doctor tests.test_model_router tests.test_llm_client tests.test_model_status_api -v
rg -n "qwen|AGENTEDU_MODEL_(MID|LIGHT)" .env.example README.md docs/接入大模型.md docs/book/chapter/chapter08.tex core/llm.py core/model_router.py
```

Expected: all tests pass; `rg` returns no matches and therefore exits with code 1.

- [ ] **Step 6: Review the Task 6 diff and request commit authorization**

Run:

```powershell
git diff --check -- .env.example README.md docs/接入大模型.md docs/book/chapter/chapter08.tex evalkit/doctor.py tests/test_dotenv.py tests/test_doctor.py
git diff -- .env.example README.md docs/接入大模型.md docs/book/chapter/chapter08.tex evalkit/doctor.py tests/test_dotenv.py tests/test_doctor.py
```

Stop and ask the user to authorize this commit. After explicit authorization only:

```powershell
git add -- .env.example README.md docs/接入大模型.md docs/book/chapter/chapter08.tex evalkit/doctor.py tests/test_dotenv.py tests/test_doctor.py
git commit -m "docs: explain intelligent model routing"
```

---

### Task 7: Full verification and local runtime handoff

**Files:**
- Verify all files changed in Tasks 1 through 6.
- Do not modify unrelated `AGENTS.md`, `tools/qykw/*`, `tests/test_qykw_*`, `delivery/`, or `docs/research/` changes already present in the worktree.

**Interfaces:**
- Consumes the completed router, real client, server singleton, status API, and documentation.
- Produces a verification record and a restarted local service at `http://127.0.0.1:8000/`.

- [ ] **Step 1: Run syntax compilation**

Run:

```powershell
py -3 -m py_compile core/model_router.py core/llm.py server.py evalkit/doctor.py tests/test_model_router.py tests/test_model_status_api.py
```

Expected: exit code 0 and no output.

- [ ] **Step 2: Run the complete test suite**

Run:

```powershell
py -3 -m unittest discover -s tests
```

Expected: all tests pass with zero failures and zero errors. Test count may exceed the current 593 because the plan adds router and endpoint tests.

- [ ] **Step 3: Check whitespace, secret isolation, and exact model set**

Run:

```powershell
git diff --check
git check-ignore -v .env
rg -n "^AGENTEDU_MODEL" .env.example
rg -n "qwen|AGENTEDU_MODEL_(MID|LIGHT)" .env.example README.md docs/接入大模型.md docs/book/chapter/chapter08.tex core/llm.py core/model_router.py
```

Expected:

- `git diff --check` exits 0;
- `.env` is ignored by the `.gitignore` rule;
- the template prints only `MiniMax-M3` and `deepseek-v4-pro` model lines;
- the final residue search prints nothing and exits 1.

- [ ] **Step 4: Restart the local server and verify public endpoints**

Stop the known local `server.py` session through the active Codex terminal session, then run:

```powershell
py -3 server.py
```

In a second terminal run:

```powershell
$home = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/' -UseBasicParsing
$profiles = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/profiles'
$models = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/model-status'
Write-Output ('HOME_STATUS=' + $home.StatusCode)
Write-Output ('PROFILE_COUNT=' + @($profiles).Count)
Write-Output ('MODEL_MODE=' + $models.mode)
Write-Output ('MODEL_COUNT=' + @($models.models).Count)
```

Expected with an empty local key: `HOME_STATUS=200`, `PROFILE_COUNT=3`, `MODEL_MODE=offline`, and `MODEL_COUNT=0`.

- [ ] **Step 5: Perform real two-model validation only after the user fills `.env` locally**

Do not ask the user to paste the key into chat. After they state that the local key is filled, run:

```powershell
py -3 -m evalkit.doctor
```

Expected:

- authentication passes without printing the key;
- both `MiniMax-M3` and `deepseek-v4-pro` are probed;
- router strategy and per-model health are printed;
- the server is restarted again after the `.env` change;
- `GET /api/model-status` returns `mode=real` and exactly two sanitized model records.

- [ ] **Step 6: Present the final scoped diff and request final commit authorization**

Run:

```powershell
git status --short
git diff --stat -- core/model_router.py core/llm.py server.py evalkit/doctor.py tests/test_model_router.py tests/test_llm_client.py tests/test_model_status_api.py tests/test_dotenv.py tests/test_examiner.py .env.example README.md docs/接入大模型.md docs/book/chapter/chapter08.tex
```

Report separately:

- intelligent-router files changed by this plan;
- earlier `.env` and two-model fallback changes already in progress;
- unrelated user-owned worktree changes left untouched;
- real-model validation status and any credential-dependent blocker.

Do not commit, push, open a PR, or alter history until the user gives an explicit final authorization.
