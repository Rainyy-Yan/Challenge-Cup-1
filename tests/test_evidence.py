"""证据强度：区间、蒙对概率与判定门槛。

这组测试守的是一个统计错误，而不是显示问题。

原实现只给点估计：四选一题答对 2 道，掌握概率 0.896，判为"掌握牢固"。
但\u7eaf靠蒙达到 2/2 的概率有 6.2%，二十个人里就有一个能蒙出来；
答对 3/4 判"掌握牢固"，蒙对概率 5.1%。
少量作答下的点估计本身就是高方差的，拿它当结论，
等于把"目前的最佳猜测"说成"已经确认"。
"""

import unittest

import config
from core import bkt
from core.ability import build, load_dimensions, narrate


def _params():
    return bkt.BKTParams(p_T=config.BKT_P_T, p_S=config.BKT_P_S, p_G=config.BKT_P_G)


class TestLuckProbability(unittest.TestCase):
    """蒙对概率：最直观的一个数，直接回答"我瞎蒙也能考成这样吗"。"""

    def test_all_correct_matches_binomial(self):
        # 四选一全对 n 题，纯蒙概率恰为 0.25^n
        for n in (1, 2, 3, 4):
            self.assertAlmostEqual(bkt.luck_probability(n, n), 0.25 ** n, places=6)

    def test_zero_correct_is_certain(self):
        """答对零题，任何人都能"达到"这个成绩。"""
        self.assertAlmostEqual(bkt.luck_probability(0, 5), 1.0, places=6)

    def test_monotone_in_correct(self):
        vals = [bkt.luck_probability(k, 6) for k in range(7)]
        self.assertEqual(vals, sorted(vals, reverse=True))

    def test_two_of_two_is_above_threshold(self):
        """答对 2/2 的蒙对概率 6.2%，高于 5% 门槛，因此不得声称已确认。"""
        self.assertGreater(bkt.luck_probability(2, 2), bkt.LUCK_MAX)

    def test_judge_items_use_higher_guess_rate(self):
        """判断题蒙对率是 0.5，不是 0.25。"""
        self.assertAlmostEqual(
            bkt.luck_probability(3, 3, bkt.GUESS_TRUEFALSE), 0.125, places=6)


class TestInterval(unittest.TestCase):

    def test_interval_contains_point_estimate_direction(self):
        p = _params()
        lo, hi = bkt.mastery_interval(3, 4, p)
        self.assertLessEqual(lo, hi)
        self.assertGreaterEqual(lo, 0.0)
        self.assertLessEqual(hi, 1.0)

    def test_more_evidence_narrows_interval(self):
        """作答越多，区间越窄。这是区间估计的基本性质。"""
        p = _params()
        widths = []
        for n in (2, 4, 8, 12):
            lo, hi = bkt.mastery_interval(n, n, p)
            widths.append(hi - lo)
        self.assertEqual(widths, sorted(widths, reverse=True))

    def test_lower_bound_rises_with_evidence(self):
        p = _params()
        lows = [bkt.mastery_interval(n, n, p)[0] for n in (1, 2, 4, 8)]
        self.assertEqual(lows, sorted(lows))

    def test_eight_items_needed_to_confirm(self):
        """要让区间下界过 0.80 这条线，需要八题全对。

        这个数很重要：它说明每知识点三道题的题库，
        结构上就不可能"确认"任何一个知识点。
        """
        p = _params()
        self.assertLess(bkt.mastery_interval(7, 7, p)[0], config.MASTERY_OK)
        self.assertGreaterEqual(bkt.mastery_interval(8, 8, p)[0], config.MASTERY_OK)

    def test_degenerate_params_rejected(self):
        """p_S + p_G >= 1 时无法从作答正确率反解掌握概率。"""
        bad = bkt.BKTParams(p_S=0.6, p_G=0.5)
        with self.assertRaises(ValueError):
            bkt.mastery_interval(2, 3, bad)

    def test_no_data_gives_full_range(self):
        self.assertEqual(bkt.mastery_interval(0, 0, _params()), (0.0, 1.0))


