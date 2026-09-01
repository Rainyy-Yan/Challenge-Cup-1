"""题目质量：结构瑕疵与实测标定。

命题闸门管"题对不对"，这里管"题好不好用"。两者不能互相替代 ——
一道完全正确的题也可能测不出任何东西。
"""

import random
import unittest

import config
from core.itemquality import (calibrate, dead_options, p_value,
                              point_biserial, position_bias, structural,
                              summarize)


def item(**kw):
    base = {"id": "T-1", "kp": "KP-01", "level": 3,
            "stem": "关于工业机器人本体构成，下列说法正确的是",
            "options": ["由基座与六个关节轴组成", "由三个直线轴组成",
                        "由两个回转轴组成", "由四个平动轴组成"],
            "answer": 0}
    base.update(kw)
    return base


class TestStructural(unittest.TestCase):

    def test_clean_item_scores_high(self):
        rep = structural(item())
        self.assertTrue(rep.usable)
        self.assertGreaterEqual(rep.score, 85)

    def test_length_clue_is_flagged(self):
        rep = structural(item(options=[
            "由基座、腰部回转轴、大臂、小臂、腕部三轴和末端法兰共同组成",
            "由三轴组成", "由两轴组成", "由四轴组成"], answer=0))
        self.assertIn("LENCLUE", [f.code for f in rep.flaws])

    def test_length_rule_skips_numeric_options(self):
        """纯数值选项的长度反映量级不是正确性。"""
        rep = structural(item(
            stem="减速机润滑脂更换周期为多少小时",
            options=["10000", "500", "800", "200"], answer=0))
        self.assertNotIn("LENCLUE", [f.code for f in rep.flaws])

    def test_catchall_option_is_flagged(self):
        rep = structural(item(options=["由基座与六个关节轴组成", "由三个直线轴组成",
                                       "由两个回转轴组成", "以上都对"], answer=0))
        self.assertIn("CATCHALL", [f.code for f in rep.flaws])

    def test_duplicate_distractors_are_flagged(self):
        """两个几乎一样的干扰项等于少一个选项，实际蒙对率高于名义值。"""
        rep = structural(item(options=["由基座与六个关节轴组成",
                                       "由三个直线轴构成", "由三个直线轴组成",
                                       "由四个平动轴组成"], answer=0))
        self.assertIn("DUPDISTRACT", [f.code for f in rep.flaws])

    def test_absolute_wording_in_distractors(self):
        rep = structural(item(options=["由基座与六个关节轴组成",
                                       "所有机器人都由三轴组成",
                                       "任何机器人都绝不含回转轴",
                                       "由四个平动轴组成"], answer=0))
        self.assertIn("ABSOLUTE", [f.code for f in rep.flaws])

    def test_short_stem_is_flagged(self):
        rep = structural(item(stem="正确的是"))
        self.assertIn("SHORTSTEM", [f.code for f in rep.flaws])

    def test_malformed_item_is_unusable(self):
        self.assertFalse(structural(item(answer=9)).usable)
        self.assertFalse(structural(item(options=["a", "b"])).usable)

    def test_far_distractors_need_chunk_context(self):
        body = "工业机器人本体由基座、腰部回转轴、大臂、小臂、腕部三轴和末端法兰组成。"
        rep = structural(item(options=["由基座与六个关节轴组成", "使用柴油发动机驱动",
                                       "需要定期更换轮胎", "由四个平动轴组成"],
                              answer=0), body)
        self.assertIn("FARDISTRACT", [f.code for f in rep.flaws])


class TestPositionBias(unittest.TestCase):

    def test_skewed_bank_is_detected(self):
        items = [item(id=f"Q{i}", answer=1) for i in range(20)]
        self.assertTrue(position_bias(items)["skewed"])

    def test_balanced_bank_is_not_flagged(self):
        items = [item(id=f"Q{i}", answer=i % 4) for i in range(20)]
        self.assertFalse(position_bias(items)["skewed"])

    def test_real_bank_is_balanced(self):
        """题库自身必须过这一关。

        这条测试是有来由的：第一版题库 47 道题里 38 道答案在 B，
        一路选 B 能得 81 分，整套测评的测量效力等于零。
        """
        import json
        items = json.loads(config.PRETEST_PATH.read_text(encoding="utf-8"))["items"]
        bias = position_bias(items)
        self.assertFalse(bias["skewed"], bias["detail"])


class TestCalibration(unittest.TestCase):

    def test_p_value_direction(self):
        """p 值越大题越简单 —— 与直觉相反，容易读错。"""
        self.assertAlmostEqual(p_value([True] * 8 + [False] * 2), 0.8)

    def test_discrimination_needs_enough_samples(self):
        self.assertIsNone(point_biserial([True, False], [1.0, 0.0]))

    def test_discrimination_is_none_when_all_same(self):
        self.assertIsNone(point_biserial([True] * 10, [0.5] * 10))

    def test_good_item_has_positive_discrimination(self):
        rng = random.Random(3)
        ability = [rng.random() for _ in range(80)]
        correct = [a > 0.5 if rng.random() > 0.2 else a <= 0.5 for a in ability]
        r = point_biserial(correct, ability)
        self.assertIsNotNone(r)
        self.assertGreater(r, 0.3)

    def test_wrong_answer_key_shows_negative_discrimination(self):
        """答案标错的题，水平高的人反而更容易做错，区分度为负。

        这是本项目里最重要的一道事后防线：生成题的答案是模型断言的，
        标错了不会有任何环节报错，只有区分度看得出来。
        """
        rng = random.Random(11)
        ability = [rng.random() for _ in range(120)]
        recs = []
        for a in ability:
            picks_truth = rng.random() < max(0.05, min(0.95, a))
            recs.append({"choice": 1 if picks_truth else rng.choice([0, 2, 3]),
                         "total": a})
        wrong_key = item(answer=2)          # 真正对的是 1，这里标成了 2
        rep = calibrate(wrong_key, recs)
        self.assertIsNotNone(rep.discrimination)
        self.assertLess(rep.discrimination, 0)
        self.assertIn("NEGDISC", [f.code for f in rep.flaws])
        self.assertFalse(rep.usable, "区分度为负的题必须判为不可用")

    def test_too_easy_item_is_flagged(self):
        recs = [{"choice": 0, "total": 0.5} for _ in range(20)]
        rep = calibrate(item(), recs)
        self.assertIn("TOOEASY", [f.code for f in rep.flaws])

    def test_dead_option_detected(self):
        self.assertEqual(dead_options([0, 1, 0, 1] * 5, 4, 0), [2, 3])

    def test_dead_option_needs_enough_samples(self):
        self.assertEqual(dead_options([0, 1], 4, 0), [])


