"""Regression tests for formal-Demo retrieval boundaries."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.retrieval import Retriever


class TestDemoOnlyLoading(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
