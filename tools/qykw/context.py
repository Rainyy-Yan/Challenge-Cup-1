"""Deterministic, bounded context planning for qykw pull-request reviews.

The module deliberately uses a character-upper-bound token estimate.  It is
less efficient than a provider tokenizer, but is deterministic, works for CJK
and source code, and cannot undercount the documented one-token-per-character
approximation used for admission control.
"""

from __future__ import annotations

import math
import re
from typing import Iterable

from tools.qykw.domain import (
    ChangedFile,
    ChangedLine,
    ContextChunk,
    ContextPlan,
    CoverageReport,
    DiffHunk,
    DiffSide,
    FileManifest,
    PullSnapshot,
    RepositoryFile,
)


class ContextError(ValueError):
    """Raised when a snapshot, unified diff, or context budget is invalid."""


_HUNK_HEADER = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@(?:.*)$"
)
_SENSITIVE_PARTS = (".env", "credential", "secret", "id_rsa", "private_key")
_OVERSIZED_CHARACTERS = 1_000_000


def estimate_tokens(text: str) -> int:
    """Return a conservative deterministic token bound for UTF-8 text.

    Admission is intentionally one token for every Unicode character.  This
    upper-bounds the approximation used here for ASCII, CJK and punctuation;
    callers must use this same function for both chunks and budgets.
    """
    if not isinstance(text, str):
        raise ContextError("text_must_be_string")
    return len(text)


def parse_hunks(file: ChangedFile) -> tuple[DiffHunk, ...]:
    """Parse a complete unified patch and retain only commentable changed lines."""
    if not isinstance(file, ChangedFile):
        raise ContextError("invalid_changed_file")
    if file.patch is None:
        return ()
    if not isinstance(file.patch, str):
        raise ContextError("patch_must_be_string")
    if not file.patch:
        return ()

    lines = file.patch.splitlines(keepends=True)
    result: list[DiffHunk] = []
    index = 0
    while index < len(lines):
        header = lines[index].rstrip("\r\n")
        matched = _HUNK_HEADER.fullmatch(header)
        if matched is None:
            raise ContextError("malformed_hunk_header")
        old_start, old_count = _hunk_range(matched, "old")
        new_start, new_count = _hunk_range(matched, "new")
        _validate_range(old_start, old_count)
        _validate_range(new_start, new_count)
        _validate_content_range(old_start, old_count, file.base_content)
        _validate_content_range(new_start, new_count, file.head_content)
        hunk_lines = [lines[index]]
        changed: list[ChangedLine] = []
        old_line = old_start
        new_line = new_start
        old_seen = 0
        new_seen = 0
        index += 1
        while index < len(lines) and not lines[index].startswith("@@"):
            item = lines[index]
            bare = item.rstrip("\r\n")
            if bare == r"\ No newline at end of file":
                if len(hunk_lines) == 1:
                    raise ContextError("orphan_no_newline_marker")
                hunk_lines.append(item)
                index += 1
                continue
            if not item:
                raise ContextError("malformed_hunk_line")
            prefix = item[0]
            if prefix == " ":
                old_line += 1
                new_line += 1
                old_seen += 1
                new_seen += 1
            elif prefix == "-":
                changed.append(ChangedLine(file.previous_path or file.path, old_line, DiffSide.LEFT))
                old_line += 1
                old_seen += 1
            elif prefix == "+":
                changed.append(ChangedLine(file.path, new_line, DiffSide.RIGHT))
                new_line += 1
                new_seen += 1
            else:
                raise ContextError("malformed_hunk_line")
            hunk_lines.append(item)
            index += 1
        if old_seen != old_count or new_seen != new_count:
            raise ContextError("hunk_count_mismatch")
        result.append(
            DiffHunk(
                path=file.path,
                previous_path=file.previous_path,
                header=header,
                changed_lines=tuple(changed),
                text="".join(hunk_lines),
            )
        )
    return tuple(result)


