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

    def test_consistent_chunks_produce_no_conflict(self):
        cs = [self._chunk("KB-1", "KP-10", "报警SRVO-005含义为机器人超程，需按住解除按钮处理。"),
              self._chunk("KB-2", "KP-10", "报警SRVO-005含义为机器人超程，处理时应反向移出限位范围。")]
        kinds = {f.kind for f in audit(cs, self.kps).findings}
        self.assertNotIn("DEFCONFLICT", kinds)
        self.assertNotIn("NUMCONFLICT", kinds)

    def test_orphan_kp_is_error(self):
        cs = [self._chunk("KB-1", "KP-999", "挂在不存在的知识点上的内容，检索永远召回不到。")]
        f = [x for x in audit(cs, self.kps).findings if x.kind == "ORPHAN"]
        self.assertTrue(f)

    def test_dangling_reference_is_warned(self):
        cs = [self._chunk("KB-1", "KP-04", "按照上述步骤完成示教之后控制器会自动解算偏移量，数值见如下表所示界面。")]
        kinds = {f.kind for f in audit(cs, self.kps).findings}
        self.assertIn("DANGLING", kinds)

    def test_real_kb_has_no_internal_conflict(self):
        """现有知识库不得存在库内矛盾。这条防止冲突数据被悄悄引入。"""
        from core.retrieval import Retriever
        rep = audit(Retriever.from_jsonl(config.KB_PATH).chunks, self.kps)
        conflicts = [f for f in rep.findings
                     if f.kind in ("NUMCONFLICT", "DEFCONFLICT")]
        self.assertFalse(conflicts, [f.detail for f in conflicts])