class TestSummary(unittest.TestCase):

    def test_summary_reports_unusable(self):
        reps = [structural(item()), structural(item(id="X", answer=9))]
        s = summarize(reps)
        self.assertEqual(s["unusable"], 1)
        self.assertIn("X", s["unusable_ids"])

    def test_empty_summary_is_safe(self):
        self.assertEqual(summarize([])["n"], 0)


class TestKeyBalance(unittest.TestCase):
    """整套题的答案分布。单题看不出来，成套才看得出。"""

    def test_all_same_key_is_flagged(self):
        from core.itemquality import key_balance
        items = [{"answer": True} for _ in range(6)]
        self.assertTrue(key_balance(items)["skewed"])

    def test_balanced_keys_pass(self):
        from core.itemquality import key_balance
        items = [{"answer": i % 2 == 0} for i in range(8)]
        self.assertFalse(key_balance(items)["skewed"])

    def test_too_few_items_no_verdict(self):
        from core.itemquality import key_balance
        self.assertFalse(key_balance([{"answer": True}] * 2)["skewed"])

    def test_generated_quiz_has_both_keys(self):
        """判断题必须有真有假。

        早期版本每道判断题的答案都是 true —— 题干由已过审的断言直接生成，
        自然全是正命题。学习者一路点「正确」就是满分，这份测评一点信息都没有。
        比选项位置偏斜更糟：位置偏斜至少还有 25% 的蒙对下限，答案全同是 100%。
        """
        from core.itemquality import key_balance
        from orchestrator import Orchestrator, load_profile
        orch = Orchestrator()
        session = orch.run(load_profile("P-C"), max_kp=4)
        items = [it for r in session.resources
                 if r.kind == "quiz" for it in r.items]
        self.assertGreaterEqual(len(items), 6, "应当产出足够的判断题")
        self.assertFalse(key_balance(items)["skewed"],
                         key_balance(items)["detail"])


class TestFalsification(unittest.TestCase):
    """反向题的"假"必须由审核闸认证，不能是我们自己认为它假。"""

    @classmethod
    def setUpClass(cls):
        from agents.audit import AuditAgent
        from agents.generate import GenerateAgent
        from core.llm import MockLLM
        from core.retrieval import Retriever
        import json as _json
        R = Retriever.from_jsonl(config.KB_PATH)
        kps = _json.loads(config.KP_PATH.read_text(encoding="utf-8"))["points"]
        kpi = {k["id"]: k for k in kps}
        auditor = AuditAgent(MockLLM(), R)
        cls.gen = GenerateAgent(MockLLM(), R, kpi, auditor=auditor)
        cls.auditor = auditor

    def test_numeric_claim_can_be_falsified(self):
        from core.schema import Claim
        c = Claim(text="T1模式下末端法兰中心的移动速度被限制在250毫米每秒以内。",
                  source_id="KB-004")
        fake = self.gen._falsify(c)
        self.assertIsNotNone(fake)
        self.assertNotIn("250", fake)

    def test_falsified_text_is_certified_false(self):
        """产出的假命题必须真的过不了审核，否则就是我们在瞎标。"""
        from core.schema import Claim, VERDICT_CONTRADICTED
        for text, src in [
            ("T1模式下末端法兰中心的移动速度被限制在250毫米每秒以内。", "KB-004"),
            ("机器人安全围栏高度不低于1.4米。", "KB-022"),
            ("安全回路板负责处理急停信号、安全门信号和使能开关信号。", "KB-002"),
        ]:
            fake = self.gen._falsify(Claim(text=text, source_id=src))
            if fake is None:
                continue
            _, dropped = self.auditor.review([Claim(text=fake, source_id=src)])
            self.assertTrue(dropped, f"假命题应当被审核拦下：{fake}")
            self.assertEqual(dropped[0].verdict, VERDICT_CONTRADICTED)

    def test_falsify_returns_none_without_auditor(self):
        """没有审核闸就不产出反向题 —— 认证不了就不做。"""
        from agents.generate import GenerateAgent
        from core.llm import MockLLM
        from core.retrieval import Retriever
        from core.schema import Claim
        import json as _json
        kps = _json.loads(config.KP_PATH.read_text(encoding="utf-8"))["points"]
        g = GenerateAgent(MockLLM(), Retriever.from_jsonl(config.KB_PATH),
                          {k["id"]: k for k in kps}, auditor=None)
        self.assertIsNone(g._falsify(
            Claim(text="机器人安全围栏高度不低于1.4米。", source_id="KB-022")))