def build_context_plan(
    snapshot: PullSnapshot,
    *,
    repository_limit: int,
    backend_context_window: int,
    output_reserve: int,
    safety_reserve_ratio: float,
    max_chunk_ratio: float,
) -> ContextPlan:
    """Build a single-PR context plan with total and per-chunk hard limits."""
    _validate_snapshot(snapshot)
    effective_budget, max_chunk_tokens = _effective_budget(
        repository_limit,
        backend_context_window,
        output_reserve,
        safety_reserve_ratio,
        max_chunk_ratio,
    )
    files = tuple(snapshot.changed_files)
    paths = tuple(sorted(file.path for file in files))
    if len(paths) != len(set(paths)):
        raise ContextError("duplicate_changed_path")
    manifest = FileManifest(paths=paths, risk_order=tuple(sorted(paths, key=_risk_key)))
    by_path = {file.path: file for file in files}
    immutable_omissions = _snapshot_omissions(snapshot)
    omissions: list[str] = list(immutable_omissions)
    handled: set[str] = set()
    hunks_by_path: dict[str, tuple[DiffHunk, ...]] = {}
    commentable: set[ChangedLine] = set()
    candidates: dict[str, tuple[str, ...]] = {}

    for path in manifest.risk_order:
        file = by_path[path]
        reason = _file_skip_reason(file, immutable_omissions)
        if reason is not None:
            omissions.append(f"{reason}:{path}")
            handled.add(path)
            continue
        try:
            hunks = parse_hunks(file)
        except ContextError as error:
            omissions.append(f"malformed_patch:{path}:{error}")
            handled.add(path)
            continue
        if file.patch is None:
            omissions.append(f"patch_missing:{path}")
            handled.add(path)
            continue
        if not hunks and _is_mode_only(file):
            omissions.append(f"mode_only:{path}")
            handled.add(path)
            continue
        hunks_by_path[path] = hunks
        commentable.update(line for hunk in hunks for line in hunk.changed_lines)
        candidates[path] = _file_context_units(file, hunks)

    chunks: list[ContextChunk] = []
    used = 0
    chunk_index = 1
    reviewed: set[str] = set()
    reviewed_hunks: set[tuple[str, int]] = set()
    for path in manifest.risk_order:
        units = candidates.get(path)
        if units is None:
            continue
        any_allocated = False
        for unit_index, unit in enumerate(units):
            allocated, chunk_index, used = _allocate_unit(
                unit,
                path=path,
                snapshot=snapshot,
                chunk_index=chunk_index,
                used=used,
                effective_budget=effective_budget,
                max_chunk_tokens=max_chunk_tokens,
                chunks=chunks,
            )
            if allocated:
                any_allocated = True
                if unit_index < len(hunks_by_path[path]):
                    reviewed_hunks.add((path, unit_index))
            if used >= effective_budget:
                break
        if any_allocated:
            reviewed.add(path)
            handled.add(path)
        else:
            omissions.append(f"budget_exhausted:{path}")
            handled.add(path)

    # Rules and related files are trusted/read-only context, never PR manifest
    # entries.  They are appended only after every PR file has been triaged.
    for reference in _ordered_references(snapshot.trusted_rules, snapshot.related_files):
        unit = _reference_context_unit(reference)
        _, chunk_index, used = _allocate_unit(
            unit,
            path=reference.path,
            snapshot=snapshot,
            chunk_index=chunk_index,
            used=used,
            effective_budget=effective_budget,
            max_chunk_tokens=max_chunk_tokens,
            chunks=chunks,
        )

    total_hunks = sum(len(value) for value in hunks_by_path.values())
    coverage = CoverageReport(
        total_files=len(paths),
        reviewed_files=len(reviewed),
        total_hunks=total_hunks,
        reviewed_hunks=len(reviewed_hunks),
        omissions=tuple(_deduplicate(omissions)),
        explains_every_file=all(path in handled for path in paths),
    )
    return ContextPlan(
        repository=snapshot.target_repository,
        pr_number=snapshot.number,
        source_head_sha=snapshot.source_head_sha,
        run_id=_run_id(snapshot),
        manifest=manifest,
        chunks=tuple(chunks),
        coverage=coverage,
        commentable_lines=frozenset(commentable),
        max_chunk_tokens=max_chunk_tokens,
    )


def _hunk_range(match: re.Match[str], side: str) -> tuple[int, int]:
    start = int(match.group(f"{side}_start"))
    count_text = match.group(f"{side}_count")
    return start, int(count_text) if count_text is not None else 1


