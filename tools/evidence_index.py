"""Validate and render the G0-G4 delivery evidence index."""

from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INDEX = ROOT / "delivery" / "evidence" / "index.json"
DEFAULT_MARKDOWN = ROOT / "delivery" / "evidence" / "INDEX.md"

GATES = {f"G{number}" for number in range(5)}
CATEGORIES = {
    "specification",
    "raw-data",
    "statistical-result",
    "human-review",
    "runtime-artifact",
    "presentation",
    "package-control",
}
STATUSES = {"planned", "working", "candidate", "approved", "superseded"}
VISIBILITIES = {"public", "restricted"}
REQUIRED_ENTRY_FIELDS = {
    "id",
    "gate",
    "category",
    "title",
    "path",
    "status",
    "owner",
    "related_issues",
    "source",
    "repo_commit",
    "visibility",
    "limitations",
}
EVIDENCE_ID = re.compile(r"^EV-G[0-4]-[A-Z0-9]+(?:-[A-Z0-9]+)*-\d{3}$")
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
AMBIGUOUS_NAME = re.compile(
    r"(?:^|[-_. ])(?:final(?:[-_. ]?v?\d+)?|latest|new|old|copy)(?:[-_. ]|$)"
    r"|最终版|最新版|副本",
    re.IGNORECASE,
)

CATEGORY_LABELS = {
    "specification": "规范",
    "raw-data": "原始数据",
    "statistical-result": "统计结果",
    "human-review": "人工复核",
    "runtime-artifact": "运行产物",
    "presentation": "展示材料",
    "package-control": "封包控制",
}
STATUS_LABELS = {
    "planned": "计划",
    "working": "工作稿",
    "candidate": "候选",
    "approved": "已批准",
    "superseded": "已替代",
}


def load_index(path: Path = DEFAULT_INDEX) -> dict[str, Any]:
    """Load the canonical JSON index."""
    return json.loads(path.read_text(encoding="utf-8"))


def _valid_repository_path(value: str) -> bool:
    if not value or "\\" in value or value.startswith(("/", "~")) or ":" in value:
        return False
    if re.match(r"^[A-Za-z]:", value):
        return False
    if any(part in {"", ".", ".."} for part in value.split("/")):
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts


def validate_index(data: dict[str, Any], root: Path = ROOT) -> list[str]:
    """Return all structural, naming, and path errors in an index."""
    errors: list[str] = []
    if data.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if not isinstance(data.get("project"), str) or not data["project"].strip():
        errors.append("project must be a non-empty string")
    try:
        date.fromisoformat(str(data.get("updated_on", "")))
    except ValueError:
        errors.append("updated_on must use YYYY-MM-DD")

    entries = data.get("entries")
    if not isinstance(entries, list) or not entries:
        return errors + ["entries must be a non-empty list"]

    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    covered_gates: set[str] = set()
    for position, entry in enumerate(entries, start=1):
        prefix = f"entries[{position}]"
        if not isinstance(entry, dict):
            errors.append(f"{prefix} must be an object")
            continue
        missing = sorted(REQUIRED_ENTRY_FIELDS - entry.keys())
        if missing:
            errors.append(f"{prefix} missing fields: {', '.join(missing)}")
            continue

        evidence_id = entry["id"]
        gate = entry["gate"]
        path_text = entry["path"]
        status = entry["status"]
        if not isinstance(evidence_id, str) or not EVIDENCE_ID.fullmatch(evidence_id):
            errors.append(f"{prefix}.id has an invalid evidence ID")
        elif evidence_id in seen_ids:
            errors.append(f"duplicate evidence ID: {evidence_id}")
        else:
            seen_ids.add(evidence_id)

        if not isinstance(gate, str) or gate not in GATES:
            errors.append(f"{prefix}.gate must be G0-G4")
        else:
            covered_gates.add(gate)
            if isinstance(evidence_id, str) and not evidence_id.startswith(f"EV-{gate}-"):
                errors.append(f"{prefix}.id does not match gate {gate}")
        if not isinstance(entry["category"], str) or entry["category"] not in CATEGORIES:
            errors.append(f"{prefix}.category is not recognized")
        if not isinstance(status, str) or status not in STATUSES:
            errors.append(f"{prefix}.status is not recognized")
        if (not isinstance(entry["visibility"], str)
                or entry["visibility"] not in VISIBILITIES):
            errors.append(f"{prefix}.visibility is not recognized")

        if not isinstance(path_text, str) or not _valid_repository_path(path_text):
            errors.append(f"{prefix}.path must be a safe repository-relative POSIX path")
        elif path_text in seen_paths:
            errors.append(f"duplicate evidence path: {path_text}")
        else:
            seen_paths.add(path_text)
            if any(AMBIGUOUS_NAME.search(part) for part in PurePosixPath(path_text).parts):
                errors.append(f"{prefix}.path uses an ambiguous version name")
            if status != "planned" and not (root / Path(*PurePosixPath(path_text).parts)).is_file():
                errors.append(f"{prefix}.path does not exist: {path_text}")

        related_issues = entry["related_issues"]
        if (not isinstance(related_issues, list) or not related_issues
                or any(type(number) is not int or number <= 0
                       for number in related_issues)):
            errors.append(f"{prefix}.related_issues must contain positive issue numbers")
        for field in ("title", "owner", "source", "limitations"):
            if not isinstance(entry[field], str) or not entry[field].strip():
                errors.append(f"{prefix}.{field} must be a non-empty string")
        commit = entry["repo_commit"]
        if commit is not None and (not isinstance(commit, str) or not COMMIT_SHA.fullmatch(commit)):
            errors.append(f"{prefix}.repo_commit must be null or a 40-character SHA")
        if isinstance(status, str) and status in {"candidate", "approved"} and commit is None:
            errors.append(f"{prefix}.repo_commit is required for {status} evidence")

    missing_gates = sorted(GATES - covered_gates)
    if missing_gates:
        errors.append(f"index does not cover gates: {', '.join(missing_gates)}")
    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            continue
        source = entry.get("source")
        if isinstance(source, str) and source.startswith("EV-") and source not in seen_ids:
            errors.append(f"entries[{position}].source references unknown evidence: {source}")
    return errors


