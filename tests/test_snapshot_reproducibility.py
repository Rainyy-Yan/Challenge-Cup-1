"""Reproducibility checks for generated competition artifacts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent


def without_timings(value: Any) -> Any:
    """Remove runtime-only timing fields before comparing snapshots."""
    if isinstance(value, dict):
        return {
            key: without_timings(item)
            for key, item in value.items()
            if key != "ms"
        }
    if isinstance(value, list):
        return [without_timings(item) for item in value]
    return value


class TestSnapshotReproducibility(unittest.TestCase):
    def test_snapshot_does_not_depend_on_python_hash_seed(self) -> None:
        snapshots = []
        with tempfile.TemporaryDirectory() as directory:
            for seed in ("1", "2"):
                output = Path(directory) / f"snapshot-{seed}.json"
                env = os.environ.copy()
                env["PYTHONHASHSEED"] = seed
                subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "evalkit.snapshot",
                        "--out",
                        str(output),
                    ],
                    cwd=ROOT,
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                snapshots.append(
                    without_timings(json.loads(output.read_text(encoding="utf-8")))
                )

        self.assertEqual(snapshots[0], snapshots[1])


if __name__ == "__main__":
    unittest.main()
