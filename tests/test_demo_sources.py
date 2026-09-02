"""最终 Demo 的知识来源台账必须随快照一起更新。

台账不是把机器核验伪装成人工核验：它只固定正式演示实际用到的切片、
来源定位和待填写的人工复核字段。这样快照改变时，来源范围会立即在测试中暴露。
"""

import json
import unittest
from pathlib import Path

from core.demo_sources import validate_demo_source_manifest
from core.retrieval import Retriever


ROOT = Path(__file__).resolve().parents[1]
KB_PATH = ROOT / "data" / "kb" / "robotics.jsonl"
SNAPSHOT_PATH = ROOT / "web" / "snapshot.json"
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
        cls.snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        cls.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        cls.records = {row["id"]: row for row in cls.manifest["records"]}

    def _demo_sources_by_profile(self):
        result = {}
        for profile_id, session in self.snapshot["sessions"].items():
            result[profile_id] = {
                claim["source_id"]
                for resource in session["resources"]
                for claim in resource["claims"]
                if claim.get("source_id")
            }
        return result

    def test_manifest_covers_exactly_the_sources_embedded_in_snapshot(self):
        used_by_profile = self._demo_sources_by_profile()
        used = set().union(*used_by_profile.values())
        self.assertEqual(set(self.records), set(self.snapshot["kb"]))
        self.assertLessEqual(used, set(self.snapshot["kb"]))

        for chunk_id, record in self.records.items():
            expected_profiles = sorted(
                profile_id for profile_id, ids in used_by_profile.items()
                if chunk_id in ids
            )
            self.assertEqual(sorted(record["profiles"]), expected_profiles, chunk_id)

    def test_snapshot_source_set_passes_runtime_manifest_validation(self):
        validate_demo_source_manifest(self.snapshot["kb"], artifact="测试快照")

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

    def test_source_unavailable_chunks_are_excluded_from_formal_demo(self):
        """失效的资料可留在原始库复核，但不能随正式 Demo 重新出现。"""
        excluded = {"KB-015", "KB-016", "KB-018"}
        self.assertTrue(all(not self.kb[chunk_id]["demo_eligible"]
                            for chunk_id in excluded))

        formal_demo = Retriever.from_jsonl(KB_PATH, demo_only=True)
        self.assertFalse(excluded & {chunk.id for chunk in formal_demo.chunks})

        self.assertFalse(excluded & set(self.snapshot["kb"]))
        used = set().union(*self._demo_sources_by_profile().values())
        self.assertFalse(excluded & used)