def _escape_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(data: dict[str, Any]) -> str:
    """Render a deterministic human-readable index from canonical JSON."""
    lines = [
        "# G0–G4 交付证据索引",
        "",
        "> 本文件由 `python -m tools.evidence_index --write` 从 `index.json` 生成，",
        "> 请勿手工编辑。状态定义和命名规则见 [README.md](README.md)。",
        "",
        f"- 项目：`{_escape_cell(data['project'])}`",
        f"- 索引日期：`{_escape_cell(data['updated_on'])}`",
        "- `planned` 仅表示计划路径，不能作为完成证据。",
        "",
        "| ID | 门禁 | 类型 | 证据 | 状态 | 仓库路径 | 来源/生成方式 | 关联 Issue | 责任人 | 使用限制 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for entry in sorted(data["entries"], key=lambda item: (item["gate"], item["id"])):
        path_text = entry["path"]
        if entry["status"] == "planned":
            path_cell = f"`{_escape_cell(path_text)}`"
        else:
            path_cell = f"[{_escape_cell(path_text)}](../../{path_text})"
        issues = "、".join(
            f"[#{number}](https://github.com/qiyuankaiwu/Challenge-Cup/issues/{number})"
            for number in entry["related_issues"]
        )
        lines.append(
            "| {id} | {gate} | {category} | {title} | {status} | {path} | {source} | "
            "{issues} | {owner} | {limitations} |".format(
                id=_escape_cell(entry["id"]),
                gate=_escape_cell(entry["gate"]),
                category=CATEGORY_LABELS[entry["category"]],
                title=_escape_cell(entry["title"]),
                status=STATUS_LABELS[entry["status"]],
                path=path_cell,
                source=_escape_cell(entry["source"]),
                issues=issues,
                owner=_escape_cell(entry["owner"]),
                limitations=_escape_cell(entry["limitations"]),
            )
        )
    lines.extend([
        "",
        "## 查证顺序",
        "",
        "1. 从本表按门禁或 Issue 找到证据 ID。",
        "2. 打开仓库路径；统计结果继续按 `source` 字段回到原始证据或生成命令。",
        "3. 只有状态为 `approved` 且绑定提交 SHA 的记录可进入最终交付清单。",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="validate JSON and generated Markdown")
    action.add_argument("--write", action="store_true", help="validate JSON and regenerate Markdown")
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--output", type=Path, default=DEFAULT_MARKDOWN)
    args = parser.parse_args()

    data = load_index(args.index)
    errors = validate_index(data, ROOT)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1

    expected = render_markdown(data)
    if args.write:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(expected, encoding="utf-8")
        print(f"证据索引已写入 {args.output.relative_to(ROOT)}")
        return 0

    if not args.output.is_file():
        print(f"ERROR: generated index is missing: {args.output.relative_to(ROOT)}")
        return 1
    if args.output.read_text(encoding="utf-8") != expected:
        print("ERROR: generated index is stale; run --write")
        return 1
    print(f"证据索引有效：{len(data['entries'])} 条，覆盖 G0-G4")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
