"""协同调度逻辑的单元测试。

榜题明确点名要测"多智能体协同调度逻辑"，这个文件就是对着那句话写的。
重点不是覆盖率数字，是把每条状态转移边都钉住，后面加功能时改坏流程会立刻红。
"""

import unittest

from core.llm import MockLLM
from core.retrieval import Retriever
import config
from orchestrator import (STATES, TRANSITIONS, IllegalTransition, Orchestrator,
                          load_profile)


class TestTransitionTable(unittest.TestCase):
    def test_every_state_declared(self):
        for state in STATES:
            self.assertIn(state, TRANSITIONS, f"{state} 缺少转移声明")

    def test_no_dangling_target(self):
        for src, targets in TRANSITIONS.items():
            for t in targets:
                self.assertIn(t, STATES, f"{src} -> {t} 指向了未声明的状态")

    def test_done_is_terminal(self):
        self.assertEqual(TRANSITIONS["DONE"], set())

    def test_audit_can_loop_back_to_revise(self):
        self.assertIn("REVISE", TRANSITIONS["AUDIT"])
        self.assertIn("AUDIT", TRANSITIONS["REVISE"])


class TestRunPath(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.retriever = Retriever.from_jsonl(config.KB_PATH)

    def _orch(self, inject=0.0):
        return Orchestrator(llm=MockLLM(hallucination_rate=inject),
                            retriever=self.retriever)

    def test_happy_path_reaches_ready(self):
        orch = self._orch()
        session = orch.run(load_profile("P-A"))
        self.assertEqual(orch.state, "READY")
        self.assertGreater(len(session.events), 0)

    def test_all_emitted_transitions_are_legal(self):
        orch = self._orch(inject=1.0)
        session = orch.run(load_profile("P-C"))
        orch.feedback(session, session.path[0], [False, False, False, True])
        prev = "INIT"
        for e in session.events:
            self.assertIn(e.state, TRANSITIONS[prev],
                          f"非法转移 {prev} -> {e.state} (seq={e.seq})")
            prev = e.state

    def test_feedback_rejected_when_not_ready(self):
        orch = self._orch()
        session = orch.run(load_profile("P-A"))
        orch.state = "GENERATE"
        with self.assertRaises(IllegalTransition):
            orch.feedback(session, session.path[0], [True, True])

    def test_illegal_jump_raises(self):
        orch = self._orch()
        session = orch.run(load_profile("P-A"))
        with self.assertRaises(IllegalTransition):
            orch._goto("DIAGNOSE", session, "test", "应当被拒绝")

    def test_revise_round_is_capped(self):
        """注入率拉满时重写轮数不能失控，否则会把 token 烧干。"""
        orch = self._orch(inject=1.0)
        session = orch.run(load_profile("P-C"))
        per_kp: dict[str, int] = {}
        for e in session.events:
            if e.state == "REVISE":
                kp = e.detail.get("kp", "?")
                per_kp[kp] = per_kp.get(kp, 0) + 1
        for kp, rounds in per_kp.items():
            self.assertLessEqual(rounds, config.MAX_REVISE_ROUNDS,
                                 f"{kp} 重写了 {rounds} 轮")

    def test_three_resource_kinds_per_kp(self):
        orch = self._orch()
        session = orch.run(load_profile("P-B"))
        for kp in session.path:
            kinds = {r.kind for r in session.resources if r.kp == kp}
            self.assertEqual(kinds, {"lecture", "sop", "quiz"})

    def test_events_carry_agent_and_timing(self):
        orch = self._orch()
        session = orch.run(load_profile("P-A"))
        for e in session.events:
            self.assertTrue(e.agent)
            self.assertGreaterEqual(e.ms, 0)


if __name__ == "__main__":
    unittest.main()
