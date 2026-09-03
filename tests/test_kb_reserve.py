"""受控知识储备的运行时与来源状态契约。"""

import json
import re
import unittest
from pathlib import Path

from core.retrieval import Retriever


ROOT = Path(__file__).resolve().parents[1]
KB_PATH = ROOT / "data" / "kb" / "robotics.jsonl"
KP_PATH = ROOT / "data" / "knowledge_points.json"
DEMO_MANIFEST_PATH = ROOT / "data" / "demo_source_manifest.json"
SOURCE_LEDGER_PATH = ROOT / "docs" / "research" / "来源台账.md"
EVIDENCE_CARD_DIR = ROOT / "docs" / "research" / "evidence-cards"


def _load_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class TestKnowledgeReserve(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.rows = _load_jsonl(KB_PATH)
        cls.reserve = [
            row for row in cls.rows
            if 27 <= int(row["id"].removeprefix("KB-")) <= 120
        ]
        cls.knowledge_point_ids = {
            point["id"]
            for point in json.loads(KP_PATH.read_text(encoding="utf-8"))["points"]
        }

    def test_runtime_corpus_has_continuous_ids_through_kb_120(self):
        expected = [f"KB-{number:03d}" for number in range(1, 121)]
        self.assertEqual(expected, [row["id"] for row in self.rows])

    def test_new_records_remain_sourced_unverified_and_demo_disabled(self):
        self.assertEqual(94, len(self.reserve))
        for row in self.reserve:
            with self.subTest(chunk_id=row["id"]):
                self.assertEqual("sourced", row.get("origin"))
                self.assertIs(False, row.get("verified"))
                self.assertIs(False, row.get("demo_eligible"))

    def test_new_records_have_valid_knowledge_points_and_traceable_sources(self):
        for row in self.reserve:
            with self.subTest(chunk_id=row["id"]):
                self.assertIn(row["kp"], self.knowledge_point_ids)
                segments = [part.strip() for part in row["source"].split("｜")]
                self.assertEqual(4, len(segments))
                self.assertRegex(
                    segments[0],
                    r"^【待(?:人工)?核实(?:[·：:][^】]*)?】REF-[A-Z0-9-]+$",
                )
                self.assertTrue(segments[1], "来源定位不能为空")
                self.assertTrue(segments[2].startswith("https://"))
                self.assertRegex(segments[3], r"^sha:[0-9a-f]{64}$")
                self.assertGreaterEqual(len(row["text"]), 60)
                self.assertLessEqual(len(row["text"]), 400)

    def test_formal_demo_source_set_does_not_expand(self):
        manifest = json.loads(DEMO_MANIFEST_PATH.read_text(encoding="utf-8"))
        expected_ids = {record["id"] for record in manifest["records"]}
        formal_ids = {
            chunk.id
            for chunk in Retriever.from_jsonl(KB_PATH, demo_only=True).chunks
        }

        self.assertEqual(20, len(formal_ids))
        self.assertEqual(expected_ids, formal_ids)

    def test_new_source_refs_and_hashes_are_preserved_in_research_docs(self):
        self.assertTrue(SOURCE_LEDGER_PATH.is_file(), "缺少知识库来源台账")
        self.assertTrue(EVIDENCE_CARD_DIR.is_dir(), "缺少知识库证据卡目录")

        ledger = SOURCE_LEDGER_PATH.read_text(encoding="utf-8")
        card_by_ref = {
            match.group("ref"): SOURCE_LEDGER_PATH.parent / match.group("card")
            for match in re.finditer(
                r"^\|\s*(?P<ref>REF-[A-Z0-9-]+)\s*\|\s*"
                r"\[[^]]+\]\((?P<card>[^)]+)\)",
                ledger,
                re.MULTILINE,
            )
        }

        for row in self.reserve:
            with self.subTest(chunk_id=row["id"]):
                status_ref, _locator, url, digest = [
                    part.strip() for part in row["source"].split("｜")
                ]
                ref = re.search(r"REF-[A-Z0-9-]+", status_ref).group(0)
                self.assertIn(ref, card_by_ref)
                card_path = card_by_ref[ref]
                self.assertTrue(card_path.is_file())
                card = card_path.read_text(encoding="utf-8")
                self.assertIn(url, card)
                self.assertIn(digest.removeprefix("sha:"), card.lower())


if __name__ == "__main__":
    unittest.main()
