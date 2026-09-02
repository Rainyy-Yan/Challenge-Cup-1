"""Validation for the source manifest exposed by the formal Demo."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

import config


MANIFEST_PATH = config.DATA / "demo_source_manifest.json"
HUMAN_REVIEW_FIELDS = ("reviewer", "reviewed_on", "conclusion", "authorization")


class DemoSourceManifestError(ValueError):
    """The published Demo source set and its review manifest disagree."""


def manifest_records(path: Path = MANIFEST_PATH) -> list[dict]:
    """Read manifest records and fail closed on malformed or duplicate IDs."""
    data = json.loads(path.read_text(encoding="utf-8"))
    records = data.get("records")
    if not isinstance(records, list):
        raise DemoSourceManifestError("Demo 来源台账缺少 records 列表")

    ids: list[str] = []
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("id"), str):
            raise DemoSourceManifestError("Demo 来源台账包含无效记录 ID")
        ids.append(record["id"])

    if len(ids) != len(set(ids)):
        raise DemoSourceManifestError("Demo 来源台账包含重复记录 ID")
    return records


def manifest_source_ids(path: Path = MANIFEST_PATH) -> set[str]:
    """Return all source IDs registered in the Demo manifest."""
    return {record["id"] for record in manifest_records(path)}


def publicly_verified_source_ids(path: Path = MANIFEST_PATH) -> set[str]:
    """Return sources backed by a complete human verification record."""
    verified: set[str] = set()
    for record in manifest_records(path):
        if record.get("review_status") != "human_verified":
            continue
        missing = [
            field for field in HUMAN_REVIEW_FIELDS
            if not isinstance(record.get(field), str) or not record[field].strip()
        ]
        if missing:
            raise DemoSourceManifestError(
                f"{record['id']} 声称 human_verified 但缺少人工复核字段：{', '.join(missing)}"
            )
        verified.add(record["id"])
    return verified


def validate_demo_source_manifest(source_ids: Iterable[str], *, artifact: str) -> None:
    """Require each source exposed by an artifact to have one manifest record."""
    actual = set(source_ids)
    expected = manifest_source_ids()
    absent_from_artifact = sorted(expected - actual)
    absent_from_manifest = sorted(actual - expected)
    if absent_from_artifact or absent_from_manifest:
        details = []
        if absent_from_artifact:
            details.append("台账有而产物未暴露：" + ", ".join(absent_from_artifact))
        if absent_from_manifest:
            details.append("产物有而台账未登记：" + ", ".join(absent_from_manifest))
        raise DemoSourceManifestError(
            f"{artifact} 的 Demo 来源台账不一致（{'；'.join(details)}）"
        )