def _validate_range(start: int, count: int) -> None:
    if count < 0 or start < 0 or (start == 0 and count != 0):
        raise ContextError("invalid_hunk_range")


def _validate_content_range(start: int, count: int, content: str | None) -> None:
    """Reject a patch coordinate which cannot name a line in known content."""
    if content is None:
        return
    line_count = len(content.splitlines())
    if count == 0:
        if start > line_count + 1:
            raise ContextError("hunk_line_out_of_range")
        return
    if start + count - 1 > line_count:
        raise ContextError("hunk_line_out_of_range")


def _effective_budget(
    repository_limit: int,
    backend_context_window: int,
    output_reserve: int,
    safety_reserve_ratio: float,
    max_chunk_ratio: float,
) -> tuple[int, int]:
    if any(not isinstance(value, int) or isinstance(value, bool) for value in (repository_limit, backend_context_window, output_reserve)):
        raise ContextError("budget_must_be_integer")
    if repository_limit <= 0 or backend_context_window <= 0 or output_reserve < 0:
        raise ContextError("invalid_budget")
    if not isinstance(safety_reserve_ratio, (int, float)) or not 0 <= safety_reserve_ratio < 1:
        raise ContextError("invalid_safety_reserve_ratio")
    if not isinstance(max_chunk_ratio, (int, float)) or not 0 < max_chunk_ratio <= 1:
        raise ContextError("invalid_max_chunk_ratio")
    safety_reserve = math.ceil(backend_context_window * safety_reserve_ratio)
    available = backend_context_window - output_reserve - safety_reserve
    effective = min(repository_limit, available)
    if effective <= 0:
        raise ContextError("impossible_budget")
    maximum = math.floor(effective * max_chunk_ratio)
    if maximum <= 0:
        raise ContextError("impossible_chunk_budget")
    return effective, maximum


def _validate_snapshot(snapshot: PullSnapshot) -> None:
    if not isinstance(snapshot, PullSnapshot):
        raise ContextError("invalid_snapshot")
    if snapshot.number <= 0 or not snapshot.target_repository or not snapshot.source_head_sha:
        raise ContextError("invalid_snapshot_identity")
    for file in snapshot.changed_files:
        if not isinstance(file, ChangedFile) or not file.path or file.path.startswith(("/", "\\")) or ".." in file.path.split("/"):
            raise ContextError("unsafe_changed_path")


def _snapshot_omissions(snapshot: PullSnapshot) -> tuple[str, ...]:
    value = getattr(snapshot, "omissions", ())
    if not isinstance(value, tuple) or not all(isinstance(item, str) for item in value):
        raise ContextError("invalid_snapshot_omissions")
    return value


def _file_skip_reason(file: ChangedFile, omissions: tuple[str, ...]) -> str | None:
    lowered = file.path.lower()
    if file.binary:
        return "binary"
    if file.generated:
        return "generated"
    if any(part in lowered for part in _SENSITIVE_PARTS):
        return "sensitive"
    if any(item.endswith(f":{file.path}") and "missing" in item for item in omissions):
        return "unreadable"
    required_base = file.status not in {"added"}
    required_head = file.status not in {"removed"}
    if (required_base and file.base_content is None) or (required_head and file.head_content is None):
        return "unreadable"
    contents = (file.base_content or "") + (file.head_content or "") + (file.patch or "")
    if len(contents) > _OVERSIZED_CHARACTERS:
        return "oversized"
    return None


def _is_mode_only(file: ChangedFile) -> bool:
    return not file.patch and file.base_mode != file.head_mode


def _file_context_units(file: ChangedFile, hunks: tuple[DiffHunk, ...]) -> tuple[str, ...]:
    units = [_diff_unit(file, hunk) for hunk in hunks]
    if not units:
        units.append(_content_unit(file, "base", DiffSide.LEFT, file.previous_path or file.path, file.base_mode, file.base_content))
        units.append(_content_unit(file, "head", DiffSide.RIGHT, file.path, file.head_mode, file.head_content))
    else:
        # A compact, provenance-preserving state record complements the patch.
        units.append(
            f"FILE path={file.path} previous_path={file.previous_path or '-'} status={file.status} "
            f"base_mode={file.base_mode or '-'} head_mode={file.head_mode or '-'}\n"
        )
    return tuple(unit for unit in units if unit)


