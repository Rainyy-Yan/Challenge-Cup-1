"""原始资料摄入与知识库交叉校验。

这两个工具解决的是"太封闭、无从下手"：
在此之前往知识库加内容必须手写特定格式，且知识点编号必须**已经存在** ——
拿到一本真实手册想录进去，第一步就卡住。

摄入反过来做：先喂原始资料，让知识点从语料里长出来。
交叉校验查的是前两道关口都看不见的东西 —— 切片**之间**的矛盾。
"""

import json
import unittest
from pathlib import Path

import config
from core.schema import Chunk
from tools.ingest import (TABLE_MIN, TARGET_MAX, TARGET_MIN, Segment, Staged,
                          chunk_segments, quality_gate, read_table, read_txt)
from tools.kb_audit import audit, subjects_of


def _st(text, from_table=False, **kw):
    d = dict(id="ST-1", kp="KP-01", title="t", text=text,
             source="f.txt｜第1段｜sha:abc", locator="第1段", file_sha="abc",
             match_score=0.5, from_table=from_table)
    d.update(kw)
    return Staged(**d)


class TestChunking(unittest.TestCase):

    def test_never_splits_mid_sentence(self):
        """在句中切开会产出半截话 —— 既过不了审核，也没法给人复核。"""
        long = "。".join(f"这是第{i}句用于验证切分边界的测试内容足够长" for i in range(40)) + "。"
        out = chunk_segments([Segment(long, "第1段")])
        for text, _ in out:
            self.assertTrue(text.rstrip().endswith(("。", "！", "？", "；")),
                            f"切片未在句末结束：…{text[-14:]}")

    def test_respects_max_length(self):
        long = "内容。" * 400
        for text, _ in chunk_segments([Segment(long, "第1段")]):
            self.assertLessEqual(len(text), TARGET_MAX + 40)

    def test_table_rows_stay_atomic(self):
        """表格每行必须独立成片。

        实测把报警代码表按普通正文处理，四行报警码被合并成一条 202 字的切片，
        四个代码互相干扰，审核无法定位到底哪句有依据。
        """
        segs = [Segment(f"报警代码为SRVO-00{i}；含义为某种故障状态；处理方法为复位。", f"第{i}行")
                for i in range(1, 5)]
        out = chunk_segments(segs, atomic=True)
        self.assertEqual(len(out), 4)
        for text, _ in out:
            self.assertEqual(text.count("报警代码为"), 1)

    def test_locator_is_preserved(self):
        out = chunk_segments([Segment("正文内容。" * 20, "第7页第2段")])
        self.assertTrue(all(loc == "第7页第2段" for _, loc in out))


class TestQualityGate(unittest.TestCase):

    def test_escape_sequences_are_caught(self):
        """字面量转义序列全是 ASCII，按高位字符占比判乱码完全看不见。"""
        s = _st("这段内容包含 \\xc3\\x28 这样的转义序列说明文件其实已经读坏了需要人工处理确认编码" * 2)
        quality_gate([s])
        self.assertTrue(any("转义序列" in r for r in s.reasons))

    def test_replacement_char_is_caught(self):
        s = _st("这段内容含有\ufffd替换字符说明解码失败了应当被拦下来不要进入知识库以免污染" * 2)
        quality_gate([s])
        self.assertTrue(any("替换字符" in r for r in s.reasons))

    def test_toc_page_is_caught(self):
        s = _st("目 录 ..................... 1 第一章 概述 ............... 3 第二章 安装 ....... 15")
        quality_gate([s])
        self.assertTrue(any("目录" in r for r in s.reasons))

    def test_no_punctuation_is_caught(self):
        s = _st("这是一段很长的没有任何标点符号的内容通常意味着抽取失败" * 4)
        quality_gate([s])
        self.assertTrue(any("无句读" in r for r in s.reasons))

    def test_duplicate_with_different_prefix_is_caught(self):
        """同一段内容出现两次，其中一次前面粘了章节标题。

        按开头比对判不出来 —— 手册里正文被反复引用是常态。
        """
        body = "示教器上的三位使能开关有松开中间位按死三种状态只有保持在中间位时伺服才能上电这是安全设计的一部分需要牢记" * 2
        a = _st(body, id="ST-1")
        b = _st("第三章 示教器操作" + body, id="ST-2")
        quality_gate([a, b])
        self.assertTrue(any("重复" in r for r in b.reasons))

    def test_table_rows_use_lower_floor(self):
        """表格行的上下文由列名结构提供，不需要靠字数堆。

        用正文的尺子量表格，会把质量最高的一类资料整批丢掉。
        """
        row = "报警代码为SRVO-001；含义为操作面板急停被按下；处理方法为复位。"
        self.assertLess(len(row), TARGET_MIN)
        self.assertGreaterEqual(len(row), TABLE_MIN)
        s = _st(row, from_table=True)
        quality_gate([s])
        self.assertFalse(any("过短" in r for r in s.reasons))

    def test_prose_below_floor_is_caught(self):
        s = _st("太短。", from_table=False)
        quality_gate([s])
        self.assertTrue(any("过短" in r for r in s.reasons))

    def test_quarantine_not_delete(self):
        """不合格条目改状态，不丢内容。"""
        s = _st("太短。")
        quality_gate([s])
        self.assertEqual(s.status, "quarantined")
        self.assertTrue(s.text, "内容必须保留")


