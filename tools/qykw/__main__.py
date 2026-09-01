"""Strict artifact boundary for isolated qykw workflow phases."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
from typing import Sequence


_CLI_PHASES = frozenset({"control", "authorize", "analyze", "publish", "record-failure"})
_ARTIFACT_PHASES = _CLI_PHASES | {"request"}
_PREDECESSORS = {
    "control": frozenset({"control"}),
    "authorize": frozenset({"request"}),
    "analyze": frozenset({"authorize"}),
    "publish": frozenset({"analyze"}),
    "record-failure": frozenset({"authorize", "analyze", "publish"}),
}
_MAX_ARTIFACT_BYTES = 64 * 1024
_ARTIFACT_KEYS = frozenset({"version", "phase", "run", "payload"})
_RUN_KEYS = frozenset({
    "run_id", "idempotency_key", "repository_id", "repository", "pr_number",
    "event_name", "event_action", "source_repository", "source_head_sha",
    "target_base_sha", "target_base_ref", "actor_login", "command",
})
_COMMAND_KEYS = frozenset({"name", "argument", "mode"})


def main(argv: Sequence[str] | None = None, *, controller: object | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.qykw", allow_abbrev=False)
    parser.add_argument("--phase", required=True, choices=sorted(_CLI_PHASES))
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--error-code")
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code)
    try:
        artifact = _read_artifact(Path(args.artifact))
        result = _run_phase(args.phase, artifact, controller, args.error_code)
        _write_artifact(Path(args.output), result)
    except (OSError, ValueError, TypeError) as error:
        return _error(_safe_code(error))
    return 0


def _run_phase(phase: str, artifact: dict[str, object], controller: object | None,
               error_code: str | None) -> dict[str, object]:
    if artifact["phase"] not in _PREDECESSORS[phase]:
        raise ValueError("artifact_phase_mismatch")
    if phase == "control" and artifact["run"]["command"]["name"] != "停止":  # type: ignore[index]
        raise ValueError("control_command_not_stop")
    if phase == "record-failure":
        if not _error_code(error_code):
            raise ValueError("invalid_error_code")
        return _artifact("record-failure", artifact["run"], {"error_code": error_code})
    if controller is None:
        raise ValueError("phase_controller_required")
    method = getattr(controller, phase, None)
    if not callable(method):
        raise ValueError("phase_not_available")
    result = method(artifact)
    if not isinstance(result, dict):
        raise ValueError("invalid_phase_result")
    _validate_artifact(result, expected_phase=phase)
    if result["run"] != artifact["run"]:
        raise ValueError("immutable_run_binding_changed")
    return result


def _artifact(phase: str, run: object, payload: object) -> dict[str, object]:
    result = {"version": 1, "phase": phase, "run": run, "payload": payload}
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
    if not isinstance(payload, dict) or set(payload) != _ARTIFACT_KEYS:
        raise ValueError("invalid_artifact_schema")
    phase = payload.get("phase")
    if phase not in _ARTIFACT_PHASES or (expected_phase is not None and phase != expected_phase):
        raise ValueError("artifact_phase_mismatch")
    if payload.get("version") != 1:
        raise ValueError("unsupported_artifact_version")
    _validate_run(payload.get("run"))
    _validate_payload(phase, payload.get("payload"))


def _validate_run(run: object) -> None:
    if not isinstance(run, dict) or set(run) != _RUN_KEYS:
        raise ValueError("invalid_run_binding")
    strings = (
        "run_id", "idempotency_key", "repository", "event_name", "event_action",
        "source_repository", "source_head_sha", "target_base_sha", "target_base_ref", "actor_login",
    )
    if any(not _text(run.get(key), 512) for key in strings):
        raise ValueError("invalid_run_binding")
    if any(type(run.get(key)) is not int or run[key] <= 0 for key in ("repository_id", "pr_number")):
        raise ValueError("invalid_run_binding")
    command = run.get("command")
    if not isinstance(command, dict) or set(command) != _COMMAND_KEYS:
        raise ValueError("invalid_run_binding")
    if not _text(command.get("name"), 64) or not _text(command.get("argument"), 4096, allow_empty=True) or not _text(command.get("mode"), 64):
        raise ValueError("invalid_run_binding")


def _validate_payload(phase: object, payload: object) -> None:
    if not isinstance(payload, dict):
        raise ValueError("invalid_phase_payload")
    if phase == "request" and set(payload) == {"command"} and _text(payload.get("command"), 64):
        return
    if phase == "control" and set(payload) == {"stop_comment_id"} and type(payload.get("stop_comment_id")) is int and payload["stop_comment_id"] > 0:
        return
    if phase == "authorize" and payload == {"authorization": "accepted"}:
        return
    if phase == "analyze":
        analysis = payload.get("analysis")
        if set(payload) == {"analysis"} and isinstance(analysis, dict) and set(analysis) == {"result_ref"} and _text(analysis.get("result_ref"), 512):
            return
    if phase == "publish" and payload == {"published": True}:
        return
    if phase == "record-failure" and set(payload) == {"error_code"} and _error_code(payload.get("error_code")):
        return
    raise ValueError("invalid_phase_payload")


def _text(value: object, maximum: int, *, allow_empty: bool = False) -> bool:
    return isinstance(value, str) and len(value) <= maximum and (allow_empty or bool(value))


def _error_code(value: object) -> bool:
    return isinstance(value, str) and len(value) <= 80 and value.isidentifier()


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
