"""Regression tests for formal-Demo retrieval boundaries."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.retrieval import Retriever


class TestDemoOnlyLoading(unittest.TestCase):
    @staticmethod
    def _valid_record() -> dict:
        return {
            "id": "KB-TEST",
            "kp": "KP-01",
            "title": "测试切片",
            "source": "测试来源",
            "text": "足够用于读取测试的正文",
        }

    def test_excluded_malformed_record_is_skipped_before_chunk_parsing(self) -> None:
        """An internal-only record cannot break the formal Demo loader."""
        record = {"id": "KB-EXCLUDED", "demo_eligible": False}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kb.jsonl"
            path.write_text(json.dumps(record) + "\n", encoding="utf-8")

            retriever = Retriever.from_jsonl(path, demo_only=True)

        self.assertEqual([], retriever.chunks)

    def test_full_loader_reports_the_invalid_source_id(self) -> None:
        """Internal review still gets a source ID when its record is malformed."""
        record = {"id": "KB-EXCLUDED", "demo_eligible": False}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kb.jsonl"
            path.write_text(json.dumps(record) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "KB-EXCLUDED.*字段无效"):
                Retriever.from_jsonl(path)

    def test_verified_must_be_a_json_boolean(self) -> None:
        record = {**self._valid_record(), "verified": "false"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kb.jsonl"
            path.write_text(json.dumps(record) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "KB-TEST.*verified.*布尔"):
                Retriever.from_jsonl(path)

    def test_demo_eligibility_must_be_a_json_boolean_before_filtering(self) -> None:
        record = {**self._valid_record(), "demo_eligible": "false"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kb.jsonl"
            path.write_text(json.dumps(record) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "KB-TEST.*demo_eligible.*布尔"):
                Retriever.from_jsonl(path, demo_only=True)


if __name__ == "__main__":
    unittest.main()
