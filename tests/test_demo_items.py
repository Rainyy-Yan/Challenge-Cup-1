"""正式 Demo 固定题候选集不能夹带待改或已排除的题。"""

import json
import unittest
from pathlib import Path

import config
from agents.examiner import ExaminerAgent, ItemRejected
from core.demo_items import formal_demo_items
from core.llm import MockLLM
from core.retrieval import Retriever
from evalkit.itemreport import build


ROOT = Path(__file__).resolve().parents[1]
PRETEST_PATH = ROOT / "data" / "pretest.json"
ITEM_MANIFEST_PATH = ROOT / "data" / "demo_item_source_manifest.json"
EXCLUDED_IDS = {"Q-008", "Q-017", "Q-020", "Q-038", "Q-039", "Q-041", "Q-044"}


class TestFormalDemoItems(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.items = json.loads(PRETEST_PATH.read_text(encoding="utf-8"))["items"]
        cls.formal_items = formal_demo_items(cls.items)
        cls.item_manifest = json.loads(
            ITEM_MANIFEST_PATH.read_text(encoding="utf-8")
        )
        cls.item_sources = {
            record["id"] for record in cls.item_manifest["records"]
        }
        cls.retriever = Retriever.from_jsonl(config.KB_PATH, demo_only=True)
        cls.kps = {
            point["id"]: point
            for point in json.loads(config.KP_PATH.read_text(encoding="utf-8"))["points"]
        }

    def test_excluded_items_are_not_in_formal_demo_candidates(self):
        flagged = {item["id"] for item in self.items
                   if item.get("demo_eligible") is False}
        self.assertTrue(EXCLUDED_IDS <= flagged)
        self.assertFalse(EXCLUDED_IDS & {item["id"] for item in self.formal_items})

    def test_formal_demo_quality_report_has_no_pending_items(self):
        report = build(formal_demo=True)
        self.assertFalse(any(item["grade"] == "待改" for item in report["items"]))

    def test_every_formal_item_has_a_locatable_source_record(self):
        """正式题只引用可定位的非占位来源，且台账范围与题库完全一致。"""
        required = {
            "id", "source_title", "version", "locator", "url", "review_status",
            "reviewer", "reviewed_on", "conclusion", "authorization",
        }
        expected = {item.get("source_id") for item in self.formal_items}
        self.assertNotIn(None, expected)
        self.assertEqual(expected, self.item_sources)

        records = {record["id"]: record for record in self.item_manifest["records"]}
        for item in self.formal_items:
            source_id = item["source_id"]
            record = records[source_id]
            self.assertTrue(required <= set(record), source_id)
            self.assertTrue(record["url"].startswith("https://"), source_id)
            self.assertTrue(record["locator"], source_id)
            chunk = self.retriever.get(source_id)
            self.assertIsNotNone(chunk, source_id)
            self.assertNotIn("占位出处", chunk.source, source_id)
            self.assertTrue(chunk.demo_eligible, source_id)
            self.assertIn(record["url"], chunk.source, source_id)

    def test_every_formal_item_passes_the_evidence_gate(self):
        """带上 source_id 还不够；答案和选项仍必须逐题通过命题审核。"""
        examiner = ExaminerAgent(MockLLM(), self.retriever, self.kps)
        failures = []
        for item in self.formal_items:
            try:
                examiner.vet(dict(item))
            except ItemRejected as exc:
                failures.append(f"{item['id']}: {exc}")
        self.assertFalse(failures, "\n".join(failures))
