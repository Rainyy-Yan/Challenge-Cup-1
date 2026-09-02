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

    def run_checker_path(self, report: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CHECKER), str(report)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )

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

    def test_report_at_two_mebibyte_limit_is_accepted(self) -> None:
        maximum_size = 2 * 1024 * 1024
        encoded = json.dumps(coverage_payload(), separators=(",", ":")).encode("utf-8")
        self.assertLess(len(encoded), maximum_size)
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "coverage.json"
            report.write_bytes(encoded + b" " * (maximum_size - len(encoded)))
            process = self.run_checker_path(report)
        self.assertEqual(process.returncode, 0, process.stderr)

    def test_oversized_report_fails_closed_without_traceback(self) -> None:
        maximum_size = 2 * 1024 * 1024
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "coverage.json"
            report.write_bytes(b" " * (maximum_size + 1))
            process = self.run_checker_path(report)
        self.assertEqual(process.returncode, 2)
        self.assertIn("coverage report exceeds 2 MiB", process.stderr)
        self.assertNotIn("Traceback", process.stderr)

    def test_symlink_report_path_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            target = base / "target.json"
            target.write_text(json.dumps(coverage_payload()), encoding="utf-8")
            link = base / "coverage.json"
            try:
                link.symlink_to(target)
            except (NotImplementedError, OSError) as error:
                self.skipTest(f"symlink creation is unavailable: {error}")
            process = self.run_checker_path(link)
            self.assertEqual(process.returncode, 2)
            self.assertIn("regular file", process.stderr)
            self.assertNotIn("Traceback", process.stderr)

    def test_non_regular_report_path_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            process = self.run_checker_path(base)
            self.assertEqual(process.returncode, 2)
            self.assertIn("regular file", process.stderr)
            self.assertNotIn("Traceback", process.stderr)

    def test_deeply_nested_json_fails_closed_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "coverage.json"
            report.write_text("[" * 20000 + "0" + "]" * 20000, encoding="utf-8")
            process = self.run_checker_path(report)
        self.assertEqual(process.returncode, 2)
        self.assertIn("unreadable or invalid JSON", process.stderr)
        self.assertNotIn("Traceback", process.stderr)

    def test_oversized_integer_fails_closed_without_traceback(self) -> None:
        parser_limit = '{"value":' + "9" * 5000 + "}"
        percentage_overflow = json.dumps(
            coverage_payload(statements="HUGE"), separators=(",", ":")
        ).replace('"HUGE"', "9" * 400)
        for source in (parser_limit, percentage_overflow):
            with (
                self.subTest(length=len(source)),
                tempfile.TemporaryDirectory() as directory,
            ):
                report = Path(directory) / "coverage.json"
                report.write_text(source, encoding="utf-8")
                process = self.run_checker_path(report)
            self.assertEqual(process.returncode, 2)
            self.assertNotIn("Traceback", process.stderr)

    def test_invalid_utf8_fails_closed_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "coverage.json"
            report.write_bytes(b"\xff")
            process = self.run_checker_path(report)
        self.assertEqual(process.returncode, 2)
        self.assertIn("unreadable or invalid JSON", process.stderr)
        self.assertNotIn("Traceback", process.stderr)


if __name__ == "__main__":
    unittest.main()
