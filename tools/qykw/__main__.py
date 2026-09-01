"""Narrow, controller-owned entry point for qykw workflow phases.

This module intentionally does not construct a gateway or inference provider.
The Actions controller supplies the phase-specific service object, so a job
cannot accidentally combine review-token and inference credentials.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
from typing import Sequence


_PHASES = frozenset({"control", "authorize", "analyze", "publish", "record-failure"})
_MAX_ARTIFACT_BYTES = 64 * 1024
_RUN_KEYS = frozenset({"run_id", "idempotency_key", "repository_id", "repository", "pr_number", "head_sha", "base_sha", "base_ref"})
_ARTIFACT_KEYS = frozenset({"version", "kind", "phase", "run"})
_FAILURE_ARTIFACT_KEYS = _ARTIFACT_KEYS | {"failure"}


def main(argv: Sequence[str] | None = None, *, controller: object | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.qykw", allow_abbrev=False)
    parser.add_argument("--phase", required=True, choices=sorted(_PHASES))
    parser.add_argument("--artifact")
    parser.add_argument("--output")
    parser.add_argument("--error-code")
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code)
    if args.phase != "record-failure" and (not args.artifact or not args.output):
        return _error("artifact_and_output_required")
    if args.phase == "record-failure" and (not args.artifact or not args.output or not args.error_code):
        return _error("failure_artifact_output_and_code_required")
    try:
        artifact = _read_artifact(Path(args.artifact))
        if artifact["phase"] != args.phase and not (args.phase == "record-failure" and artifact["phase"] in _PHASES):
            raise ValueError("artifact_phase_mismatch")
        result = _run_phase(args.phase, artifact, controller, args.error_code)
        _write_artifact(Path(args.output), result)
    except (OSError, ValueError, TypeError) as error:
        return _error(_safe_code(error))
    return 0


def _run_phase(phase: str, artifact: dict[str, object], controller: object | None,
               error_code: str | None) -> dict[str, object]:
    if phase == "record-failure":
        if not isinstance(error_code, str) or not error_code.isidentifier() or len(error_code) > 80:
            raise ValueError("invalid_error_code")
        return {**artifact, "phase": phase, "failure": {"code": error_code}}
    if controller is None:
        raise ValueError("phase_controller_required")
    method = getattr(controller, phase, None)
    if not callable(method):
        raise ValueError("phase_not_available")
    result = method(artifact)
    if result is None:
        raise ValueError("empty_phase_result")
    if not isinstance(result, dict):
        raise ValueError("invalid_phase_result")
    _validate_artifact(result, expected_phase=phase)
    return result


def _read_artifact(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("unsafe_artifact_path")
    if path.stat().st_size > _MAX_ARTIFACT_BYTES:
        raise ValueError("artifact_too_large")
    raw = path.read_bytes()
    if len(raw) > _MAX_ARTIFACT_BYTES:
        raise ValueError("artifact_too_large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid_artifact_json") from error
    _validate_artifact(payload)
    return payload


def _validate_artifact(payload: object, *, expected_phase: str | None = None) -> None:
    if not isinstance(payload, dict):
        raise ValueError("invalid_artifact_schema")
    phase = payload.get("phase")
    expected_keys = _FAILURE_ARTIFACT_KEYS if phase == "record-failure" else _ARTIFACT_KEYS
    if set(payload) != expected_keys:
        raise ValueError("invalid_artifact_schema")
    if payload.get("version") != 1 or payload.get("kind") != "qykw-run":
        raise ValueError("unsupported_artifact_version")
    if phase not in _PHASES or (expected_phase is not None and phase != expected_phase):
        raise ValueError("artifact_phase_mismatch")
    run = payload.get("run")
    if not isinstance(run, dict) or set(run) != _RUN_KEYS:
        raise ValueError("invalid_run_binding")
    strings = ("run_id", "idempotency_key", "repository", "head_sha", "base_sha", "base_ref")
    if any(not isinstance(run.get(key), str) or not run[key] or len(run[key]) > 512 for key in strings):
        raise ValueError("invalid_run_binding")
    if any(type(run.get(key)) is not int or run[key] <= 0 for key in ("repository_id", "pr_number")):
        raise ValueError("invalid_run_binding")
    if phase == "record-failure":
        failure = payload.get("failure")
        if not isinstance(failure, dict) or set(failure) != {"code"}:
            raise ValueError("invalid_failure_artifact")
        code = failure.get("code")
        if not isinstance(code, str) or not code.isidentifier() or len(code) > 80:
            raise ValueError("invalid_failure_artifact")


def _write_artifact(path: Path, payload: dict[str, object]) -> None:
    _validate_artifact(payload)
    if path.exists() and path.is_symlink():
        raise ValueError("unsafe_output_path")
    if not path.parent.exists() or path.parent.is_symlink():
        raise ValueError("unsafe_output_path")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAX_ARTIFACT_BYTES:
        raise ValueError("artifact_too_large")
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=".qykw-artifact-", delete=False) as temporary:
        temporary.write(encoded)
        temporary_path = Path(temporary.name)
    try:
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _error(code: str) -> int:
    print(f"::error::{code}", file=sys.stderr)
    return 2


def _safe_code(error: BaseException) -> str:
    code = str(error)
    return code if code and code.replace("_", "").isalnum() and len(code) <= 80 else "phase_failed"


if __name__ == "__main__":
    raise SystemExit(main())
