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

    def test_explicit_non_production_specs_remain_injectable(self) -> None:
        specs = build_default_specs("test-default", "test-strong", 5, 0.0, 0.0)
        router = SmartModelRouter(specs, clock=self.clock)
        self.assertEqual(
            [spec.model_id for spec in router.ordered_candidates("make_item")],
            ["test-strong", "test-default"],
        )


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

    def test_half_open_probe_precedes_dynamic_score_and_healthy_model_stays_available(self) -> None:
        router = SmartModelRouter(
            build_default_specs(
                "MiniMax-M3", "deepseek-v4-pro", 60, 0.0, 0.0),
            clock=self.clock,
            failure_threshold=5,
            cooldown_seconds=60.0,
        )
        for _ in range(5):
            router.record_failure("deepseek-v4-pro", "provider", 503, 100)
            router.record_success("MiniMax-M3", 80, 10, 5)
        self.clock.advance(60.0)
        for _ in range(5):
            router.record_success("MiniMax-M3", 80, 10, 5)

        ordered = [item.model_id for item in router.ordered_candidates("make_item")]
        self.assertEqual(ordered, ["deepseek-v4-pro", "MiniMax-M3"])
        self.assertTrue(router.begin_attempt("deepseek-v4-pro"))
        self.assertFalse(router.begin_attempt("deepseek-v4-pro"))
        self.assertTrue(router.begin_attempt("MiniMax-M3"))

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
        ids = [spec.model_id for spec in self.router.ordered_candidates("make_item")]
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


if __name__ == "__main__":
    unittest.main()
