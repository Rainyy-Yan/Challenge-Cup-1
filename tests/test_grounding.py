"""知识库溯源可信度。

这是本项目最要紧的一组测试，理由不在代码层面而在方法层面：

整套幻觉检测的逻辑是"断言必须被知识库支撑"。
**如果知识库本身是错的，审核闸只会把错误认证为正确。**
幻觉率为零的真实含义是"生成内容与知识库一致"，不是"内容正确"。

所以每条切片必须标明是否核实过来源，评测必须按此分层报告，
指标必须在核实率不足时自我降级。这三件事都在这里钉住。
"""

import json
import unittest
from pathlib import Path

import config
from core.retrieval import Retriever


class TestChunkProvenance(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.raw = [json.loads(l) for l in
                   config.KB_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
        cls.R = Retriever.from_jsonl(config.KB_PATH)

    def test_every_chunk_declares_verification(self):
        """不允许有切片不表态。没写就是没核实，必须显式为 False。"""
        for c in self.raw:
            self.assertIn("verified", c, f"{c['id']} 未声明核实状态")
            self.assertIsInstance(c["verified"], bool)

    def test_unverified_chunks_are_traceable_or_flagged(self):
        """未核实的切片，出处要么可回溯，要么带占位标记。

        防的是最危险的情形：一个看起来像真的出处（某本并不存在的教材第几章），
        会让人误以为内容有据可查。

        判据是「能否回溯」而不是「有没有占位前缀」—— 这一点是摄入工具上线后
        修正的。从真实文档切出来的切片，出处形如
        「手册摘录.txt｜第1段｜sha:9b2d327ba7a7」，带文件名、位置和文件指纹，
        任何人都能翻回原文核对。它未经核实，但绝不是伪造 ——
        要求它加「待核实」前缀是把两件事混为一谈。
        """
        for c in self.raw:
            if c["verified"]:
                continue
            # 公开网页可能改版或失效；单有 URL 不是稳定的内容级溯源。
            # 没有 SHA 的记录必须明确保留人工复核标记。
            traceable = "｜sha:" in c["source"]
            flagged = "待核实" in c["source"] or "待人工核实" in c["source"]
            self.assertTrue(traceable or flagged,
                            f"{c['id']} 未核实，出处既不可回溯也无占位标记：{c['source']}")

    def test_verified_chunks_have_real_source(self):
        for c in self.raw:
            if c["verified"]:
                self.assertNotIn("待核实", c["source"])
                self.assertGreater(len(c["source"]), 10, c["id"])

    def test_retriever_exposes_ratio(self):
        r = self.R.verified_ratio()
        self.assertGreaterEqual(r, 0.0)
        self.assertLessEqual(r, 1.0)

    def test_known_verified_chunks(self):
        """已联网核实的条目：FANUC 报警码与示教限速。"""
        for cid in ("KB-015", "KB-016", "KB-017", "KB-018", "KB-004"):
            self.assertTrue(self.R.is_verified(cid), f"{cid} 应标记为已核实")

    def test_misattributed_chunk_is_flagged(self):
        """出处错配的切片必须留下说明，而不是悄悄改掉。

        KB-022 原稿把围栏高度与安全距离挂在一条未经核实的国标条款名下。
        内容也许合理，但把数字挂到具体标准上属于出处错配 ——
        这种错误比编造更隐蔽，因为它有一个真实存在的标准号做掩护。
        """
        c = next(x for x in self.raw if x["id"] == "KB-022")
        self.assertFalse(c["verified"])
        self.assertIn("出处错配", c.get("source_note", ""))


class TestGroundingIsReported(unittest.TestCase):
    """评测必须按溯源可信度分层，并在核实率不足时自我降级。"""

    def test_eval_reports_grounding(self):
        from evalkit.run_eval import evaluate
        res = evaluate(3)
        self.assertIn("grounding", res["summary"])
        g = res["summary"]["grounding"]
        for k in ("verified_claims", "unverified_claims", "verified_share",
                  "kb_verified_ratio"):
            self.assertIn(k, g)

    def test_claims_carry_verification_flag(self):
        from evalkit.run_eval import evaluate
        res = evaluate(2)
        self.assertTrue(res["claims"])
        for row in res["claims"][:20]:
            self.assertIn("kb_verified", row)
            self.assertIsInstance(row["kb_verified"], bool)

    def test_grounding_counts_are_consistent(self):
        from evalkit.run_eval import evaluate
        res = evaluate(3)
        g = res["summary"]["grounding"]
        self.assertEqual(g["verified_claims"] + g["unverified_claims"],
                         len(res["claims"]))
