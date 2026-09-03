"""Validation for the source manifest exposed by the formal Demo."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path

import config


MANIFEST_PATH = config.DATA / "demo_source_manifest.json"
HUMAN_REVIEW_FIELDS = ("reviewer", "reviewed_on", "conclusion", "authorization")
SOURCE_SLICE_FIELDS = ("source_slice", "source_slice_sha256")


class DemoSourceManifestError(ValueError):
    """The published Demo source set and its review manifest disagree."""


def _source_slice_digest(content: bytes) -> str:
    """Hash text content canonically so Git line-ending conversion is harmless."""
    normalized = content.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(normalized).hexdigest()


def manifest_records(path: Path = MANIFEST_PATH) -> list[dict]:
    """Read manifest records and fail closed on unreadable or malformed data."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DemoSourceManifestError(
            f"无法读取 Demo 来源台账 {path}：{exc}"
        ) from exc
    if not isinstance(data, dict):
        raise DemoSourceManifestError("Demo 来源台账根节点必须是对象")
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
                f"{record['id']} 声称 human_verified 但缺少人工复核字段："
                f"{', '.join(missing)}"
            )
        missing_slice = [
            field for field in SOURCE_SLICE_FIELDS
            if not isinstance(record.get(field), str) or not record[field].strip()
        ]
        if missing_slice:
            raise DemoSourceManifestError(
                f"{record['id']} 声称 human_verified 但缺少原文片段字段："
                f"{', '.join(missing_slice)}"
            )
        relative_slice = Path(record["source_slice"])
        if relative_slice.is_absolute():
            raise DemoSourceManifestError(
                f"{record['id']} 的原文片段路径必须位于仓库内"
            )
        source_slice = (config.ROOT / relative_slice).resolve()
        try:
            source_slice.relative_to(config.ROOT.resolve())
        except ValueError as exc:
            raise DemoSourceManifestError(
                f"{record['id']} 的原文片段路径越出仓库"
            ) from exc
        try:
            content = source_slice.read_bytes()
        except OSError as exc:
            raise DemoSourceManifestError(
                f"{record['id']} 的原文片段无法读取：{relative_slice.as_posix()}"
            ) from exc
        actual_hash = _source_slice_digest(content)
        if actual_hash != record["source_slice_sha256"]:
            raise DemoSourceManifestError(
                f"{record['id']} 的原文片段哈希不匹配"
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
