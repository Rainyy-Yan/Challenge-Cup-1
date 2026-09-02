"""P22 regression tests for the sources exposed by the demo service."""

from __future__ import annotations

import json
import unittest

import config
import server
from core.retrieval import Retriever
from orchestrator import Orchestrator, load_profile


class TestProvenancePayload(unittest.TestCase):
    def setUp(self) -> None:
        self.session_id = "p22-provenance"
        self.orchestrator = Orchestrator()
        session = self.orchestrator.run(load_profile("P-A"), max_kp=1)
        server.SESSIONS[self.session_id] = (self.orchestrator, session)

    def tearDown(self) -> None:
        server.SESSIONS.pop(self.session_id, None)

    def test_resources_include_locatable_source_metadata(self) -> None:
        payload = server.session_payload(self.session_id)
        source_id = payload["resources"][0]["claims"][0]["source_id"]
        source = payload["kb"][source_id]

        self.assertIn("https://", source["source"])
        self.assertIn("定位：", source["source"])
        self.assertIn("verified", source)
        self.assertIn("source_note", source)

    def test_session_source_set_matches_manifest(self) -> None:
        payload = server.session_payload(self.session_id)
        manifest = json.loads(
            (config.DATA / "demo_source_manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            set(payload["kb"]), {record["id"] for record in manifest["records"]}
        )

    def test_source_excluded_from_demo_is_not_exposed_as_verified(self) -> None:
        excluded = next(
            chunk for chunk in Retriever.from_jsonl(config.KB_PATH).chunks
            if chunk.id == "KB-015"
        )
        self.assertTrue(excluded.verified)
        self.assertFalse(excluded.demo_eligible)
        self.orchestrator.retriever.chunks.append(excluded)

        payload = server.session_payload(self.session_id)

        self.assertNotIn(excluded.id, payload["kb"])

    def test_pending_legacy_verification_is_not_published(self) -> None:
        """旧 verified 标记没有完整人工复核记录时不得对外宣称已核实。"""
        payload = server.session_payload(self.session_id)

        self.assertFalse(payload["kb"]["KB-004"]["verified"])
        self.assertFalse(payload["kb"]["KB-017"]["verified"])


if __name__ == "__main__":
    unittest.main()
