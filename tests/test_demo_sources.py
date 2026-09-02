"""在线 Demo 的知识来源台账必须与正式知识库同步。

台账不是把机器核验伪装成人工核验：它只固定正式演示实际用到的切片、
来源定位和待填写的人工复核字段。这样在线暴露范围改变时会立即在测试中暴露。
"""

import json
import tempfile
import unittest
from pathlib import Path

from core.demo_sources import (
    DemoSourceManifestError,
    manifest_source_ids,
    publicly_verified_source_ids,
    validate_demo_source_manifest,
)
from core.retrieval import Retriever


ROOT = Path(__file__).resolve().parents[1]
KB_PATH = ROOT / "data" / "kb" / "robotics.jsonl"
MANIFEST_PATH = ROOT / "data" / "demo_source_manifest.json"


class TestDemoSourceManifest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.kb = {
            row["id"]: row
            for row in (
                json.loads(line)
                for line in KB_PATH.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
        }
        cls.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        cls.records = {row["id"]: row for row in cls.manifest["records"]}
        cls.formal = Retriever.from_jsonl(KB_PATH, demo_only=True)

    def test_manifest_covers_exactly_the_sources_exposed_online(self):
        self.assertEqual(
            set(self.records),
            {chunk.id for chunk in self.formal.chunks},
        )

    def test_online_source_set_passes_runtime_manifest_validation(self):
        validate_demo_source_manifest(
            {chunk.id for chunk in self.formal.chunks}, artifact="测试在线会话"
        )

    def test_each_formal_demo_source_has_a_locatable_source_record(self):
        required = {
            "id", "profiles", "source_title", "version", "locator", "url",
            "review_status", "reviewer", "reviewed_on", "conclusion",
            "authorization",
        }
        for chunk_id, record in self.records.items():
            self.assertTrue(required <= set(record), chunk_id)
            for field in ("source_title", "version", "locator", "url"):
                self.assertTrue(record[field], f"{chunk_id} 缺少 {field}")
            self.assertTrue(record["url"].startswith("https://"), chunk_id)

            chunk = self.kb[chunk_id]
            self.assertIn(record["url"], chunk["source"], chunk_id)
            self.assertIn("定位：", chunk["source"], chunk_id)
            self.assertNotIn("占位出处", chunk["source"], chunk_id)

    def test_human_verification_fields_are_never_forged(self):
        """机器可登记待审状态，只有完整人工记录才能声称 human_verified。"""
        allowed = {
            "pending_manual_review",
            "legacy_verified_record_pending",
            "human_verified",
            "rejected",
        }
        for chunk_id, record in self.records.items():
            status = record["review_status"]
            self.assertIn(status, allowed, chunk_id)
            chunk = self.kb[chunk_id]

            if status == "human_verified":
                for field in ("reviewer", "reviewed_on", "conclusion", "authorization"):
                    self.assertTrue(record[field], f"{chunk_id} 缺少人工复核 {field}")
                self.assertTrue(chunk["verified"], chunk_id)
            elif chunk["verified"]:
                self.assertEqual("legacy_verified_record_pending", status, chunk_id)
            else:
                self.assertEqual("pending_manual_review", status, chunk_id)

    def test_online_demo_only_publishes_completed_human_reviews(self):
        """在线会话的核实状态必须由完整人工复核台账决定。"""
        review_fields = ("reviewer", "reviewed_on", "conclusion", "authorization")
        published = publicly_verified_source_ids()
        for chunk_id, record in self.records.items():
            expected = (
                record["review_status"] == "human_verified"
                and all(record[field] for field in review_fields)
                and self.kb[chunk_id]["verified"]
            )
            self.assertEqual(expected, chunk_id in published, chunk_id)

    def test_public_verification_requires_a_complete_human_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            path.write_text(json.dumps({"records": [
                {
                    "id": "KB-OK",
                    "review_status": "human_verified",
                    "reviewer": "reviewer@example.org",
                    "reviewed_on": "2026-09-02",
                    "conclusion": "来源与摘录一致",
                    "authorization": "内部复核记录",
                },
                {"id": "KB-PENDING", "review_status": "pending_manual_review"},
            ]}, ensure_ascii=False), encoding="utf-8")

            self.assertEqual({"KB-OK"}, publicly_verified_source_ids(path))

    def test_incomplete_human_record_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            path.write_text(json.dumps({"records": [{
                "id": "KB-BROKEN",
                "review_status": "human_verified",
                "reviewer": "",
                "reviewed_on": "2026-09-02",
                "conclusion": "来源与摘录一致",
                "authorization": "内部复核记录",
            }]}, ensure_ascii=False), encoding="utf-8")

            with self.assertRaisesRegex(DemoSourceManifestError, "KB-BROKEN"):
                publicly_verified_source_ids(path)

    def test_missing_manifest_uses_the_domain_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.json"
            with self.assertRaisesRegex(
                DemoSourceManifestError, "无法读取 Demo 来源台账"
            ):
                manifest_source_ids(path)

    def test_malformed_manifest_uses_the_domain_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaisesRegex(
                DemoSourceManifestError, "无法读取 Demo 来源台账"
            ):
                manifest_source_ids(path)

    def test_source_unavailable_chunks_are_excluded_from_formal_demo(self):
        """失效的资料可留在原始库复核，但不能随正式 Demo 重新出现。"""
        excluded = {"KB-015", "KB-016", "KB-018"}
        self.assertTrue(all(not self.kb[chunk_id]["demo_eligible"]
                            for chunk_id in excluded))

        formal_demo = Retriever.from_jsonl(KB_PATH, demo_only=True)
        self.assertFalse(excluded & {chunk.id for chunk in formal_demo.chunks})
