"""在线 Demo 的知识来源台账必须与正式知识库同步。

台账不是把机器核验伪装成人工核验：它固定正式演示实际用到的切片、来源定位、
仓库原文片段和人工复核字段。这样在线暴露范围改变时会立即在测试中暴露。
"""

import hashlib
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
SOURCE_INDEX_PATH = ROOT / "data" / "sources" / "index.json"


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
            "authorization", "source_slice", "source_slice_sha256",
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

    def test_all_formal_demo_sources_have_completed_human_review(self):
        """项目负责人已逐条复核的 20 条来源必须完整回填并发布。"""
        self.assertEqual(20, len(self.records))
        for chunk_id, record in self.records.items():
            self.assertEqual("human_verified", record["review_status"], chunk_id)
            self.assertEqual("xyh202131", record["reviewer"], chunk_id)
            self.assertEqual("2026-09-03", record["reviewed_on"], chunk_id)
            self.assertTrue(self.kb[chunk_id]["verified"], chunk_id)

    def test_source_slice_index_matches_manifest_and_file_hashes(self):
        """GitHub 内的原文片段索引必须覆盖同一批来源且能检出篡改。"""
        self.assertTrue(SOURCE_INDEX_PATH.is_file(), "缺少原文片段索引")
        index = json.loads(SOURCE_INDEX_PATH.read_text(encoding="utf-8"))
        indexed = {row["id"]: row for row in index["records"]}
        self.assertEqual(set(self.records), set(indexed))

        for chunk_id, record in self.records.items():
            entry = indexed[chunk_id]
            self.assertEqual(record["url"], entry["source_url"], chunk_id)
            self.assertEqual(record["locator"], entry["locator"], chunk_id)
            self.assertEqual(record["source_slice"], entry["slice_path"], chunk_id)
            self.assertEqual(
                record["source_slice_sha256"], entry["sha256"], chunk_id
            )
            slice_path = ROOT / entry["slice_path"]
            self.assertTrue(slice_path.is_file(), chunk_id)
            slice_bytes = slice_path.read_bytes()
            self.assertIn(
                entry["source_url"], slice_bytes.decode("utf-8"), chunk_id
            )
            normalized = slice_bytes.replace(b"\r\n", b"\n").replace(
                b"\r", b"\n"
            )
            self.assertEqual(
                entry["sha256"], hashlib.sha256(normalized).hexdigest(), chunk_id
            )

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
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            tmp_path = Path(tmp)
            source_slice = tmp_path / "KB-OK.md"
            source_slice.write_text("source excerpt", encoding="utf-8")
            path = tmp_path / "manifest.json"
            path.write_text(json.dumps({"records": [
                {
                    "id": "KB-OK",
                    "review_status": "human_verified",
                    "reviewer": "reviewer@example.org",
                    "reviewed_on": "2026-09-02",
                    "conclusion": "来源与摘录一致",
                    "authorization": "内部复核记录",
                    "source_slice": source_slice.relative_to(ROOT).as_posix(),
                    "source_slice_sha256": "e3cb9c650c9808320ed0d1d8ed42fe6bda4a7e5c2c1ffb46bdd3ba236604b81a",
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

    def test_human_verified_record_without_source_slice_fails_closed(self):
        """人工通过记录必须同时绑定仓库内可复核的原文片段。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            path.write_text(json.dumps({"records": [{
                "id": "KB-NO-SLICE",
                "review_status": "human_verified",
                "reviewer": "xyh202131",
                "reviewed_on": "2026-09-03",
                "conclusion": "原文支持知识切片",
                "authorization": "公开来源，仅收录项目实际引用片段",
            }]}, ensure_ascii=False), encoding="utf-8")

            with self.assertRaisesRegex(DemoSourceManifestError, "原文片段"):
                publicly_verified_source_ids(path)

    def test_human_verified_record_with_tampered_source_slice_fails_closed(self):
        """原文片段内容与登记哈希不一致时不得发布为已核实。"""
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            tmp_path = Path(tmp)
            slice_path = tmp_path / "KB-TAMPERED.md"
            slice_path.write_text("source excerpt", encoding="utf-8")
            relative_slice = slice_path.relative_to(ROOT).as_posix()
            manifest_path = tmp_path / "manifest.json"
            manifest_path.write_text(json.dumps({"records": [{
                "id": "KB-TAMPERED",
                "review_status": "human_verified",
                "reviewer": "xyh202131",
                "reviewed_on": "2026-09-03",
                "conclusion": "原文支持知识切片",
                "authorization": "公开来源，仅收录项目实际引用片段",
                "source_slice": relative_slice,
                "source_slice_sha256": "0" * 64,
            }]}, ensure_ascii=False), encoding="utf-8")

            with self.assertRaisesRegex(DemoSourceManifestError, "哈希"):
                publicly_verified_source_ids(manifest_path)

    def test_source_slice_hash_is_stable_across_git_line_endings(self):
        """同一 Markdown 的 LF/CRLF checkout 不应改变已登记的内容哈希。"""
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            tmp_path = Path(tmp)
            slice_path = tmp_path / "KB-CRLF.md"
            slice_path.write_bytes(b"line one\r\nline two\r\n")
            manifest_path = tmp_path / "manifest.json"
            manifest_path.write_text(json.dumps({"records": [{
                "id": "KB-CRLF",
                "review_status": "human_verified",
                "reviewer": "xyh202131",
                "reviewed_on": "2026-09-03",
                "conclusion": "原文支持知识切片",
                "authorization": "公开来源，仅收录项目实际引用片段",
                "source_slice": slice_path.relative_to(ROOT).as_posix(),
                "source_slice_sha256": "e9024f1a07d29d52ad3aa5e1a18e94db1f3a9fd32b89e39d47c472cd99071e13",
            }]}, ensure_ascii=False), encoding="utf-8")

            try:
                published = publicly_verified_source_ids(manifest_path)
            except DemoSourceManifestError as exc:
                self.fail(f"仅换行符变化不应破坏片段哈希：{exc}")
            self.assertEqual({"KB-CRLF"}, published)

    def test_missing_source_slice_fails_closed_without_leaking_local_path(self):
        """缺失片段必须阻断发布，错误信息不得暴露本机绝对路径。"""
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            manifest_path = Path(tmp) / "manifest.json"
            manifest_path.write_text(json.dumps({"records": [{
                "id": "KB-MISSING",
                "review_status": "human_verified",
                "reviewer": "xyh202131",
                "reviewed_on": "2026-09-03",
                "conclusion": "原文支持知识切片",
                "authorization": "公开来源，仅收录项目实际引用片段",
                "source_slice": "data/sources/slices/does-not-exist.md",
                "source_slice_sha256": "0" * 64,
            }]}, ensure_ascii=False), encoding="utf-8")

            with self.assertRaisesRegex(
                DemoSourceManifestError, "KB-MISSING.*无法读取"
            ) as raised:
                publicly_verified_source_ids(manifest_path)
            self.assertNotIn(str(ROOT), str(raised.exception))

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
