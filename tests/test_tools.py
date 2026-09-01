"""内容采集工具：知识库导入器与人工评分工具。

这两个工具承担的是骨架版到完整版之间那段路 —— 内容只能由人供给，
工具的职责是把入口守住、把仪器校准好。

导入器的测试重点全在"该拒的必须拒"，尤其是伪造出处那一条。
评分工具的测试重点是"系统结论不能泄漏给评分者"。
"""

import json
import unittest
from pathlib import Path

import config
from tools.kb_import import (LEN_MAX, LEN_MIN, PLACEHOLDER, Draft,
                             parse_markdown, validate)


def _kps():
    return {k["id"] for k in
            json.loads(Path(config.KP_PATH).read_text(encoding="utf-8"))["points"]}


def _check(text, verified=False, source="【待核实·占位出处】某手册", kp="KP-01"):
    d = Draft(id="", kp=kp, title="测试切片", source=source, text=text,
              verified=verified)
    validate(d, _kps(), set(), {})
    return d


def _codes(d):
    return {i.code for i in d.issues}


GOOD = ("示教器上的三位使能开关有松开、中间位、按死三种状态。只有保持在中间位时"
        "伺服才能上电，松开或用力按死都会立即切断伺服电源。该设计用于防止操作者"
        "受惊吓时握紧手柄造成机器人失控。")


class TestParsing(unittest.TestCase):

    def test_parses_meta_and_body(self):
        md = ("## 标题甲\n- kp: KP-01\n- source: 某手册第 3 节\n- verified: true\n\n"
              "正文内容在这里。\n")
        d = parse_markdown(md, "t.md")[0]
        self.assertEqual(d.kp, "KP-01")
        self.assertTrue(d.verified)
        self.assertIn("正文内容", d.text)

    def test_multiple_blocks(self):
        md = "## 甲\n- kp: KP-01\n\n正文甲\n\n## 乙\n- kp: KP-02\n\n正文乙\n"
        self.assertEqual(len(parse_markdown(md, "t.md")), 2)

    def test_verified_accepts_chinese_yes(self):
        md = "## 甲\n- kp: KP-01\n- verified: 是\n\n正文\n"
        self.assertTrue(parse_markdown(md, "t.md")[0].verified)


class TestProvenanceGate(unittest.TestCase):
    """溯源纪律。这组是导入器存在的主要理由。"""

    def test_fake_source_is_rejected(self):
        """未核实却给了看似真实的出处 —— 必须拒收。

        一个像真的假出处会让所有人误以为内容有据可查，
        比明显编造更危险，因为它没有任何视觉破绽。
        """
        d = _check(GOOD, verified=False, source="《工业机器人操作与运维》第 3 章")
        self.assertIn("FAKESOURCE", _codes(d))
        self.assertTrue(d.blocked)

    def test_placeholder_source_passes(self):
        d = _check(GOOD, verified=False, source=PLACEHOLDER + "某教材")
        self.assertNotIn("FAKESOURCE", _codes(d))

    def test_verified_with_placeholder_is_contradiction(self):
        d = _check(GOOD, verified=True, source=PLACEHOLDER + "某教材")
        self.assertIn("CONTRADICT", _codes(d))
        self.assertTrue(d.blocked)

    def test_verified_needs_specific_source(self):
        d = _check(GOOD, verified=True, source="手册")
        self.assertIn("THINSOURCE", _codes(d))

    def test_verified_with_real_source_passes(self):
        d = _check(GOOD, verified=True,
                   source="FANUC R-30iB 操作说明书 B-83284CM 第 3.2 节")
        self.assertFalse(d.blocked, [i.detail for i in d.issues])


