"""审核裁判 Agent 的单元测试。

这里的植入用例是整套方案的证据来源。评委问"你怎么证明拦得住"，
答案就是这个文件加 evalkit 的消融对照，不是靠嘴说。

夹具分四类，对应大模型在垂直领域最常见的四种翻车方式：
  1. 完全编造，无出处
  2. 引用了不存在的切片
  3. 内容与切片无关，但引用写得像模像样（张冠李戴）
  4. 句式正确、数字被改（最阴的一类，人眼也容易漏）
"""

import unittest

from agents.audit import AuditAgent, semantic_boundary_issue
import config
from core.llm import MockLLM
from core.retrieval import Retriever
from core.schema import (Chunk, Claim, VERDICT_CONTRADICTED, VERDICT_SUPPORTED,
                         VERDICT_UNSUPPORTED)


class TestAudit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.retriever = Retriever.from_jsonl(config.KB_PATH)
        cls.auditor = AuditAgent(MockLLM(), cls.retriever)

    # ---- 真断言应当放行 ----

    def test_true_claims_pass(self):
        truths = [
            Claim(text="三位使能装置有松开、中间位和按死三种状态。",
                  source_id="KB-003"),
            Claim(text="手动模式速度最高为250 mm/s。",
                  source_id="KB-004"),
            Claim(text="SRVO-001 表示操作面板急停被按下。", source_id="KB-015"),
            Claim(text="带联锁的安全门打开时应停止自动运行。", source_id="KB-022"),
        ]
        kept, dropped = self.auditor.review(truths)
        self.assertEqual(len(dropped), 0, [d.audit_note for d in dropped])
        self.assertTrue(all(c.verdict == VERDICT_SUPPORTED for c in kept))

    # ---- 四类幻觉应当拦下 ----

    def test_no_citation_is_unsupported(self):
        c = Claim(text="控制柜每运行500小时需要更换一次主控板电池。")
        kept, dropped = self.auditor.review([c])
        self.assertEqual(len(kept), 0)
        self.assertEqual(dropped[0].verdict, VERDICT_UNSUPPORTED)

    def test_dangling_citation_is_unsupported(self):
        c = Claim(text="示教器支持离线编程导入。", source_id="KB-999")
        kept, dropped = self.auditor.review([c])
        self.assertEqual(len(kept), 0)
        self.assertIn("不存在", dropped[0].audit_note)

    def test_misattributed_citation_is_caught(self):
        """内容讲润滑，却引用安全围栏的切片。"""
        c = Claim(text="减速机润滑脂应当每运行三千小时更换一次。",
                  source_id="KB-022")
        kept, dropped = self.auditor.review([c])
        self.assertEqual(len(kept), 0)
        self.assertIn(dropped[0].verdict,
                      (VERDICT_UNSUPPORTED, VERDICT_CONTRADICTED))

    def test_altered_number_is_contradicted(self):
        """句式照抄，把 250 改成 200。这类最难靠人眼发现。"""
        c = Claim(text="手动模式速度最高为200 mm/s。",
                  source_id="KB-004")
        kept, dropped = self.auditor.review([c])
        self.assertEqual(len(kept), 0)
        self.assertEqual(dropped[0].verdict, VERDICT_CONTRADICTED)
        self.assertIn("200", dropped[0].audit_note)

    def test_wrong_alarm_code_mapping_is_caught(self):
        """把操作面板急停的含义安到示教器急停上。"""
        c = Claim(text="SRVO-002 表示操作面板急停被按下。", source_id="KB-016")
        kept, dropped = self.auditor.review([c])
        self.assertEqual(len(kept), 0)

    # ---- 批量拦截率 ----

    def test_intercept_rate_on_planted_set(self):
        planted = [
            Claim(text="国家标准规定该项检测每72小时执行一次。", source_id="KB-021"),
            Claim(text="TCP标定四个姿态的夹角必须小于10度。", source_id="KB-006"),
            Claim(text="手动模式速度最高为900 mm/s。", source_id="KB-004"),
            Claim(text="子程序调用层数上限为32层。", source_id="KB-012"),
            Claim(text="三位使能装置有8种状态。", source_id="KB-003"),
            Claim(text="控制器会自动切换到备用固件分区。", source_id=None),
        ]
        kept, dropped = self.auditor.review(planted)
        rate = AuditAgent.intercept_rate(kept, dropped)
        self.assertGreaterEqual(rate, 0.8,
                                f"拦截率仅 {rate:.0%}，漏网：{[k.text for k in kept]}")

    def test_evidence_score_recorded(self):
        c = Claim(text="SRVO-001 表示操作面板急停被按下。", source_id="KB-015")
        self.auditor.review([c])
        self.assertGreater(c.evidence_score, config.EVIDENCE_MIN)

    def test_scope_expansion_is_contradicted(self):
        retriever = Retriever([Chunk(
            id="KB-SCOPE", kp="KP-X", title="特定控制器手册",
            source="manual", verified=True,
            text="以ABB所列控制器为例，手动模式速度最高为250 mm/s；"
                 "具体模式权限应按实际机型手册确认。",
        )])
        auditor = AuditAgent(MockLLM(), retriever)
        claim = Claim(
            text="所有工业机器人的手动模式速度最高为250 mm/s。",
            source_id="KB-SCOPE",
        )
        kept, dropped = auditor.review([claim])
        self.assertFalse(kept)
        self.assertEqual(dropped[0].verdict, VERDICT_CONTRADICTED)
        self.assertIn("适用范围", dropped[0].audit_note)

    def test_requirement_relaxation_is_contradicted(self):
        retriever = Retriever([Chunk(
            id="KB-LOAD", kp="KP-X", title="负载数据",
            source="manual", verified=True,
            text="工具或有效载荷数据未知时，可用负载辨识功能定义；"
                 "错误的负载数据可能导致碰撞检测失效。",
        )])
        auditor = AuditAgent(MockLLM(), retriever)
        claim = Claim(
            text="使用负载辨识功能后无需再核对工具或有效载荷数据。",
            source_id="KB-LOAD",
        )
        kept, dropped = auditor.review([claim])
        self.assertFalse(kept)
        self.assertEqual(dropped[0].verdict, VERDICT_CONTRADICTED)
        self.assertIn("条件或步骤", dropped[0].audit_note)

    def test_explicit_universal_scope_is_not_blocked(self):
        retriever = Retriever([Chunk(
            id="KB-UNIVERSAL", kp="KP-X", title="通用规则",
            source="manual", verified=True,
            text="所有设备都必须在检修前断开能源。",
        )])
        auditor = AuditAgent(MockLLM(), retriever)
        claim = Claim(
            text="所有设备都必须在检修前断开能源。",
            source_id="KB-UNIVERSAL",
        )
        kept, dropped = auditor.review([claim])
        self.assertFalse(dropped)
        self.assertEqual(kept[0].verdict, VERDICT_SUPPORTED)

    def test_preserved_scope_limit_is_not_blocked(self):
        retriever = Retriever([Chunk(
            id="KB-LIMITED", kp="KP-X", title="特定控制器手册",
            source="manual", verified=True,
            text="以ABB所列控制器为例，所有该型号设备在检修前必须断开能源。",
        )])
        auditor = AuditAgent(MockLLM(), retriever)
        claim = Claim(
            text="以ABB所列控制器为例，所有该型号设备在检修前必须断开能源。",
            source_id="KB-LIMITED",
        )
        kept, dropped = auditor.review([claim])
        self.assertFalse(dropped)
        self.assertEqual(kept[0].verdict, VERDICT_SUPPORTED)

    def test_explicit_relaxation_is_not_blocked(self):
        retriever = Retriever([Chunk(
            id="KB-RELAX", kp="KP-X", title="自动检查",
            source="manual", verified=True,
            text="自检完成后无需再次运行同一项自检。",
        )])
        auditor = AuditAgent(MockLLM(), retriever)
        claim = Claim(
            text="自检完成后无需再次运行同一项自检。",
            source_id="KB-RELAX",
        )
        kept, dropped = auditor.review([claim])
        self.assertFalse(dropped)
        self.assertEqual(kept[0].verdict, VERDICT_SUPPORTED)

    def test_equivalent_relaxation_wording_is_not_blocked(self):
        retriever = Retriever([Chunk(
            id="KB-RELAX-SYNONYM", kp="KP-X", title="自动检查",
            source="manual", verified=True,
            text="自检完成后无需再次运行同一项自检。",
        )])
        auditor = AuditAgent(MockLLM(), retriever)
        claim = Claim(
            text="自检完成后不需要再次运行同一项自检。",
            source_id="KB-RELAX-SYNONYM",
        )
        kept, dropped = auditor.review([claim])
        self.assertFalse(dropped)
        self.assertEqual(kept[0].verdict, VERDICT_SUPPORTED)

    def test_automatic_mode_does_not_hide_cancelled_validation(self):
        issue = semantic_boundary_issue(
            "快速校对完成后无需验证精度。",
            "自动模式可用于生产；快速校对完成后必须验证精度。",
        )
        self.assertIsNotNone(issue)

    def test_broad_substrings_do_not_create_a_requirement(self):
        issue = semantic_boundary_issue(
            "查看错误信息无需打开附录。",
            "维护需求记录在附录中，应用程序重新定义了错误信息格式。",
        )
        self.assertIsNone(issue)

    def test_explicit_automatic_validation_in_evidence_is_preserved(self):
        issue = semantic_boundary_issue(
            "自检完成后会自动验证精度。",
            "自检完成后会自动验证精度。",
        )
        self.assertIsNone(issue)


if __name__ == "__main__":
    unittest.main()
