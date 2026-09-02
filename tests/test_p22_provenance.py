"""P22 regression tests for the sources exposed by the demo service."""

from __future__ import annotations

import unittest

import server
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


if __name__ == "__main__":
    unittest.main()