def _diff_unit(file: ChangedFile, hunk: DiffHunk) -> str:
    return (
        f"DIFF path={file.path} previous_path={file.previous_path or '-'} "
        f"base_ref={file.base_sha or '-'} head_ref={file.head_sha or '-'}\n{hunk.text}"
    )


def _content_unit(
    file: ChangedFile,
    ref_name: str,
    side: DiffSide,
    path: str,
    mode: str | None,
    content: str | None,
) -> str:
    if content is None:
        return ""
    return f"CONTENT path={path} ref={ref_name} side={side.value} line=1 mode={mode or '-'}\n{content}"


def _ordered_references(
    trusted_rules: tuple[RepositoryFile, ...], related_files: tuple[RepositoryFile, ...]
) -> Iterable[RepositoryFile]:
    return tuple(sorted(trusted_rules + related_files, key=lambda item: (item.purpose, item.path, item.ref)))


def _reference_context_unit(file: RepositoryFile) -> str:
    return f"REFERENCE path={file.path} ref={file.ref} purpose={file.purpose} line=1\n{file.content}"


def _allocate_unit(
    unit: str,
    *,
    path: str,
    snapshot: PullSnapshot,
    chunk_index: int,
    used: int,
    effective_budget: int,
    max_chunk_tokens: int,
    chunks: list[ContextChunk],
) -> tuple[bool, int, int]:
    """Allocate a unit without exceeding either budget, even for one huge line."""
    remaining = min(max_chunk_tokens, effective_budget - used)
    if remaining <= 0 or not unit:
        return False, chunk_index, used
    offset = 0
    allocated = False
    while offset < len(unit) and used < effective_budget:
        allowance = min(max_chunk_tokens, effective_budget - used)
        prefix = _fragment_prefix(snapshot, path, unit, offset)
        payload_capacity = allowance - estimate_tokens(prefix)
        if payload_capacity <= 0:
            break
        piece = prefix + unit[offset : offset + payload_capacity]
        if not piece:
            break
        tokens = estimate_tokens(piece)
        chunks.append(
            ContextChunk(
                chunk_id=f"{snapshot.target_repository}#{snapshot.number}:{snapshot.source_head_sha}:{chunk_index}",
                paths=(path,),
                text=piece,
                estimated_tokens=tokens,
            )
        )
        allocated = True
        chunk_index += 1
        used += tokens
        offset += len(piece) - len(prefix)
    return allocated, chunk_index, used


def _fragment_prefix(snapshot: PullSnapshot, path: str, unit: str, offset: int) -> str:
    """Attach self-contained provenance to every fragment, not just its first."""
    header = next(
        (_HUNK_HEADER.fullmatch(line) for line in unit.splitlines() if line.startswith("@@")),
        None,
    )
    if "\n+" in unit and "\n-" not in unit:
        side = DiffSide.RIGHT.value
        line = int(header.group("new_start")) if header is not None else 1
    elif "\n-" in unit and "\n+" not in unit:
        side = DiffSide.LEFT.value
        line = int(header.group("old_start")) if header is not None else 1
    elif "\n+" in unit or "\n-" in unit:
        side = "LEFT,RIGHT"
        line = int(header.group("new_start")) if header is not None else 1
    else:
        side = "CONTEXT"
        line = 1
    # Offset records the exact fragment boundary.  The parser, rather than this
    # context label, is the authority for publishable comment coordinates.
    return (
        f"PROVENANCE repository={snapshot.target_repository} pr={snapshot.number} "
        f"head={snapshot.source_head_sha} path={path} side={side} line={line} offset={offset}\n"
    )


def _risk_key(path: str) -> tuple[int, str]:
    lowered = path.lower()
    score = sum(
        weight
        for marker, weight in (
            ("auth", 100), ("permission", 95), ("security", 90), ("policy", 80),
            ("config", 70), ("migration", 60), ("payment", 60), ("test", 20),
        )
        if marker in lowered
    )
    return (-score, path)


def _run_id(snapshot: PullSnapshot) -> str:
    return f"{snapshot.target_repository}:{snapshot.number}:{snapshot.source_head_sha}"


def _deduplicate(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))