class TestEvidenceState(unittest.TestCase):

    def _state(self, k, n):
        p = _params()
        obs = [True] * k + [False] * (n - k)
        score = bkt.trace(obs, p, p_L0=0.3) if n else 0.0
        lo, _ = bkt.mastery_interval(k, n, p)
        lk = bkt.luck_probability(k, n)
        return bkt.evidence_state(score, lo, lk, n,
                                  config.MASTERY_OK, config.MASTERY_BLIND)

    def test_two_of_two_is_only_likely(self):
        """答对 2/2 点估计 0.896，但下界仅 0.33 且蒙对概率 6.2%，
        只能算疑似掌握。这正是本模块要防的那个错误。"""
        st, why = self._state(2, 2)
        self.assertEqual(st, "likely_strong")
        self.assertIn("下界", why)

    def test_three_of_four_is_only_likely(self):
        st, why = self._state(3, 4)
        self.assertEqual(st, "likely_strong")
        self.assertIn("纯蒙", why)

    def test_four_of_four_still_not_confirmed(self):
        """即使四题全对，下界只有 0.59，仍不足以确认。"""
        self.assertEqual(self._state(4, 4)[0], "likely_strong")

    def test_enough_evidence_confirms(self):
        self.assertEqual(self._state(10, 10)[0], "confirmed_strong")

    def test_blind_needs_two_items(self):
        """判盲区不必卡蒙对概率 —— 蒙对只会把成绩往上抬，
        成绩低说明连蒙都没蒙上，结论方向是安全的。但一题仍不够。"""
        self.assertEqual(self._state(0, 1)[0], "likely_blind")
        self.assertEqual(self._state(0, 2)[0], "confirmed_blind")

    def test_untested(self):
        self.assertEqual(self._state(0, 0)[0], "untested")


class TestAbilityProfile(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from agents.diagnose import DiagnoseAgent
        from core.llm import MockLLM
        from orchestrator import load_profile
        cls.diag = DiagnoseAgent(MockLLM()).run(load_profile("P-B"))
        cls.prof = build(cls.diag)

    def test_dimensions_cover_all_knowledge_points(self):
        """六个维度必须覆盖全部知识点，否则有知识点永远不出现在图谱上。"""
        import json
        from pathlib import Path
        kps = {k["id"] for k in json.loads(
            Path(config.KP_PATH).read_text(encoding="utf-8"))["points"]}
        covered = {k for d in load_dimensions() for k in d["kps"]}
        self.assertEqual(kps - covered, set())

    def test_each_dimension_has_enough_points(self):
        """每维至少三个知识点。

        不直接用知识点标签做轴：十三个标签里八个只覆盖一个知识点，
        一道题的对错就能让整条轴塌掉，看着像能力图谱，其实是噪声图。
        """
        for d in load_dimensions():
            self.assertGreaterEqual(len(d["kps"]), 3, d["name"])

    def test_lower_never_exceeds_score(self):
        for d in self.prof.dims:
            self.assertLessEqual(d.lower, d.score + 1e-9, d.name)

    def test_untested_dimension_is_not_zero_score(self):
        """未测维度必须单独记录，不能混同于"得分为零"。

        零和"没测过"在雷达图上长得一样，但含义相反：
        前者是"确定不会"，后者是"不知道"。
        """
        from core.ability import AbilityProfile, DimScore
        d = DimScore(id="X", name="未测维度", desc="", score=0.0, lower=0.0,
                     tested=0, total=3)
        self.assertEqual(d.coverage, 0.0)
        self.assertEqual(d.tested, 0)

    def test_gap_reflects_evidence_thinness(self):
        """点估计与下界之间的间隙就是"还不确定的部分"。"""
        for d in self.prof.dims:
            if d.tested:
                self.assertAlmostEqual(d.evidence_gap, round(d.score - d.lower, 3))

    def test_narration_is_rule_based(self):
        text = narrate(self.prof)
        self.assertTrue(text)
        self.assertIn("维度", text)

    def test_profiles_have_different_shapes(self):
        """三个画像的能力形状必须不同，否则图谱没有区分度。"""
        from agents.diagnose import DiagnoseAgent
        from core.llm import MockLLM
        from orchestrator import load_profile
        shapes = []
        for pid in ("P-A", "P-B", "P-C"):
            d = DiagnoseAgent(MockLLM()).run(load_profile(pid))
            shapes.append(tuple(round(x.score, 2) for x in build(d).dims))
        self.assertEqual(len(set(shapes)), 3)
