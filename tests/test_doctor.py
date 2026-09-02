"""Doctor model-status formatting contract."""

from __future__ import annotations

import unittest

from core.llm import LLMError, RealLLM
from evalkit import doctor
from evalkit.doctor import _safe_error_detail, format_model_status


class TestDoctorModelStatus(unittest.TestCase):
    def test_model_probe_keeps_target_provider_adapter(self) -> None:
        calls: list[str] = []

        class RecordingAdapter:
            def __init__(self, name: str) -> None:
                self.name = name
                self.base_url = f"https://{name}.example"
                self.api_key = f"{name}-test-key"

            def post(self, payload: dict, timeout: int):
                calls.append(self.name)
                return ({
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }, 1)

        source = RealLLM(
            "https://minimax.example", "minimax-test-key", "MiniMax-M3",
            models={"strong": "deepseek-v4-pro"},
            adapters={
                "MiniMax-M3": RecordingAdapter("minimax"),
                "deepseek-v4-pro": RecordingAdapter("deepseek"),
            },
        )
        builder = getattr(doctor, "_build_model_probe", None)
        self.assertIsNotNone(builder, "doctor 必须保留目标模型自己的供应商适配器")

        probe = builder(source, "deepseek-v4-pro", "MiniMax-M3")
        self.assertEqual(probe.run("simplify", "s", "u"), "ok")
        self.assertEqual(calls, ["deepseek"])

    def test_safe_error_detail_does_not_echo_unknown_provider_text(self) -> None:
        canary = "provider-secret-canary-9f4d"
        detail = _safe_error_detail(LLMError(f"provider:http_503:{canary}"))
        self.assertNotIn(canary, detail)

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
