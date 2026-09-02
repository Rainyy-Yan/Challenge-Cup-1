"""模型客户端的健壮性。

接真模型时最常见的三类故障 —— 限流、JSON 模式不支持、模型名写错 ——
在全流程里表现出来都是"生成结果为空"，极难定位。所以在客户端这一层
就要处理掉，并且要有测试盯着，不能等到答辩现场才发现。

用本地假端点测，不联网。
"""

import config
import http.client
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from core.llm import (
    TASK_TIER,
    LLMError,
    ModelCallError,
    OpenAIAdapter,
    RealLLM,
    classify_http_error,
)
from core.model_router import ModelSpec, SmartModelRouter

STATE = {"hits": 0, "mode": "normal"}


class TestProviderErrorClassification(unittest.TestCase):
    def test_auth_is_terminal(self) -> None:
        self.assertEqual(classify_http_error(401, "unauthorized"), "auth")

    def test_retryable_provider_errors_are_classified(self) -> None:
        self.assertEqual(classify_http_error(429, "rate limit"), "rate_limit")
        for status in (500, 501, 502, 503, 504, 505, 507, 599):
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


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json_response(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _error(self, status: int, payload: dict) -> None:
        self._json_response(status, payload)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        STATE["hits"] += 1
        STATE.setdefault("request_times", []).append(time.monotonic())
        STATE.setdefault("last", {})
        STATE["last"] = body
        STATE.setdefault("models", []).append(body.get("model"))
        mode = STATE["mode"]
        if mode == "no_json" and "response_format" in body:
            return self._error(
                400, {"error": {"message": "response_format is not supported"}})
        if mode == "flaky" and STATE["hits"] % 3 != 0:
            return self._error(429, {"error": {"message": "rate limited"}})
        if mode == "auth":
            return self._error(401, {"error": {"message": "unauthorized"}})
        if mode in {"adapter_long_error", "adapter_delayed_error"}:
            if mode == "adapter_delayed_error":
                time.sleep(0.15)
            return self._error(503, {"error": {"message": "x" * 400}})
        if mode == "sensitive_error":
            return self._error(503, {
                "error": {"message": "provider-secret-canary-9f4d"},
            })
        if (mode == "truncated_primary"
                and body.get("model") == "deepseek-v4-pro"):
            raw = b'{"choices":'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw) + 50))
            self.end_headers()
            self.wfile.write(raw)
            self.close_connection = True
            return
        if mode == "bad_request":
            return self._error(400, {"error": {"message": "invalid messages"}})
        if (mode == "model_not_found"
                and body.get("model") == "deepseek-v4-pro"):
            return self._error(400, {"error": {"code": "model_not_found"}})
        if mode == "primary_unavailable" and body.get("model") == "deepseek-v4-pro":
            return self._error(503, {"error": {"message": "upstream unavailable"}})
        if (mode == "malformed_primary"
                and body.get("model") == "deepseek-v4-pro"):
            return self._json_response(200, {"choices": [], "usage": {}})
        if (mode == "array_primary"
                and body.get("model") == "deepseek-v4-pro"):
            return self._json_response(200, [])
        if (mode == "malformed_usage_primary"
                and body.get("model") == "deepseek-v4-pro"):
            return self._json_response(200, {
                "choices": [{"message": {"content": '{"ok":true}'}}],
                "usage": {"prompt_tokens": None, "completion_tokens": "NaN"},
            })
        if (mode == "null_usage_primary"
                and body.get("model") == "deepseek-v4-pro"):
            return self._json_response(200, {
                "choices": [{"message": {"content": '{"ok":true}'}}],
                "usage": None,
            })
        if (mode == "missing_usage_primary"
                and body.get("model") == "deepseek-v4-pro"):
            return self._json_response(200, {
                "choices": [{"message": {"content": '{"ok":true}'}}],
            })
        if mode == "delayed_success":
            time.sleep(0.20)
        if mode == "empty_content":
            return self._json_response(200, {
                "choices": [{"message": {"content": "  \n\t"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 25},
            })
        if mode == "cache_json_mode":
            user_content = body.get("messages", [{}, {}])[-1].get("content", "")
            content = (
                "json"
                if "response_format" in body or "请只输出合法 JSON" in user_content
                else "plain"
            )
            return self._json_response(200, {
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 25},
            })
        return self._json_response(200, {
            "choices": [{"message": {"content": '{"ok":true}'}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 25},
        })


class TestOpenAIAdapterErrors(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 8395), _Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        time.sleep(0.1)
        cls.adapter = OpenAIAdapter("http://127.0.0.1:8395", "k")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        STATE["hits"] = 0
        STATE["mode"] = "normal"

    def test_http_error_summary_is_capped_at_300_characters(self):
        STATE["mode"] = "adapter_long_error"
        with self.assertRaises(ModelCallError) as raised:
            self.adapter.post({"model": "m"}, timeout=5)
        self.assertLessEqual(len(raised.exception.summary), 300)

    def test_http_error_summary_never_contains_provider_body(self):
        STATE["mode"] = "sensitive_error"
        with self.assertRaises(ModelCallError) as raised:
            self.adapter.post({"model": "m"}, timeout=5)
        self.assertNotIn("provider-secret-canary-9f4d", raised.exception.summary)
        self.assertEqual(raised.exception.kind, "provider")
        self.assertEqual(raised.exception.status, 503)

    def test_incomplete_http_body_is_sanitized_network_error(self):
        with patch(
            "urllib.request.urlopen",
            side_effect=http.client.IncompleteRead(b"partial-secret-canary", 50),
        ), self.assertRaises(ModelCallError) as raised:
            OpenAIAdapter("https://unit.invalid/v1", "k").post(
                {"model": "m"}, timeout=5)
        self.assertEqual(raised.exception.kind, "network")
        self.assertNotIn("partial-secret-canary", raised.exception.summary)

    def test_http_error_latency_includes_delayed_body_read(self):
        STATE["mode"] = "adapter_delayed_error"
        with self.assertRaises(ModelCallError) as raised:
            self.adapter.post({"model": "m"}, timeout=5)
        self.assertGreaterEqual(raised.exception.latency_ms, 100)


class TestRealLLMResilience(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 8393), _Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        time.sleep(0.2)
        cls.url = "http://127.0.0.1:8393"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        STATE["hits"] = 0
        STATE["mode"] = "normal"
        STATE["models"] = []
        STATE["request_times"] = []

    def _llm(self, **kw):
        # 这组测试量的是网络行为（重试、降级、计费），必须关掉缓存，
        # 否则第二次相同请求会命中缓存、根本不发出去，测到的就不是网络了。
        kw.setdefault("cache", False)
        kw.setdefault("models", {"strong": "strong-model"})
        model = kw.pop("model", "default-model")
        return RealLLM(self.url, "k", model, timeout=5, **kw)

    def test_normal_call(self):
        self.assertIn("ok", self._llm().run("verify", "s", "u"))

    def test_task_routing_picks_tier_model(self):
        llm = self._llm()
        self.assertEqual(llm.model_for("make_item"), "strong-model")
        self.assertEqual(llm.model_for("synthesize"), "default-model")

    def test_item_generation_uses_strongest_tier(self):
        """命题出错代价最高，必须走强模型。"""
        self.assertEqual(TASK_TIER["make_item"], "strong")

    def test_json_mode_downgrades_once_and_remembers(self):
        """端点不支持 response_format 时降级，且不再反复白试。"""
        STATE["mode"] = "no_json"
        router = SmartModelRouter((
            ModelSpec("strong-model", "strong", 5, True),
            ModelSpec("default-model", "default", 5, True),
        ))
        llm = self._llm(router=router)
        self.assertIn("ok", llm.run("verify", "只输出JSON", "u", json_mode=True))
        self.assertFalse(llm._json_mode_ok["default-model"])
        before = STATE["hits"]
        llm.run("verify", "只输出JSON", "u", json_mode=True)
        self.assertEqual(STATE["hits"] - before, 1, "降级后不应再试 response_format")

    def test_downgrade_puts_constraint_in_prompt(self):
        STATE["mode"] = "no_json"
        router = SmartModelRouter((
            ModelSpec("strong-model", "strong", 5, True),
            ModelSpec("default-model", "default", 5, True),
        ))
        llm = self._llm(router=router)
        llm.run("verify", "系统提示", "u", json_mode=True)
        self.assertIn("请只输出合法 JSON", STATE["last"]["messages"][1]["content"])

    def test_retries_on_rate_limit(self):
        STATE["mode"] = "flaky"
        llm = self._llm(retries=4)
        self.assertIn("ok", llm.run("simplify", "s", "u"))
        self.assertGreaterEqual(STATE["hits"], 2)

    def test_auth_error_is_not_retried(self):
        """401 重试多少次都没用，快速失败比慢慢磨好。"""
        STATE["mode"] = "auth"
        llm = self._llm(retries=3)
        with self.assertRaises(LLMError):
            llm.run("simplify", "s", "u")
        self.assertEqual(STATE["hits"], 1)

    def test_strong_model_failure_falls_back_to_default_model(self):
        STATE["mode"] = "primary_unavailable"
        llm = self._llm(
            model="MiniMax-M3",
            models={"strong": "deepseek-v4-pro"},
            retries=1,
        )

        self.assertIn("ok", llm.run("make_item", "s", "u"))
        self.assertEqual(STATE["models"], ["deepseek-v4-pro", "MiniMax-M3"])
        self.assertEqual(llm.stats()["fallbacks"], 1)
        self.assertEqual(llm.stats()["by_model"]["MiniMax-M3"]["calls"], 1)

    def test_provider_error_retries_then_falls_back(self):
        STATE["mode"] = "primary_unavailable"
        llm = self._llm(
            model="MiniMax-M3",
            models={"strong": "deepseek-v4-pro"},
            retries=2,
        )
        self.assertIn("ok", llm.run("make_item", "s", "u"))
        self.assertEqual(
            STATE["models"],
            ["deepseek-v4-pro", "deepseek-v4-pro", "MiniMax-M3"],
        )

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

    def test_top_level_array_is_recorded_as_invalid_then_falls_back(self) -> None:
        STATE["mode"] = "array_primary"
        llm = self._llm(
            model="MiniMax-M3",
            models={"strong": "deepseek-v4-pro"},
            retries=1,
        )
        self.assertIn("ok", llm.run("make_item", "s", "u"))
        self.assertEqual(STATE["models"], ["deepseek-v4-pro", "MiniMax-M3"])
        primary = llm.model_status()["models"][0]
        self.assertEqual(primary["failures"], 1)
        self.assertEqual(primary["last_error"]["type"], "invalid_response")

    def test_malformed_usage_is_recorded_as_invalid_then_falls_back(self) -> None:
        STATE["mode"] = "malformed_usage_primary"
        llm = self._llm(
            model="MiniMax-M3",
            models={"strong": "deepseek-v4-pro"},
            retries=1,
        )
        self.assertIn("ok", llm.run("make_item", "s", "u"))
        self.assertEqual(STATE["models"], ["deepseek-v4-pro", "MiniMax-M3"])
        primary = llm.model_status()["models"][0]
        self.assertEqual(primary["failures"], 1)
        self.assertEqual(primary["last_error"]["type"], "invalid_response")

    def test_null_usage_is_recorded_as_invalid_then_falls_back(self) -> None:
        STATE["mode"] = "null_usage_primary"
        llm = self._llm(
            model="MiniMax-M3",
            models={"strong": "deepseek-v4-pro"},
            retries=1,
        )
        self.assertIn("ok", llm.run("make_item", "s", "u"))
        self.assertEqual(STATE["models"], ["deepseek-v4-pro", "MiniMax-M3"])
        primary = llm.model_status()["models"][0]
        self.assertEqual(primary["failures"], 1)
        self.assertEqual(primary["last_error"]["type"], "invalid_response")

    def test_missing_usage_is_allowed_as_zero_tokens(self) -> None:
        STATE["mode"] = "missing_usage_primary"
        llm = self._llm(
            model="MiniMax-M3",
            models={"strong": "deepseek-v4-pro"},
            retries=1,
        )
        self.assertIn("ok", llm.run("make_item", "s", "u"))
        self.assertEqual(STATE["models"], ["deepseek-v4-pro"])
        self.assertEqual(llm.stats()["tokens_in"], 0)
        self.assertEqual(llm.stats()["tokens_out"], 0)

    def test_blank_content_is_invalid_and_falls_back(self) -> None:
        STATE["mode"] = "empty_content"
        llm = self._llm(
            model="MiniMax-M3",
            models={"strong": "deepseek-v4-pro"},
            retries=1,
        )
        with self.assertRaises(LLMError):
            llm.run("make_item", "s", "u")
        models = llm.model_status()["models"]
        self.assertEqual([item["failures"] for item in models], [1, 1])
        self.assertTrue(all(
            item["last_error"]["type"] == "invalid_response" for item in models
        ))

    def test_provider_body_is_absent_from_final_error_and_status(self) -> None:
        STATE["mode"] = "sensitive_error"
        llm = self._llm(
            model="MiniMax-M3",
            models={"strong": "deepseek-v4-pro"},
            retries=1,
        )
        with self.assertRaises(LLMError) as raised:
            llm.run("make_item", "s", "u")
        self.assertNotIn("provider-secret-canary-9f4d", str(raised.exception))
        self.assertNotIn(
            "provider-secret-canary-9f4d",
            json.dumps(llm.model_status(), ensure_ascii=False),
        )

    def test_unexpected_adapter_exception_releases_half_open_probe(self) -> None:
        class ExplodingAdapter:
            def post(self, payload: dict, timeout: int):
                raise RuntimeError("unexpected adapter failure")

        router = SmartModelRouter((
            ModelSpec("deepseek-v4-pro", "strong", 5, False),
            ModelSpec("MiniMax-M3", "default", 5, False),
        ))
        router.runtime["deepseek-v4-pro"].state = "half_open"
        llm = self._llm(
            model="MiniMax-M3",
            models={"strong": "deepseek-v4-pro"},
            retries=1,
            router=router,
            adapter=ExplodingAdapter(),
        )
        with self.assertRaisesRegex(RuntimeError, "unexpected adapter"):
            llm.run("make_item", "s", "u")
        self.assertTrue(router.begin_attempt("deepseek-v4-pro"))

    def test_truncated_response_falls_back_and_records_network_metrics(self) -> None:
        STATE["mode"] = "truncated_primary"
        llm = self._llm(
            model="MiniMax-M3",
            models={"strong": "deepseek-v4-pro"},
            retries=1,
        )
        self.assertIn("ok", llm.run("make_item", "s", "u"))
        self.assertEqual(STATE["models"], ["deepseek-v4-pro", "MiniMax-M3"])
        status = llm.model_status()
        self.assertEqual(status["models"][0]["last_error"]["type"], "network")
        self.assertEqual(status["router"]["fallbacks"], 1)

    def test_static_cache_key_survives_open_primary_breaker(self) -> None:
        STATE["mode"] = "primary_unavailable"
        router = SmartModelRouter((
            ModelSpec("deepseek-v4-pro", "strong", 5, False),
            ModelSpec("MiniMax-M3", "default", 5, False),
        ), failure_threshold=1)
        llm = self._llm(
            model="MiniMax-M3",
            models={"strong": "deepseek-v4-pro"},
            retries=1,
            cache=True,
            router=router,
        )
        first = llm.run("make_item", "s", "u")
        hits = STATE["hits"]
        STATE["mode"] = "normal"
        self.assertEqual(llm.run("make_item", "s", "u"), first)
        self.assertEqual(STATE["hits"], hits)
        self.assertEqual(llm.stats()["cache_hits"], 1)

    def test_concurrent_same_key_shares_one_http_result(self) -> None:
        STATE["mode"] = "delayed_success"
        llm = self._llm(cache=True)
        start = threading.Barrier(3)
        results: list[str] = []
        errors: list[Exception] = []

        def call() -> None:
            start.wait()
            try:
                results.append(llm.run("verify", "s", "same"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        workers = [threading.Thread(target=call) for _ in range(2)]
        for worker in workers:
            worker.start()
        start.wait()
        for worker in workers:
            worker.join(3)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        self.assertEqual(results, ['{"ok":true}', '{"ok":true}'])
        self.assertEqual(STATE["hits"], 1)
        self.assertEqual(llm.stats()["cache_hits"], 1)

    def test_concurrent_same_key_terminal_failure_wakes_waiters(self) -> None:
        STATE["mode"] = "bad_request"
        llm = self._llm(cache=True)
        start = threading.Barrier(3)
        errors: list[Exception] = []

        def call() -> None:
            start.wait()
            try:
                llm.run("verify", "s", "same")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        workers = [threading.Thread(target=call) for _ in range(2)]
        for worker in workers:
            worker.start()
        start.wait()
        for worker in workers:
            worker.join(3)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(STATE["hits"], 1)
        self.assertEqual(len(errors), 2)
        self.assertTrue(all(isinstance(exc, LLMError) for exc in errors))

    def test_concurrent_budget_reservation_allows_only_one_http_attempt(self) -> None:
        STATE["mode"] = "delayed_success"
        llm = self._llm(cache=False, budget_calls=1)
        start = threading.Barrier(3)
        results: list[str] = []
        errors: list[Exception] = []

        def call(user: str) -> None:
            start.wait()
            try:
                results.append(llm.run("verify", "s", user))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        workers = [threading.Thread(target=call, args=(user,)) for user in ("a", "b")]
        for worker in workers:
            worker.start()
        start.wait()
        for worker in workers:
            worker.join(3)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(STATE["hits"], 1)
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("调用上限", str(errors[0]))

    def test_concurrent_rpm_reservation_keeps_minimum_gap(self) -> None:
        llm = self._llm(cache=False, rpm=300)
        start = threading.Barrier(3)
        errors: list[Exception] = []

        def call(user: str) -> None:
            start.wait()
            try:
                llm.run("verify", "s", user)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        workers = [threading.Thread(target=call, args=(user,)) for user in ("a", "b")]
        for worker in workers:
            worker.start()
        start.wait()
        for worker in workers:
            worker.join(3)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        self.assertEqual(len(STATE["request_times"]), 2)
        self.assertGreaterEqual(
            STATE["request_times"][1] - STATE["request_times"][0], 0.16)

    def test_auth_error_does_not_try_fallback_model(self):
        STATE["mode"] = "auth"
        llm = self._llm(
            model="MiniMax-M3",
            models={"strong": "deepseek-v4-pro"},
            retries=3,
        )

        with self.assertRaises(LLMError):
            llm.run("make_item", "s", "u")

        self.assertEqual(STATE["models"], ["deepseek-v4-pro"])
        self.assertEqual(llm.stats()["fallbacks"], 0)

    def test_usage_and_cost_accounted(self):
        llm = self._llm(price_in=1.0, price_out=2.0)
        llm.run("verify", "s", "u")
        st = llm.stats()
        self.assertEqual(st["tokens_in"], 100)
        self.assertEqual(st["tokens_out"], 25)
        self.assertAlmostEqual(st["cost_cny"], 100 / 1e6 * 1 + 25 / 1e6 * 2, places=6)
        self.assertEqual(st["by_task"]["verify"]["calls"], 1)
        self.assertEqual(st["http_attempts"], 1)
        self.assertEqual(st["by_model"]["default-model"]["calls"], 1)
        self.assertEqual(st["router"]["all_models_failed"], 0)
        self.assertEqual(st["models"][1]["id"], "default-model")
        self.assertFalse(st["json_mode"])
        status = llm.model_status()
        self.assertEqual(status["mode"], "real")
        self.assertEqual(status["strategy"], "task-aware-health-adaptive")

    def test_failure_is_counted(self):
        STATE["mode"] = "auth"
        llm = self._llm(retries=1)
        with self.assertRaises(LLMError):
            llm.run("simplify", "s", "u")
        self.assertEqual(llm.stats()["failures"], 1)


class TestBuildLLM(unittest.TestCase):

    def test_legacy_three_argument_constructor_remains_available(self):
        llm = RealLLM("https://example.invalid/v1", "k", "legacy-model")
        self.assertEqual(llm.model, "legacy-model")
        self.assertIn("legacy-model", llm.router.specs)

    def test_no_key_falls_back_to_mock(self):
        import os
        from core.llm import MockLLM, build_llm

        with patch.dict(os.environ, {}, clear=True), \
                patch("core.llm._load_project_env"):
            self.assertIsInstance(build_llm(), MockLLM)


class TestFreeTierGuards(unittest.TestCase):
    """免费额度下的三条保护：限速、缓存、调用上限。

    免费档的真实约束不是"能不能用"，是每分钟请求数和总额度。
    这三条没有的话，一次演示很容易把额度打光，而且失败方式很难看。
    """

    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 8394), _Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        time.sleep(0.2)
        cls.url = "http://127.0.0.1:8394"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        STATE["hits"] = 0
        STATE["mode"] = "normal"

    def _llm(self, **kw):
        return RealLLM(
            self.url,
            "k",
            kw.pop("model", "MiniMax-M3"),
            timeout=5,
            models=kw.pop("models", {"strong": "deepseek-v4-pro"}),
            **kw,
        )

    def test_cache_avoids_duplicate_requests(self):
        llm = self._llm(cache=True)
        for _ in range(5):
            llm.run("draft_claims", "固定系统提示", "同一输入")
        self.assertEqual(STATE["hits"], 1, "相同请求应只发一次")
        self.assertEqual(llm.stats()["cache_hits"], 4)

    def test_cache_distinguishes_different_input(self):
        llm = self._llm(cache=True)
        for i in range(3):
            llm.run("draft_claims", "固定系统提示", f"输入{i}")
        self.assertEqual(STATE["hits"], 3)

    def test_cache_separates_plain_and_json_modes(self):
        STATE["mode"] = "cache_json_mode"
        llm = self._llm(cache=True)
        plain = llm.run("verify", "s", "same", json_mode=False)
        structured = llm.run("verify", "s", "same", json_mode=True)
        self.assertEqual((plain, structured), ("plain", "json"))
        self.assertEqual(STATE["hits"], 2)

    def test_cache_can_be_disabled(self):
        llm = self._llm(cache=False)
        for _ in range(3):
            llm.run("verify", "s", "u")
        self.assertEqual(STATE["hits"], 3)

    def test_throttle_spaces_out_requests(self):
        llm = self._llm(rpm=120, cache=False)
        t0 = time.monotonic()
        for i in range(3):
            llm.run("verify", "s", f"u{i}")
        # rpm=120 → 间隔 0.5s，三次至少 1.0s
        self.assertGreaterEqual(time.monotonic() - t0, 0.9)

    def test_budget_stops_calling_and_flags(self):
        llm = self._llm(budget_calls=2, cache=False)
        llm.run("verify", "s", "a")
        llm.run("verify", "s", "b")
        with self.assertRaises(LLMError):
            llm.run("verify", "s", "c")
        self.assertTrue(llm.stats()["budget_hit"])
        self.assertEqual(STATE["hits"], 2, "超限后不应再发请求")

    def test_item_generation_degrades_instead_of_crashing(self):
        """额度耗尽时命题必须返回 None 回退题库，而不是把链路炸掉。

        命题是增强项，没有它系统照样能用固定题库跑完。
        演示现场额度用完还能继续演，和当场白屏，是两回事。
        """
        import json as _json
        from agents.examiner import ExaminerAgent
        from core.retrieval import Retriever
        llm = self._llm(budget_calls=0, cache=False)
        llm.budget_calls = 1
        R = Retriever.from_jsonl(config.KB_PATH)
        kps = _json.loads(config.KP_PATH.read_text(encoding="utf-8"))["points"]
        ex = ExaminerAgent(llm, R, {k["id"]: k for k in kps})
        results = [ex.make_item(k["id"], 3) for k in kps[:3]]
        self.assertTrue(any(r is None for r in results))
        self.assertGreaterEqual(ex.llm_errors, 1)

    def test_intake_still_works_without_model(self):
        """自述解析在模型不可用时必须仍有结果 —— 规则通路本来就在。"""
        from agents.intake import IntakeAgent
        llm = self._llm(budget_calls=1, cache=False)
        ia = IntakeAgent(llm)
        for _ in range(3):
            bg = ia.parse("机械专业大三，实操大概40小时")
            self.assertEqual(bg["education"], "本科")
            self.assertEqual(bg["hands_on_hours"], 40)
