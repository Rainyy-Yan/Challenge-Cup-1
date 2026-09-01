from __future__ import annotations

from pathlib import Path
import runpy
import unittest
from unittest.mock import patch

from tools import minimax_review
from tools.qykw.__main__ import main as qykw_main


class TestLegacyQykwDelegate(unittest.TestCase):
    def test_wrapper_exposes_the_qykw_entry_point(self) -> None:
        self.assertIs(minimax_review.main, qykw_main)

    def test_script_delegates_the_qykw_exit_code(self) -> None:
        wrapper = Path(minimax_review.__file__)
        with patch("tools.qykw.__main__.main", return_value=7):
            with self.assertRaises(SystemExit) as caught:
                runpy.run_path(str(wrapper), run_name="__main__")
        self.assertEqual(caught.exception.code, 7)

    def test_wrapper_does_not_expose_legacy_provider_or_model_api(self) -> None:
        forbidden = {
            "ReviewConfig", "ReviewError", "review_pull_request", "build_minimax_payload",
            "parse_review_result", "parse_changed_lines", "urlopen", "MINIMAX_API_KEY",
        }
        self.assertTrue(forbidden.isdisjoint(vars(minimax_review)))


if __name__ == "__main__":
    unittest.main()
