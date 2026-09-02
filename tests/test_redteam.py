"""正式 Demo 红队报告的回归门禁。"""

import unittest

import config
from core.retrieval import Retriever
from evalkit.redteam import FIXTURES, TRUE_CLAIMS, run


class TestRedteam(unittest.TestCase):
    def test_corpus_only_cites_formal_demo_sources(self):
        source_ids = {
            chunk.id for chunk in
            Retriever.from_jsonl(config.KB_PATH, demo_only=True).chunks
        }
        cited = {
            source_id for category, _, source_id, _ in FIXTURES
            if source_id is not None and category != "H2"
        }
        cited.update(source_id for _, source_id in TRUE_CLAIMS)
        self.assertLessEqual(cited, source_ids)

    def test_h5_h6_close_without_false_positives(self):
        result = run()
        for category in ("H5", "H6"):
            row = result["by_category"][category]
            self.assertEqual(row["caught"], row["total"])
            self.assertEqual(row["missed"], [])
        expected_note = {"H5": "适用范围", "H6": "条件或步骤"}
        for detail in result["detail"]:
            category = detail["category"]
            if category not in expected_note:
                continue
            self.assertEqual(detail["verdict"], "contradicted", detail)
            self.assertIn(expected_note[category], detail["audit_note"], detail)
        self.assertEqual(result["summary"]["false_positives"], 0)
        self.assertEqual(result["summary"]["false_positive_rate"], 0.0)


if __name__ == "__main__":
    unittest.main()
