"""Fail-closed statement and branch thresholds for coverage.py JSON reports."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import json
import math
from pathlib import Path
import sys


_COVERAGE_VERSION = "7.16.0"


class CoverageDataError(ValueError):
    """Raised when a report cannot prove the configured coverage contract."""


def _percentage(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CoverageDataError(f"invalid {field}")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 100.0:
        raise CoverageDataError(f"invalid {field}")
    return number


def _positive_count(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CoverageDataError(f"invalid or zero {field}")
    return value


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise CoverageDataError(f"missing or invalid {field}")
    return value


def read_percentages(path: Path) -> tuple[float, float]:
    """Read separate statement and branch percentages without modifying *path*."""

    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CoverageDataError("coverage report is unreadable or invalid JSON") from error
    root = _mapping(report, "report")
    meta = _mapping(root.get("meta"), "meta")
    if meta.get("version") != _COVERAGE_VERSION:
        raise CoverageDataError(f"coverage report version must be {_COVERAGE_VERSION}")
    if meta.get("branch_coverage") is not True:
        raise CoverageDataError("branch coverage mode is missing or disabled")
    totals = _mapping(root.get("totals"), "totals")
    _positive_count(totals.get("num_statements"), "statement count")
    _positive_count(totals.get("num_branches"), "branch count")
    statements = _percentage(
        totals.get("percent_statements_covered"), "statement coverage percentage"
    )
    branches = _percentage(
        totals.get("percent_branches_covered"), "branch coverage percentage"
    )
    return statements, branches


def _threshold(value: str) -> float:
    try:
        return _percentage(float(value), "threshold")
    except (ValueError, CoverageDataError) as error:
        raise argparse.ArgumentTypeError("threshold must be a finite number from 0 to 100") from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("report", type=Path)
    parser.add_argument("--line", type=_threshold, default=95.0,
                        help="minimum statement coverage percentage")
    parser.add_argument("--branch", type=_threshold, default=90.0,
                        help="minimum branch coverage percentage")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        statements, branches = read_percentages(arguments.report)
    except CoverageDataError as error:
        print(f"qykw coverage data error: {error}", file=sys.stderr)
        return 2

    failures: list[str] = []
    if statements < arguments.line:
        failures.append(
            f"statement coverage {statements:.2f}% is below {arguments.line:.2f}%"
        )
    if branches < arguments.branch:
        failures.append(
            f"branch coverage {branches:.2f}% is below {arguments.branch:.2f}%"
        )
    if failures:
        for failure in failures:
            print(f"qykw coverage failed: {failure}", file=sys.stderr)
        return 1
    print(f"qykw coverage: statements {statements:.2f}%, branches {branches:.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
