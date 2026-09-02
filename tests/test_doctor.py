"""Doctor model-status formatting contract."""

from __future__ import annotations

import unittest

from core.llm import LLMError
from evalkit.doctor import _safe_error_detail, format_model_status


class TestDoctorModelStatus(unittest.TestCase):
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