class TestIngestNeverVerifies(unittest.TestCase):
    """摄入永远不能写 verified。

    从真实文档切出来的内容是"有出处的"，不等于"经过核实的"。
    出处真实只说明这句话确实印在那本书上，不说明那本书是对的。
    """

    def test_staged_defaults_unverified(self):
        self.assertFalse(_st("正文" * 40).verified)

    def test_apply_writes_unverified(self):
        import inspect
        from tools import ingest
        src = inspect.getsource(ingest.apply_to_kb)
        self.assertIn('"verified": False', src)

    def test_source_is_traceable(self):
        """出处必须能回溯到文件与位置，这是与编造出处的根本区别。"""
        s = _st("正文" * 40)
        self.assertIn("｜", s.source)
        self.assertIn("sha:", s.source)


class TestCrossValidation(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.kps = json.loads(Path(config.KP_PATH).read_text(encoding="utf-8"))["points"]

    def _chunk(self, cid, kp, text, verified=False):
        return Chunk(id=cid, kp=kp, title=text[:12],
                     source="f.pdf｜第1页第1段｜sha:abc", text=text, verified=verified)

    def test_subject_extraction(self):
        self.assertIn("SRVO-005", subjects_of("报警SRVO-005含义为机器人超程"))

    def test_subject_extraction_keeps_lowercase_model_suffix(self):
        subjects = subjects_of("UR10e 最大负载")
        self.assertIn("UR10E", subjects)
        self.assertNotIn("UR10", subjects)

    def test_subject_extraction_accepts_compact_and_spaced_models(self):
        self.assertIn("IRC5", subjects_of("IRC5 最大电流"))
        self.assertIn("IRB4600", subjects_of("IRB 4600 最大负载"))
        self.assertIn("R-30IB", subjects_of("R-30iB Plus 控制器"))
        self.assertIn("T1", subjects_of("T1 模式"))

    def test_software_version_is_not_treated_as_a_hardware_subject(self):
        self.assertEqual(set(), subjects_of("PolyScope SW5.20 软件版本"))

    def test_spaced_model_numeric_conflict_is_caught(self):
        cs = [
            self._chunk("KB-1", "KP-06", "IRB 4600 最大负载为 20 千克。"),
            self._chunk("KB-2", "KP-06", "IRB4600 最大负载为 30 千克。"),
        ]
        kinds = {f.kind for f in audit(cs, self.kps).findings}
        self.assertIn("NUMCONFLICT", kinds)

    def test_definition_conflict_is_caught_despite_low_similarity(self):
        """同一对象被赋予不同含义 —— 越严重的矛盾两条切片越不像。

        第一版用"相似度够高才比对"，恰好漏掉这一类：
        两条讲同一个报警码，相似度只有 0.175，正因为说法完全不同。
        拿相似度当门槛，逻辑正好反了。
        """
        cs = [self._chunk("KB-1", "KP-10", "报警SRVO-005含义为机器人超程。处理方法是按住超程解除按钮反向移出限位。"),
              self._chunk("KB-2", "KP-10", "报警SRVO-005含义为伺服放大器过载。处理方法是检查散热风扇并降低负载。")]
        kinds = {f.kind for f in audit(cs, self.kps).findings}
        self.assertIn("DEFCONFLICT", kinds)

    def test_numeric_conflict_is_caught(self):
        cs = [self._chunk("KB-1", "KP-02", "T1模式下末端法兰中心的移动速度被限制在250毫米每秒以内，T2模式可全速运行。"),
              self._chunk("KB-2", "KP-02", "T1模式下末端法兰中心的移动速度被限制在500毫米每秒以内，T2模式可全速运行。")]
        kinds = {f.kind for f in audit(cs, self.kps).findings}
        self.assertIn("NUMCONFLICT", kinds)

    def test_paraphrased_same_metric_numeric_conflict_is_caught(self):
        cs = [
            self._chunk(
                "KB-1", "KP-02",
                "T1模式下末端法兰中心的移动速度被限制在250毫米每秒以内。"),
            self._chunk(
                "KB-2", "KP-02",
                "T1模式下，末端法兰中心移动速度最高为500毫米每秒。"),
        ]
        kinds = {f.kind for f in audit(cs, self.kps).findings}
        self.assertIn("NUMCONFLICT", kinds)

    def test_numeric_range_bounds_can_coexist(self):
        cs = [
            self._chunk("KB-1", "KP-02", "UR30 的最大工作温度为 50 摄氏度。"),
            self._chunk("KB-2", "KP-02", "UR30 的最小工作温度为 5 摄氏度。"),
        ]
        kinds = {f.kind for f in audit(cs, self.kps).findings}
        self.assertNotIn("NUMCONFLICT", kinds)

    def test_equivalent_decimal_values_are_not_a_conflict(self):
        cs = [
            self._chunk("KB-1", "KP-09", "UR30 工具输出额定电流为 2 安培。"),
            self._chunk("KB-2", "KP-09", "UR30 工具输出额定电流为 2.0 安培。"),
        ]
        kinds = {f.kind for f in audit(cs, self.kps).findings}
        self.assertNotIn("NUMCONFLICT", kinds)

    def test_chinese_numeral_numeric_conflict_is_caught(self):
        cs = [
            self._chunk("KB-1", "KP-02", "T1 模式移动速度最高为二百五十毫米每秒。"),
            self._chunk("KB-2", "KP-02", "T1 模式移动速度最高为五百毫米每秒。"),
        ]
        kinds = {f.kind for f in audit(cs, self.kps).findings}
        self.assertIn("NUMCONFLICT", kinds)

    def test_equivalent_unit_aliases_are_compared(self):
        cs = [
            self._chunk("KB-1", "KP-06", "UR30 最大负载为 30 千克。"),
            self._chunk("KB-2", "KP-06", "UR30 最大负载为 35 公斤。"),
        ]
        kinds = {f.kind for f in audit(cs, self.kps).findings}
        self.assertIn("NUMCONFLICT", kinds)

    def test_length_unit_aliases_are_compared(self):
        cs = [
            self._chunk("KB-1", "KP-06", "UR30 最大工作范围为 100 毫米。"),
            self._chunk("KB-2", "KP-06", "UR30 最大工作范围为 120 mm。"),
        ]
        kinds = {f.kind for f in audit(cs, self.kps).findings}
        self.assertIn("NUMCONFLICT", kinds)

    def test_negative_numeric_conflict_is_caught(self):
        cs = [
            self._chunk("KB-1", "KP-02", "UR30 最低工作温度为 -10 摄氏度。"),
            self._chunk("KB-2", "KP-02", "UR30 最低工作温度为 -20 摄氏度。"),
        ]
        kinds = {f.kind for f in audit(cs, self.kps).findings}
        self.assertIn("NUMCONFLICT", kinds)

    def test_source_attribution_prefix_does_not_change_metric_key(self):
        cs = [
            self._chunk("KB-1", "KP-06", "根据 UR30 用户手册，最大负载为 30 千克。"),
            self._chunk("KB-2", "KP-06", "UR30 用户手册规定，最大负载为 35 千克。"),
        ]
        kinds = {f.kind for f in audit(cs, self.kps).findings}
        self.assertIn("NUMCONFLICT", kinds)

    def test_numeric_range_endpoint_conflict_is_caught(self):
        cs = [
            self._chunk("KB-1", "KP-02", "UR30 工作温度范围为 -10 至 50 摄氏度。"),
            self._chunk("KB-2", "KP-02", "UR30 工作温度范围为 -20 至 50 摄氏度。"),
        ]
        kinds = {f.kind for f in audit(cs, self.kps).findings}
        self.assertIn("NUMCONFLICT", kinds)

    def test_count_numeric_conflict_is_caught(self):
        cs = [
            self._chunk("KB-1", "KP-10", "SRVO-005 报警后重试 3 次。"),
            self._chunk("KB-2", "KP-10", "SRVO-005 报警后重试 5 次。"),
        ]
        kinds = {f.kind for f in audit(cs, self.kps).findings}
        self.assertIn("NUMCONFLICT", kinds)

    def test_same_product_model_can_have_independent_facts(self):
        """机型和版本是适用范围，不等于每句话都在重新定义它们。"""
        cs = [
            self._chunk(
                "KB-1",
                "KP-06",
                "UR30 用户手册（SW5.20）表示，MoveJ 在关节空间计算运动，"
                "各关节会同时完成各自位移。",
            ),
            self._chunk(
                "KB-2",
                "KP-09",
                "UR30 用户手册（SW5.20）表示，通用数字输出可配置为"
                "程序停止时自动禁用。",
            ),
        ]

        conflicts = [
            finding for finding in audit(cs, self.kps).findings
            if finding.kind in ("DEFCONFLICT", "NUMCONFLICT")
        ]

        self.assertEqual([], conflicts)

    def test_same_product_model_different_metrics_are_not_numeric_conflict(self):
        cs = [
            self._chunk(
                "KB-1",
                "KP-09",
                "UR30 用户手册说明，工具数字输出的最大电流为 2 安培。",
            ),
            self._chunk(
                "KB-2",
                "KP-09",
                "UR30 用户手册说明，控制箱数字输出的最大电流为 8 安培。",
            ),
        ]

        conflicts = [
            finding for finding in audit(cs, self.kps).findings
            if finding.kind == "NUMCONFLICT"
        ]

        self.assertEqual([], conflicts)

    def test_same_product_metric_numeric_conflict_is_caught(self):
        cs = [
            self._chunk("KB-1", "KP-06", "UR30 的最大负载为 30 千克。"),
            self._chunk("KB-2", "KP-06", "UR30 的最大负载为 35 千克。"),
        ]

        kinds = {f.kind for f in audit(cs, self.kps).findings}

        self.assertIn("NUMCONFLICT", kinds)

    def test_multiple_quantities_do_not_pollute_later_metric_key(self):
        cs = [
            self._chunk(
                "KB-1", "KP-09",
                "UR30 额定电压为 24 伏，最大电流为 2 安培。",
            ),
            self._chunk("KB-2", "KP-09", "UR30 最大电流为 3 安培。"),
        ]
        kinds = {f.kind for f in audit(cs, self.kps).findings}
        self.assertIn("NUMCONFLICT", kinds)

    def test_distinct_model_suffixes_are_not_merged_for_numeric_conflicts(self):
        first = self._chunk("KB-1", "KP-06", "本机型最大负载为 10 千克。", verified=True)
        first.title = "UR10 最大负载"
        second = self._chunk("KB-2", "KP-06", "本机型最大负载为 12.5 千克。")
        second.title = "UR10e 最大负载"

        conflicts = [
            finding for finding in audit([first, second], self.kps).findings
            if finding.kind == "NUMCONFLICT"
        ]

        self.assertEqual([], conflicts)

    def test_consistent_chunks_produce_no_conflict(self):
        cs = [self._chunk("KB-1", "KP-10", "报警SRVO-005含义为机器人超程，需按住解除按钮处理。"),
              self._chunk("KB-2", "KP-10", "报警SRVO-005含义为机器人超程，处理时应反向移出限位范围。")]
        kinds = {f.kind for f in audit(cs, self.kps).findings}
        self.assertNotIn("DEFCONFLICT", kinds)
        self.assertNotIn("NUMCONFLICT", kinds)

    def test_table_alarm_definition_conflict_is_caught(self):
        cs = [
            self._chunk(
                "KB-1", "KP-10",
                "报警代码为 SRVO-005；含义为机器人超程；处理方法为反向移出限位。"),
            self._chunk(
                "KB-2", "KP-10",
                "报警代码为 SRVO-005；含义为伺服放大器过载；处理方法为检查散热。"),
        ]
        kinds = {f.kind for f in audit(cs, self.kps).findings}
        self.assertIn("DEFCONFLICT", kinds)

    def test_possessive_definition_conflict_is_caught(self):
        cs = [
            self._chunk("KB-1", "KP-10", "SRVO-005 的含义为机器人超程。"),
            self._chunk("KB-2", "KP-10", "SRVO-005 的含义为伺服放大器过载。"),
        ]
        kinds = {f.kind for f in audit(cs, self.kps).findings}
        self.assertIn("DEFCONFLICT", kinds)

    def test_same_english_definition_with_different_remedies_is_not_a_conflict(self):
        cs = [
            self._chunk(
                "KB-1", "KP-10",
                "SRVO-005 means robot overtravel. Reset by moving the affected axis."),
            self._chunk(
                "KB-2", "KP-10",
                "SRVO-005 means robot overtravel. Inspect the controller and restart power."),
        ]
        kinds = {f.kind for f in audit(cs, self.kps).findings}
        self.assertNotIn("DEFCONFLICT", kinds)

    def test_paraphrased_definition_is_never_an_auto_quarantine_error(self):
        cs = [
            self._chunk("KB-1", "KP-10", "SRVO-005 含义为机器人超程报警。", verified=True),
            self._chunk("KB-2", "KP-10", "SRVO-005 含义为机器人运动超出允许范围。"),
        ]
        conflicts = [
            finding for finding in audit(cs, self.kps).findings
            if finding.kind == "DEFCONFLICT"
        ]
        self.assertTrue(conflicts)
        self.assertTrue(all(finding.severity == "warn" for finding in conflicts))

    def test_orphan_kp_is_error(self):
        cs = [self._chunk("KB-1", "KP-999", "挂在不存在的知识点上的内容，检索永远召回不到。")]
        f = [x for x in audit(cs, self.kps).findings if x.kind == "ORPHAN"]
        self.assertTrue(f)

    def test_dangling_reference_is_warned(self):
        cs = [self._chunk("KB-1", "KP-04", "按照上述步骤完成示教之后控制器会自动解算偏移量，数值见如下表所示界面。")]
        kinds = {f.kind for f in audit(cs, self.kps).findings}
        self.assertIn("DANGLING", kinds)

    def test_explicit_manual_review_marker_is_not_fake_source(self):
        """明确标为待人工核实的来源不能被当成伪装的已核实来源。"""
        chunk = Chunk(
            id="KB-1",
            kp="KP-04",
            title="TCP 定义",
            source="【待人工核实】厂商手册｜第 1 页",
            text="工具中心点用于描述工具相对法兰的位姿关系。",
            verified=False,
        )

        kinds = {f.kind for f in audit([chunk], self.kps).findings}

        self.assertNotIn("FAKESOURCE", kinds)

    def test_pending_words_inside_prose_are_not_a_source_status(self):
        chunk = Chunk(
            id="KB-1",
            kp="KP-04",
            title="TCP 定义",
            source="ABB 手册（未标待核实状态）",
            text="工具中心点用于描述工具相对法兰的位姿关系。",
            verified=False,
        )

        kinds = {f.kind for f in audit([chunk], self.kps).findings}

        self.assertIn("FAKESOURCE", kinds)

    def test_malformed_hash_is_not_a_traceable_source(self):
        chunk = Chunk(
            id="KB-1",
            kp="KP-04",
            title="TCP 定义",
            source="ABB 手册｜sha:abc",
            text="工具中心点用于描述工具相对法兰的位姿关系。",
            verified=False,
        )

        report = audit([chunk], self.kps)
        kinds = {finding.kind for finding in report.findings}

        self.assertIn("FAKESOURCE", kinds)
        self.assertEqual(0, report.stats["sourced"])

    def test_real_kb_has_no_internal_conflict(self):
        """现有知识库不得存在库内矛盾。这条防止冲突数据被悄悄引入。"""
        from core.retrieval import Retriever
        rep = audit(Retriever.from_jsonl(config.KB_PATH).chunks, self.kps)
        conflicts = [f for f in rep.findings
                     if f.kind in ("NUMCONFLICT", "DEFCONFLICT")]
        self.assertFalse(conflicts, [f.detail for f in conflicts])
