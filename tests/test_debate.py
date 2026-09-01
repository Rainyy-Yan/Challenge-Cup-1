"""交叉验证与辩论 Agent 的单元测试。

重点测三件事：对齐分类对不对、仲裁只看证据不看措辞、拿不准时敢弃用。
第三条最容易在后续迭代中被改坏，因为"少输出内容"看起来像 bug。
"""

import unittest

import config
from agents.audit import AuditAgent
from agents.debate import DebateAgent, similarity
from core.llm import MockLLM
from core.retrieval import Retriever
from core.schema import (Claim, CONSENSUS_ARBITRATED, CONSENSUS_BOTH,
                         CONSENSUS_SINGLE)


class TestSimilarity(unittest.TestCase):
    def test_identical_is_one(self):
        self.assertAlmostEqual(similarity("机器人超程", "机器人超程"), 1.0)

    def test_unrelated_is_low(self):
        s = similarity("减速机润滑脂更换周期", "示教器三位使能开关")
        self.assertLess(s, 0.2)

    def test_empty_is_zero(self):
        self.assertEqual(similarity("", "任意"), 0.0)


class TestDebate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.retriever = Retriever.from_jsonl(config.KB_PATH)
        cls.auditor = AuditAgent(MockLLM(), cls.retriever)

    def setUp(self):
        self.agent = DebateAgent(self.retriever, self.auditor)

    def test_same_claim_same_source_is_agreed(self):
        t = "报警SRVO-005含义为机器人超程。"
        out, log = self.agent.run([Claim(text=t, source_id="KB-017")],
                                  [Claim(text=t, source_id="KB-017")])
        self.assertEqual(log["stats"]["agreed_n"], 1)
        self.assertEqual(out[0].consensus, CONSENSUS_BOTH)
        self.assertEqual(len(out[0].proposed_by), 2)

    def test_unrelated_claims_are_singles(self):
        out, log = self.agent.run(
            [Claim(text="加注润滑脂时必须打开排脂口。", source_id="KB-020")],
            [Claim(text="机器人安全围栏高度不低于1.4米。", source_id="KB-022")])
        self.assertEqual(log["stats"]["single_n"], 2)
        self.assertTrue(all(c.consensus == CONSENSUS_SINGLE for c in out))

    def test_conflict_is_arbitrated_by_evidence(self):
        """同一件事两种说法，正确的一方证据分更高，应当胜出。"""
        right = Claim(text="T1模式下末端移动速度被限制在250毫米每秒以内。",
                      source_id="KB-004")
        wrong = Claim(text="T1模式下末端移动速度被限制在600毫米每秒以内。",
                      source_id="KB-004")
        out, log = self.agent.run([right], [wrong])
        self.assertEqual(log["stats"]["arbitrated_n"], 1)
        self.assertEqual(len(out), 1)
        self.assertIn("250", out[0].text)
        self.assertEqual(out[0].consensus, CONSENSUS_ARBITRATED)

    def test_arbitration_records_the_rival(self):
        """被否决的说法要留档，答辩时要能展示裁判否掉了什么。"""
        right = Claim(text="机器人安全围栏高度不低于1.4米。", source_id="KB-022")
        wrong = Claim(text="机器人安全围栏高度不低于9.9米。", source_id="KB-022")
        out, _ = self.agent.run([right], [wrong])
        self.assertTrue(out[0].rival, "对立说法应当被记录")

    def test_close_evidence_drops_both(self):
        """讲同一件事、措辞不同、证据分咬得很紧时，两条都弃用。

        这条守的是"拿不准就少说"的策略。后续迭代如果有人觉得"弃用太浪费"
        而放松 DEBATE_CONFLICT_MARGIN，这个测试会红。
        """
        # 两条都是改写，谁也不是原文照抄，证据分咬在 0.04 以内
        a = Claim(text="MOVJ为关节插补，末端轨迹无法预知。", source_id="KB-009")
        b = Claim(text="MOVJ属关节插补，末端轨迹难以预料。", source_id="KB-009")
        winner, why = self.agent.arbitrate(a, b)
        self.assertIsNone(winner, f"证据接近应当双方弃用，实际理由：{why}")
        self.assertIn("无法判定", why)

    def test_paraphrase_pairs_via_same_source(self):
        """措辞差异大但引用同一切片的两条，应当被配对而不是各自漏成单方。

        二元组相似度只有 0.49，低于常规配对阈值；靠同源放宽的那一档兜住。
        """
        a = Claim(text="MOVJ为关节插补，末端轨迹无法预知。", source_id="KB-009")
        b = Claim(text="MOVJ属关节插补，末端轨迹难以预料。", source_id="KB-009")
        self.assertLess(similarity(a.text, b.text), config.DEBATE_ALIGN_MIN)
        _, log = self.agent.run([a], [b])
        self.assertEqual(log["stats"]["single_n"], 0,
                         f"同源的两条不应各自成为单方：{log['stats']}")

    def test_numeric_conflict_beats_high_similarity(self):
        """只差一个数字的两条断言，文本相似度接近 1，但必须判为冲突。

        这是本文件最重要的一条测试。原实现按相似度判一致，
        「限速250」和「限速600」相似度 0.95、出处相同，被当成"双方印证"
        直接放行了。数值篡改恰恰是文本最像的一类冲突，只看相似度
        等于把最危险的情形当成最可信的情形。
        """
        a = Claim(text="T1模式下末端移动速度被限制在250毫米每秒以内。",
                  source_id="KB-004")
        b = Claim(text="T1模式下末端移动速度被限制在600毫米每秒以内。",
                  source_id="KB-004")
        self.assertGreater(similarity(a.text, b.text), 0.85)
        out, log = self.agent.run([a], [b])
        self.assertEqual(log["stats"]["agreed_n"], 0)
        self.assertEqual(len(out), 1)
        self.assertIn("250", out[0].text)
        self.assertIn("数值裁决", log["log"]["arbitrated"][0]["why"])

    def test_debate_does_not_replace_audit(self):
        """辩论产出的断言仍然必须过审。两条假断言互相印证也不能放行。"""
        fake = "国家标准规定该项检测每72小时执行一次并留存记录。"
        out, log = self.agent.run([Claim(text=fake, source_id="KB-021")],
                                  [Claim(text=fake, source_id="KB-021")])
        self.assertEqual(log["stats"]["agreed_n"], 1)
        kept, dropped = self.auditor.review(out)
        self.assertEqual(len(kept), 0, "审核必须独立于辩论把这条拦下")

    def test_empty_input_is_safe(self):
        out, log = self.agent.run([], [])
        self.assertEqual(out, [])
        self.assertEqual(log["stats"]["agreed_n"], 0)

    def test_one_side_empty_yields_singles(self):
        out, log = self.agent.run(
            [Claim(text="加注润滑脂时必须打开排脂口。", source_id="KB-020")], [])
        self.assertEqual(log["stats"]["single_n"], 1)


class TestDebateInPipeline(unittest.TestCase):
    def test_experts_see_different_context(self):
        """两位专家必须拿到不同的检索上下文，否则交叉验证没有信息增益。"""
        from orchestrator import Orchestrator, load_profile
        orch = Orchestrator(llm=MockLLM(),
                            retriever=Retriever.from_jsonl(config.KB_PATH))
        diag = orch.diagnoser.run(load_profile("P-C"))
        differs = 0
        for kp in diag.gaps[:6]:
            _, narrow = orch.generator.draft(kp, diag)
            _, wide = orch.generator.draft_wide(kp, diag)
            if [c["id"] for c in narrow] != [c["id"] for c in wide]:
                differs += 1
        self.assertGreater(differs, 0, "宽窄两种检索视角应当至少在部分知识点上不同")


if __name__ == "__main__":
    unittest.main()
