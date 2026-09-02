"""Behavioral tests for the standalone qykw coverage gate."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "tools" / "check_qykw_coverage.py"


def coverage_payload(
    *,
    statements: object = 95.0,
    branches: object = 90.0,
    branch_mode: object = True,
    num_branches: object = 10,
) -> dict[str, object]:
    """Return the relevant coverage.py 7.16 JSON fields as literal fixture data."""

    return {
        "meta": {
            "format": 3,
            "version": "7.16.0",
            "timestamp": "2026-09-02T00:00:00Z",
            "branch_coverage": branch_mode,
            "show_contexts": False,
        },
        "files": {},
        "totals": {
            "covered_lines": 95,
            "num_statements": 100,
            "percent_covered": 94.5,
            "percent_covered_display": "94",
            "missing_lines": 5,
            "excluded_lines": 0,
            "percent_statements_covered": statements,
            "percent_statements_covered_display": str(statements),
            "num_branches": num_branches,
            "num_partial_branches": 0,
            "covered_branches": 9,
            "missing_branches": 1,
            "percent_branches_covered": branches,
            "percent_branches_covered_display": str(branches),
        },
    }


class TestQykwCoverageGate(unittest.TestCase):
    def run_checker(self, payload: object, *arguments: str) -> tuple[subprocess.CompletedProcess[str], bytes, tuple[str, ...]]:
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "coverage.json"
            report.write_text(json.dumps(payload), encoding="utf-8")
            before = report.read_bytes()
            process = subprocess.run(
                [sys.executable, str(CHECKER), str(report), *arguments],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
            after = report.read_bytes()
            names = tuple(sorted(item.name for item in Path(directory).iterdir()))
        self.assertEqual(after, before, "coverage gate must not rewrite its input")
        return process, after, names

    def test_exact_independent_thresholds_pass_without_writes(self) -> None:
        process, _, names = self.run_checker(
            coverage_payload(), "--line", "95", "--branch", "90"
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("statements 95.00%", process.stdout)
        self.assertIn("branches 90.00%", process.stdout)
        self.assertEqual(names, ("coverage.json",))

    def test_statement_threshold_does_not_use_combined_percentage(self) -> None:
        payload = coverage_payload(statements=94.99, branches=100.0)
        payload["totals"]["percent_covered"] = 99.99  # type: ignore[index]
        process, _, _ = self.run_checker(payload, "--line", "95", "--branch", "90")
        self.assertEqual(process.returncode, 1)
        self.assertIn("statement coverage 94.99% is below 95.00%", process.stderr)

    def test_branch_threshold_is_checked_separately(self) -> None:
        process, _, _ = self.run_checker(
            coverage_payload(statements=100.0, branches=89.99),
            "--line", "95", "--branch", "90",
        )
        self.assertEqual(process.returncode, 1)
        self.assertIn("branch coverage 89.99% is below 90.00%", process.stderr)

    def test_missing_or_disabled_branch_mode_fails_closed(self) -> None:
        missing = coverage_payload()
        del missing["meta"]["branch_coverage"]  # type: ignore[index]
        for payload in (missing, coverage_payload(branch_mode=False)):
            with self.subTest(payload=payload):
                process, _, _ = self.run_checker(payload)
                self.assertEqual(process.returncode, 2)
                self.assertIn("branch coverage mode", process.stderr)

    def test_missing_branch_percentage_and_zero_branch_projects_fail_closed(self) -> None:
        missing = coverage_payload()
        del missing["totals"]["percent_branches_covered"]  # type: ignore[index]
        for payload in (missing, coverage_payload(branches=100.0, num_branches=0)):
            with self.subTest(payload=payload):
                process, _, _ = self.run_checker(payload)
                self.assertEqual(process.returncode, 2)
                self.assertIn("branch", process.stderr)

    def test_invalid_json_and_invalid_numeric_fields_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "coverage.json"
            report.write_text("{not-json", encoding="utf-8")
            process = subprocess.run(
                [sys.executable, str(CHECKER), str(report)], cwd=ROOT,
                text=True, capture_output=True, timeout=5, check=False,
            )
        self.assertEqual(process.returncode, 2)
        self.assertNotIn("Traceback", process.stderr)

        for value in (True, "95", None, float("nan")):
            with self.subTest(value=value):
                process, _, _ = self.run_checker(coverage_payload(statements=value))
                self.assertEqual(process.returncode, 2)


if __name__ == "__main__":
    unittest.main()