class TestStructuralGate(unittest.TestCase):

    def test_unknown_kp_is_rejected(self):
        d = _check(GOOD, kp="KP-99")
        self.assertIn("BADKP", _codes(d))
        self.assertTrue(d.blocked)

    def test_missing_kp_is_rejected(self):
        d = _check(GOOD, kp="")
        self.assertIn("NOKP", _codes(d))

    def test_too_short_is_rejected(self):
        d = _check("太短了。")
        self.assertIn("TOOSHORT", _codes(d))

    def test_too_long_is_rejected(self):
        d = _check("很长的内容。" * 120)
        self.assertIn("TOOLONG", _codes(d))

    def test_vague_number_is_warned(self):
        d = _check(GOOD + "更换周期大约一万小时左右。")
        self.assertIn("VAGUE", _codes(d))
        self.assertFalse(d.blocked, "约数只提醒不拒收")

    def test_opinion_is_warned(self):
        d = _check(GOOD + "我们认为这样更好。")
        self.assertIn("OPINION", _codes(d))

    def test_duplicate_id_is_rejected(self):
        d = Draft(id="KB-001", kp="KP-01", title="t",
                  source=PLACEHOLDER + "x", text=GOOD)
        validate(d, _kps(), {"KB-001"}, {})
        self.assertIn("DUPID", _codes(d))

    def test_near_duplicate_is_warned(self):
        d = Draft(id="", kp="KP-01", title="t", source=PLACEHOLDER + "x", text=GOOD)
        validate(d, _kps(), set(), {"KB-003": GOOD})
        self.assertIn("NEARDUP", _codes(d))

    def test_thresholds_match_real_corpus(self):
        """阈值必须容得下现有语料。

        规范初稿写的是 150-300 字，而骨架版 26 条切片全部落在 71-104 字，
        没有一条符合我们自己写的规范。这条测试防止阈值再次脱离实际。
        """
        from tools.ingest import TABLE_MIN
        recs = [json.loads(l) for l in
                Path(config.KB_PATH).read_text(encoding="utf-8").splitlines() if l.strip()]
        for r in recs:
            n = len(r["text"])
            # 表格行的下限更低：它的上下文由列名结构提供，不靠字数堆。
            floor = TABLE_MIN if "｜第" in r.get("source", "") and "行｜" in r.get("source", "") else LEN_MIN
            self.assertGreaterEqual(n, floor,
                                    f"{r['id']} 长 {n} 字，短于下限 {floor}，阈值与语料脱节")
            self.assertLessEqual(n, LEN_MAX, r["id"])


class TestRatingHarness(unittest.TestCase):
    """人工评分工具：仪器要校准，判断留给人。"""

    def test_kappa_perfect_agreement(self):
        from tools.rating import cohen_kappa
        a = {f"C{i:03d}": "支持" if i % 2 else "不支持" for i in range(1, 11)}
        r = cohen_kappa(a, dict(a), ["支持", "不支持", "无法判断"])
        self.assertEqual(r["kappa"], 1.0)

    def test_kappa_detects_systematic_disagreement(self):
        from tools.rating import cohen_kappa
        a = {f"C{i:03d}": "支持" for i in range(1, 11)}
        b = {f"C{i:03d}": "不支持" for i in range(1, 11)}
        r = cohen_kappa(a, b, ["支持", "不支持", "无法判断"])
        self.assertLessEqual(r["kappa"], 0.0)
        self.assertEqual(len(r["disagreed"]), 10)

    def test_kappa_needs_enough_common_items(self):
        from tools.rating import cohen_kappa
        r = cohen_kappa({"C001": "支持"}, {"C001": "支持"}, ["支持", "不支持"])
        self.assertIsNone(r["kappa"])

    def test_low_kappa_is_interpreted_as_realign(self):
        from tools.rating import interpret
        self.assertIn("对齐", interpret(0.45))
        self.assertIn("良好", interpret(0.85))

    def test_sheets_hide_system_verdicts(self):
        """评分表里绝不能出现系统结论。

        看到系统答案再打分，打出来的是对系统的认同度，不是独立判断。
        """
        import tempfile
        from tools.rating import build_sample, write_sheets
        sample = build_sample(6, 2)
        with tempfile.TemporaryDirectory() as td:
            paths = write_sheets(sample, 2, Path(td))
            for p in paths:
                txt = p.read_text(encoding="utf-8")
                for leak in ("_system_verdict", "_consensus", "_system_difficulty",
                             "_learner_mastery", "supported", "contradicted"):
                    self.assertNotIn(leak, txt, f"{p.name} 泄漏了系统结论 {leak}")

    def test_raters_get_different_order(self):
        """打散顺序，避免评分者形成惯性。"""
        import re as _re
        import tempfile
        from tools.rating import build_sample, write_sheets
        sample = build_sample(10, 3)
        with tempfile.TemporaryDirectory() as td:
            paths = write_sheets(sample, 2, Path(td))
            orders = [_re.findall(r"^##\s+([CF]\d{3})", p.read_text(encoding="utf-8"),
                                  flags=_re.M) for p in paths]
            self.assertEqual(sorted(orders[0]), sorted(orders[1]))
            self.assertNotEqual(orders[0], orders[1])
