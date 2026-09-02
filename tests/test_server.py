"""演示服务接口的回归测试。"""

import base64
import json
import re
import tempfile
import unittest
from pathlib import Path

import config
import server
from orchestrator import Orchestrator, load_profile


class TestSessionPayload(unittest.TestCase):
    def setUp(self):
        self.key = "test-session"
        self.orch = Orchestrator()
        self.session = self.orch.run(load_profile("P-A"), max_kp=1)
        server.SESSIONS[self.key] = (self.orch, self.session)

    def tearDown(self):
        server.SESSIONS.pop(self.key, None)

    def test_resource_provenance_includes_verification_metadata(self):
        payload = server.session_payload(self.key)

        self.assertIn("kb", payload)
        source_id = payload["resources"][0]["claims"][0]["source_id"]
        source = payload["kb"][source_id]
        self.assertIn("source", source)
        self.assertIn("verified", source)
        self.assertIn("source_note", source)


class TestMaterialStaging(unittest.TestCase):
    def test_staging_keeps_content_out_of_knowledge_base_and_unverified(self):
        text = (
            "工业机器人在示教器 T1 模式下调试时，应先确认急停按钮、防护围栏和安全"
            "距离处于可用状态。完成动作前需检查坐标系与工具参数，结束后记录报警现象"
            "及处理过程。本资料只用于受控暂存，必须由人工核对原始文件位置和外部依据，"
            "未经人工复核不得写入正式知识库，也不得标记为已核实。"
        )
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        before = config.KB_PATH.read_bytes()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = server.stage_material(
                "robot_t1.md", encoded, "团队已获授权的调试培训材料 V1.0", True,
                incoming_dir=root / "incoming", staging_root=root / "staged",
            )
            stage_dir = root / "staged" / report["upload_id"]
            manifest = json.loads((stage_dir / "upload_manifest.json").read_text(encoding="utf-8"))
            staged_lines = (stage_dir / "staged.jsonl").read_text(encoding="utf-8").splitlines()

        self.assertFalse(report["knowledge_base_written"])
        self.assertFalse(report["verified"])
        self.assertEqual(manifest["review_state"], "pending_manual_review")
        self.assertFalse(manifest["knowledge_base_written"])
        self.assertFalse(manifest["verified"])
        self.assertTrue(staged_lines)
        self.assertTrue(all(json.loads(line)["verified"] is False for line in staged_lines))
        self.assertEqual(config.KB_PATH.read_bytes(), before)

    def test_staging_rejects_unsafe_or_unattested_input(self):
        encoded = base64.b64encode("足够长的测试资料内容。".encode("utf-8")).decode("ascii")
        with self.assertRaises(server.UploadError):
            server.stage_material("../unsafe.txt", encoded, "测试来源说明", True)
        with self.assertRaises(server.UploadError):
            server.stage_material("notes.exe", encoded, "测试来源说明", True)
        with self.assertRaises(server.UploadError):
            server.stage_material("notes.txt", encoded, "测试来源说明", False)
        with self.assertRaises(server.UploadError):
            server.stage_material("notes.txt", encoded, "无", True)

    def test_staging_rejects_a_file_when_nothing_can_be_extracted(self):
        encoded = base64.b64encode(b"%PDF-1.4\nnot a real PDF\n%%EOF").decode("ascii")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(server.UploadError, "未提取到可复核内容"):
                server.stage_material(
                    "broken.pdf", encoded, "待复核的测试 PDF", True,
                    incoming_dir=root / "incoming", staging_root=root / "staged",
                )

            self.assertEqual(1, len(list((root / "incoming").iterdir())))
            self.assertFalse((root / "staged").exists())


class TestFormalExaminerBoundary(unittest.TestCase):
    def test_examiner_only_uses_formal_demo_sources(self):
        examiner = server._examiner()
        if examiner is None:
            self.skipTest("命题审核器未启用")

        excluded = {"KB-015", "KB-016", "KB-018"}
        self.assertFalse(excluded & {chunk.id for chunk in examiner.retriever.chunks})


class TestShowcaseTemplate(unittest.TestCase):
    def test_ability_renderers_are_executable_javascript_not_css(self):
        template = Path("web/showcase.template.html").read_text(encoding="utf-8")
        style = re.search(r"<style>(.*?)</style>", template, re.S)
        scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", template, re.S)

        self.assertIsNotNone(style)
        self.assertNotIn("function paintRadar", style.group(1))
        self.assertNotIn("function paintEvidence", style.group(1))
        self.assertTrue(any("function paintRadar" in script for script in scripts))
        self.assertTrue(any("function paintEvidence" in script for script in scripts))

    def test_source_links_are_rendered_from_verified_provenance_text(self):
        index = Path("web/index.html").read_text(encoding="utf-8")
        template = Path("web/showcase.template.html").read_text(encoding="utf-8")

        self.assertIn("function sourceWithLink", index)
        self.assertIn("sourceWithLink(kb.source)", index)
        self.assertIn("function sourceWithLink", template)
        self.assertIn("sourceWithLink(kb.source)", template)


if __name__ == "__main__":
    unittest.main()
