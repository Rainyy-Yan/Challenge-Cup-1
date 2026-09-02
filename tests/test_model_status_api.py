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

    def test_concurrent_client_initialization_builds_once(self) -> None:
        fake = MockLLM()
        clients = []
        with patch("server.build_llm", return_value=fake) as build:
            threads = [threading.Thread(target=lambda: clients.append(
                server.get_model_client())) for _ in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(clients, [fake] * 12)
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
