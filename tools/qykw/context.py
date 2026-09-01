"""Deterministic, bounded context planning for qykw pull-request reviews.

The module deliberately uses a character-upper-bound token estimate.  It is
less efficient than a provider tokenizer, but is deterministic, works for CJK
and source code, and cannot undercount the documented one-token-per-character
approximation used for admission control.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
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
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SENSITIVE_PARTS = (".env", "credential", "secret", "id_rsa", "private_key")
_OVERSIZED_CHARACTERS = 1_000_000
_MINIMUM_TRIAGE_TEXT = "TRIAGE\n"


@dataclass(frozen=True)
class _ContextRecord:
    """One provenance-stable context record, split only within its own line."""

    path: str
    previous_path: str | None
    base_ref: str | None
    head_ref: str | None
    side: str
    old_start: int | None
    old_end: int | None
    new_start: int | None
    new_end: int | None
    text: str
    hunk_index: int | None = None
    record_index: int = 0


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
    _validate_changed_file_paths(file)
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
    run_id: str,
    repository_id: int,
    repository_limit: int,
    backend_context_window: int,
    output_reserve: int,
    safety_reserve_ratio: float,
    max_chunk_ratio: float,
) -> ContextPlan:
    """Build a single-PR context plan with total and per-chunk hard limits."""
    _validate_snapshot(snapshot)
    _validate_run_identity(run_id, repository_id)
    plan_identity = _plan_identity(snapshot, run_id, repository_id)
    provenance_identity = _provenance_identity(snapshot, run_id, repository_id)
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
    candidates: dict[str, tuple[_ContextRecord, ...]] = {}

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
        candidates[path] = _file_context_records(file, hunks)

    chunks: list[ContextChunk] = []
    used = 0
    chunk_index = 1
    reviewed: set[str] = set()
    reviewed_hunks: set[tuple[str, int]] = set()

    # First pass is an all-or-nothing minimum allocation.  A low-risk file
    # cannot be starved merely because a prior high-risk file has a huge diff.
    triage_records = {
        path: _triage_record(by_path[path]) for path in manifest.risk_order if path in candidates
    }
    triage_costs = {
        path: _record_cost(provenance_identity, record) for path, record in triage_records.items()
    }
    if any(cost > max_chunk_tokens for cost in triage_costs.values()):
        raise ContextError("impossible_triage_chunk_budget")
    if sum(triage_costs.values()) > effective_budget:
        raise ContextError("impossible_triage_budget")
    for path in manifest.risk_order:
        record = triage_records.get(path)
        if record is None:
            continue
        allocated, completed, chunk_index, used = _allocate_record(
            record,
            plan_identity=plan_identity,
            provenance_identity=provenance_identity,
            chunk_index=chunk_index,
            used=used,
            effective_budget=effective_budget,
            max_chunk_tokens=max_chunk_tokens,
            chunks=chunks,
        )
        if not allocated or not completed:
            raise ContextError("impossible_triage_budget")
        reviewed.add(path)
        handled.add(path)
        omissions.append(f"minimum_triage:{path}")

    # Only the residual budget is prioritized by risk.  Every eligible path is
    # already explicitly triaged at this point.
    complete_records: set[tuple[str, int, int]] = set()
    partial_records: dict[tuple[str, int], set[int]] = {}
    unallocated_records: dict[tuple[str, int], set[int]] = {}
    expected_records: dict[tuple[str, int], set[int]] = {}
    for path, records in candidates.items():
        for record in records:
            if record.hunk_index is not None:
                expected_records.setdefault((path, record.hunk_index), set()).add(record.record_index)
    for path in manifest.risk_order:
        records = candidates.get(path)
        if records is None:
            continue
        for record in records:
            allocated, completed, chunk_index, used = _allocate_record(
                record,
                plan_identity=plan_identity,
                provenance_identity=provenance_identity,
                chunk_index=chunk_index,
                used=used,
                effective_budget=effective_budget,
                max_chunk_tokens=max_chunk_tokens,
                chunks=chunks,
            )
            if record.hunk_index is not None:
                key = (path, record.hunk_index, record.record_index)
                if completed:
                    complete_records.add(key)
                elif allocated:
                    partial_records.setdefault((path, record.hunk_index), set()).add(record.record_index)
                else:
                    unallocated_records.setdefault((path, record.hunk_index), set()).add(record.record_index)

    omissions.extend(
        _truncation_summaries(
            manifest.risk_order, expected_records, partial_records, unallocated_records
        )
    )

    # Rules and related files are trusted/read-only context, never PR manifest
    # entries.  They are appended only after every PR file has been triaged.
    for reference in _ordered_references(snapshot.trusted_rules, snapshot.related_files):
        _, _, chunk_index, used = _allocate_record(
            _reference_context_record(reference),
            plan_identity=plan_identity,
            provenance_identity=provenance_identity,
            chunk_index=chunk_index,
            used=used,
            effective_budget=effective_budget,
            max_chunk_tokens=max_chunk_tokens,
            chunks=chunks,
        )

    total_hunks = sum(len(value) for value in hunks_by_path.values())
    reviewed_hunks = {
        hunk_key
        for hunk_key, record_indexes in expected_records.items()
        if all((hunk_key[0], hunk_key[1], record_index) in complete_records for record_index in record_indexes)
    }
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
        run_id=plan_identity,
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
        if not isinstance(file, ChangedFile):
            raise ContextError("unsafe_changed_path")
        _validate_changed_file_paths(file)


def _validate_changed_file_paths(file: ChangedFile) -> None:
    _validate_relative_path(file.path)
    if file.previous_path is not None:
        _validate_relative_path(file.previous_path)


def _validate_relative_path(path: str) -> None:
    if not isinstance(path, str) or not path or path.startswith(("/", "\\")):
        raise ContextError("unsafe_changed_path")
    if "\\" in path or any(ord(character) < 32 or ord(character) == 127 for character in path):
        raise ContextError("unsafe_changed_path")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts) or ":" in parts[0]:
        raise ContextError("unsafe_changed_path")


def _validate_run_identity(run_id: str, repository_id: int) -> None:
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        raise ContextError("invalid_run_id")
    if not isinstance(repository_id, int) or isinstance(repository_id, bool) or repository_id <= 0:
        raise ContextError("invalid_repository_id")


def _plan_identity(snapshot: PullSnapshot, run_id: str, repository_id: int) -> str:
    return (
        f"run_id={run_id}|repository_id={repository_id}|repository={snapshot.target_repository}"
        f"|pr={snapshot.number}|base_sha={snapshot.target_base_sha}"
        f"|base_ref={snapshot.target_base_ref}|head_sha={snapshot.source_head_sha}"
    )


def _provenance_identity(snapshot: PullSnapshot, run_id: str, repository_id: int) -> str:
    return (
        f"run={run_id} rid={repository_id} repo={snapshot.target_repository} pr={snapshot.number} "
        f"bs={snapshot.target_base_sha} br={snapshot.target_base_ref} hs={snapshot.source_head_sha}"
    )


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


def _file_context_records(
    file: ChangedFile, hunks: tuple[DiffHunk, ...]
) -> tuple[_ContextRecord, ...]:
    if hunks:
        return tuple(
            record
            for hunk_index, hunk in enumerate(hunks)
            for record in _diff_records(file, hunk, hunk_index)
        )
    return _content_records(file)


def _diff_records(
    file: ChangedFile, hunk: DiffHunk, hunk_index: int
) -> tuple[_ContextRecord, ...]:
    """Represent every diff line separately so later fragments retain coordinates."""
    header = _HUNK_HEADER.fullmatch(hunk.header)
    if header is None:
        raise ContextError("malformed_hunk_header")
    old_line, old_count = _hunk_range(header, "old")
    new_line, new_count = _hunk_range(header, "new")
    records = [
        _record(
            file, "CONTEXT", old_line if old_count else None,
            old_line + old_count - 1 if old_count else None,
            new_line if new_count else None,
            new_line + new_count - 1 if new_count else None,
            hunk.header + "\n", hunk_index,
        )
    ]
    for item in hunk.text.splitlines(keepends=True)[1:]:
        if item.rstrip("\r\n") == r"\ No newline at end of file":
            records[-1] = replace(records[-1], text=records[-1].text + item)
            continue
        prefix = item[0]
        if prefix == " ":
            records.append(_record(file, "CONTEXT", old_line, old_line, new_line, new_line, item, hunk_index))
            old_line += 1
            new_line += 1
        elif prefix == "-":
            records.append(_record(file, DiffSide.LEFT.value, old_line, old_line, None, None, item, hunk_index))
            old_line += 1
        elif prefix == "+":
            records.append(_record(file, DiffSide.RIGHT.value, None, None, new_line, new_line, item, hunk_index))
            new_line += 1
        else:
            raise ContextError("malformed_hunk_line")
    return tuple(replace(record, record_index=index) for index, record in enumerate(records))


def _content_records(file: ChangedFile) -> tuple[_ContextRecord, ...]:
    records: list[_ContextRecord] = []
    for side, path, content in (
        (DiffSide.LEFT.value, file.previous_path or file.path, file.base_content),
        (DiffSide.RIGHT.value, file.path, file.head_content),
    ):
        if content is None:
            continue
        for line_number, text in enumerate(content.splitlines(keepends=True), start=1):
            records.append(
                _record(
                    file, side,
                    line_number if side == DiffSide.LEFT.value else None,
                    line_number if side == DiffSide.LEFT.value else None,
                    line_number if side == DiffSide.RIGHT.value else None,
                    line_number if side == DiffSide.RIGHT.value else None,
                    text, None, path=path,
                )
            )
    return tuple(records)


def _record(
    file: ChangedFile,
    side: str,
    old_start: int | None,
    old_end: int | None,
    new_start: int | None,
    new_end: int | None,
    text: str,
    hunk_index: int | None,
    *,
    path: str | None = None,
) -> _ContextRecord:
    return _ContextRecord(
        path=path or (file.previous_path or file.path if side == DiffSide.LEFT.value else file.path),
        previous_path=file.previous_path,
        base_ref=file.base_sha,
        head_ref=file.head_sha,
        side=side,
        old_start=old_start,
        old_end=old_end,
        new_start=new_start,
        new_end=new_end,
        text=text,
        hunk_index=hunk_index,
    )


def _triage_record(file: ChangedFile) -> _ContextRecord:
    return _record(file, "TRIAGE", None, None, None, None, _MINIMUM_TRIAGE_TEXT, None, path=file.path)


def _ordered_references(
    trusted_rules: tuple[RepositoryFile, ...], related_files: tuple[RepositoryFile, ...]
) -> Iterable[RepositoryFile]:
    return tuple(sorted(trusted_rules + related_files, key=lambda item: (item.purpose, item.path, item.ref)))


def _reference_context_record(file: RepositoryFile) -> _ContextRecord:
    return _ContextRecord(
        path=file.path,
        previous_path=None,
        base_ref=file.ref,
        head_ref=file.ref,
        side="REFERENCE",
        old_start=None,
        old_end=None,
        new_start=1,
        new_end=1,
        text=f"REFERENCE purpose={file.purpose}\n{file.content}",
    )


def _record_cost(provenance_identity: str, record: _ContextRecord) -> int:
    return estimate_tokens(_record_prefix(provenance_identity, record) + record.text)


def _allocate_record(
    record: _ContextRecord,
    *,
    plan_identity: str,
    provenance_identity: str,
    chunk_index: int,
    used: int,
    effective_budget: int,
    max_chunk_tokens: int,
    chunks: list[ContextChunk],
) -> tuple[bool, bool, int, int]:
    """Allocate a record without exceeding either budget, even for one huge line."""
    remaining = min(max_chunk_tokens, effective_budget - used)
    if remaining <= 0 or not record.text:
        return False, False, chunk_index, used
    offset = 0
    allocated = False
    while offset < len(record.text) and used < effective_budget:
        allowance = min(max_chunk_tokens, effective_budget - used)
        prefix = _record_prefix(provenance_identity, record)
        payload_capacity = allowance - estimate_tokens(prefix)
        if payload_capacity <= 0:
            break
        piece = prefix + record.text[offset : offset + payload_capacity]
        if not piece:
            break
        tokens = estimate_tokens(piece)
        chunks.append(
            ContextChunk(
                chunk_id=f"{plan_identity}|chunk={chunk_index}",
                paths=(record.path,),
                text=piece,
                estimated_tokens=tokens,
            )
        )
        allocated = True
        chunk_index += 1
        used += tokens
        offset += len(piece) - len(prefix)
    return allocated, offset == len(record.text), chunk_index, used


def _record_prefix(provenance_identity: str, record: _ContextRecord) -> str:
    """Emit complete immutable diff provenance on every physical fragment."""
    return (
        f"P {provenance_identity} path={record.path} prev={record.previous_path or '-'} "
        f"side={record.side} old={_line_range(record.old_start, record.old_end)} "
        f"new={_line_range(record.new_start, record.new_end)}\n"
    )


def _line_range(start: int | None, end: int | None) -> str:
    return "-" if start is None or end is None else f"{start}-{end}"


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


def _truncation_summaries(
    risk_order: tuple[str, ...],
    expected_records: dict[tuple[str, int], set[int]],
    partial_records: dict[tuple[str, int], set[int]],
    unallocated_records: dict[tuple[str, int], set[int]],
) -> tuple[str, ...]:
    """Summarize incomplete records once per hunk without line-count growth."""
    summaries: list[str] = []
    for path in risk_order:
        hunk_indexes = sorted({
            hunk_index
            for candidate_path, hunk_index in partial_records.keys() | unallocated_records.keys()
            if candidate_path == path
        })
        position = 0
        while position < len(hunk_indexes):
            hunk_index = hunk_indexes[position]
            partial = partial_records.get((path, hunk_index), set())
            unallocated = unallocated_records.get((path, hunk_index), set())
            ranges = _contiguous_ranges(unallocated)
            expected = expected_records.get((path, hunk_index), set())
            if not partial and unallocated == expected and len(ranges) == 1:
                start_hunk = end_hunk = hunk_index
                record_range = ranges[0]
                record_count = len(unallocated)
                position += 1
                while position < len(hunk_indexes):
                    next_hunk = hunk_indexes[position]
                    next_unallocated = unallocated_records.get((path, next_hunk), set())
                    next_ranges = _contiguous_ranges(next_unallocated)
                    if (
                        next_hunk != end_hunk + 1
                        or partial_records.get((path, next_hunk), set())
                        or next_unallocated != expected_records.get((path, next_hunk), set())
                        or next_ranges != (record_range,)
                        or len(next_unallocated) != record_count
                    ):
                        break
                    end_hunk = next_hunk
                    position += 1
                if end_hunk > start_hunk:
                    summaries.append(
                        f"budget_truncated_unallocated_hunks:{path}:hunks={start_hunk}-{end_hunk}:"
                        f"records={record_range[0]}-{record_range[1]}:records_per_hunk={record_count}"
                    )
                else:
                    summaries.append(
                        f"budget_truncated_unallocated:{path}:hunk={hunk_index}:"
                        f"records={record_range[0]}-{record_range[1]}"
                    )
                continue
            if partial:
                # Allocation consumes the shared budget monotonically, so this
                # set can contain only the final partially emitted record.
                summaries.append(
                    f"budget_truncated_partial:{path}:hunk={hunk_index}:record={min(partial)}"
                )
            for start, end in ranges:
                summaries.append(
                    f"budget_truncated_unallocated:{path}:hunk={hunk_index}:records={start}-{end}"
                )
            position += 1
    return tuple(summaries)


def _contiguous_ranges(record_indexes: set[int]) -> tuple[tuple[int, int], ...]:
    if not record_indexes:
        return ()
    ordered = sorted(record_indexes)
    ranges: list[tuple[int, int]] = []
    start = end = ordered[0]
    for value in ordered[1:]:
        if value == end + 1:
            end = value
            continue
        ranges.append((start, end))
        start = end = value
    ranges.append((start, end))
    return tuple(ranges)


def _deduplicate(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))
