"""Strict artifact boundary for isolated qykw workflow phases."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import Sequence

from tools.qykw.change_phases import (
    CHANGE_ARTIFACT_LIMITS as _CHANGE_ARTIFACT_LIMITS,
    CHANGE_HANDLER_NAMES as _CHANGE_HANDLER_NAMES,
    CHANGE_PHASES as _CHANGE_CLI_PHASES,
    CHANGE_PREDECESSORS,
    build_change_artifact,
    validate_change_artifact,
)


_REVIEW_CLI_PHASES = frozenset({"control", "authorize", "analyze", "publish", "record-failure"})
_CLI_PHASES = _REVIEW_CLI_PHASES | _CHANGE_CLI_PHASES
_ARTIFACT_PHASES = _CLI_PHASES | {"request"}
_PREDECESSORS = {
    "control": frozenset({"control"}),
    "authorize": frozenset({"request"}),
    "analyze": frozenset({"authorize"}),
    "publish": frozenset({"analyze"}),
    "record-failure": frozenset({"authorize", "analyze", "publish"}),
    **CHANGE_PREDECESSORS,
}
_MAX_ARTIFACT_BYTES = 64 * 1024
_ARTIFACT_KEYS = frozenset({"version", "phase", "run", "payload"})
_RUN_KEYS = frozenset({
    "run_id", "idempotency_key", "repository_id", "repository", "pr_number",
    "event_name", "event_action", "source_repository", "source_head_sha",
    "target_base_sha", "target_base_ref", "actor_login", "trigger_comment_id",
    "trigger_comment_kind", "command",
})
_COMMAND_KEYS = frozenset({"name", "argument", "mode"})
_PUBLIC_ERROR_CODES = frozenset({
    "ambiguous_change_phase_dependencies",
    "artifact_file_mismatch",
    "artifact_phase_mismatch",
    "artifact_required",
    "artifact_runtime_mismatch",
    "artifact_too_large",
    "change_handler_phase_mismatch",
    "change_phase_dependencies_unavailable",
    "control_command_not_stop",
    "error_code_not_allowed",
    "immutable_run_binding_changed",
    "invalid_artifact_digest",
    "invalid_artifact_file_binding",
    "invalid_artifact_json",
    "invalid_artifact_predecessor",
    "invalid_artifact_runtime",
    "invalid_artifact_schema",
    "invalid_change_phase",
    "invalid_context_digest",
    "invalid_controller_sha",
    "invalid_error_code",
    "invalid_job_results",
    "invalid_phase_payload",
    "invalid_phase_result",
    "invalid_publication_journal_root",
    "invalid_run_binding",
    "invalid_verification_image_digest",
    "invalid_workflow_run_id",
    "phase_controller_required",
    "phase_credentials_unavailable",
    "phase_job_results_unavailable",
    "phase_not_available",
    "phase_runtime_mismatch",
    "root_artifact_not_allowed",
    "root_phase_not_available",
    "unexpected_qykw_environment",
    "unsafe_artifact_path",
    "unsafe_output_path",
    "unsupported_artifact_version",
    "untrusted_phase_runtime",
    "verification_image_digest_unavailable",
})


def main(argv: Sequence[str] | None = None, *, controller: object | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.qykw", allow_abbrev=False)
    parser.add_argument("--phase", required=True, choices=sorted(_CLI_PHASES))
    parser.add_argument("--artifact")
    parser.add_argument("--output", required=True)
    parser.add_argument("--error-code")
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code)
    try:
        if args.phase == "authorize-change" and args.artifact:
            raise ValueError("root_artifact_not_allowed")
        root = args.phase == "authorize-change" or (
            args.phase in {"control", "authorize"}
            and (not args.artifact or not Path(args.artifact).is_file())
        )
        if controller is None and (root or os.environ.get("GITHUB_ACTIONS") == "true"):
            if args.phase in _CHANGE_CLI_PHASES:
                from tools.qykw.change_phases import build_change_controller
                from tools.qykw.change_runtime import build_production_change_factory

                controller = build_change_controller(
                    args.phase, factory=build_production_change_factory()
                )
            else:
                from tools.qykw.phases import build_production_controller

                controller = build_production_controller(args.phase)
        if root:
            if args.phase in _CHANGE_CLI_PHASES:
                result = _run_change_root(args.phase, controller)
            else:
                root_method = getattr(controller, "root", None)
                if not callable(root_method):
                    raise ValueError("root_phase_not_available")
                result = root_method()
            _validate_artifact(result, expected_phase=args.phase)
        else:
            if not args.artifact:
                raise ValueError("artifact_required")
            artifact = _read_artifact(Path(args.artifact))
            result = _run_phase(args.phase, artifact, controller, args.error_code)
        _write_artifact(Path(args.output), result)
    except Exception as error:
        return _error(_safe_code(error))
    return 0


def _run_phase(phase: str, artifact: dict[str, object], controller: object | None,
               error_code: str | None) -> dict[str, object]:
    if phase in _CHANGE_CLI_PHASES:
        if error_code is not None:
            raise ValueError("error_code_not_allowed")
        return _run_change_phase(phase, artifact, controller)
    if artifact["phase"] not in _PREDECESSORS[phase]:
        raise ValueError("artifact_phase_mismatch")
    if phase == "control" and artifact["run"]["command"]["name"] != "停止":  # type: ignore[index]
        raise ValueError("control_command_not_stop")
    if phase == "record-failure":
        if not _error_code(error_code):
            raise ValueError("invalid_error_code")
        method = getattr(controller, "record_failure", None) if controller is not None else None
        if callable(method):
            result = method(artifact, error_code)
            if not isinstance(result, dict):
                raise ValueError("invalid_phase_result")
            _validate_artifact(result, expected_phase=phase)
            if result["run"] != artifact["run"]:
                raise ValueError("immutable_run_binding_changed")
            return result
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


def _run_change_root(phase: str, controller: object | None) -> dict[str, object]:
    if phase != "authorize-change" or controller is None:
        raise ValueError("root_phase_not_available")
    runtime = _trusted_runtime(controller, phase)
    method = getattr(controller, _CHANGE_HANDLER_NAMES[phase], None)
    if not callable(method):
        raise ValueError("phase_not_available")
    result = method()
    if not isinstance(result, dict) or set(result) != {"run", "payload"}:
        raise ValueError("invalid_phase_result")
    return _change_artifact(
        phase,
        result["run"],
        result["payload"],
        workflow_run_id=runtime.workflow_run_id,
        controller_sha=runtime.controller_sha,
        verification_profile=runtime.verification_profile,
        predecessor=None,
    )


def _run_change_phase(
    phase: str, artifact: dict[str, object], controller: object | None
) -> dict[str, object]:
    _validate_artifact(artifact)
    if artifact["phase"] not in _PREDECESSORS[phase]:
        raise ValueError("artifact_phase_mismatch")
    if artifact["run"] is None:
        raise ValueError("invalid_run_binding")
    if controller is None:
        raise ValueError("phase_controller_required")
    runtime = _trusted_runtime(controller, phase)
    expected_runtime = {
        "controller_sha": runtime.controller_sha,
        "verification_profile": runtime.verification_profile,
    }
    if (
        artifact["workflow_run_id"] != runtime.workflow_run_id
        or artifact["runtime"] != expected_runtime
    ):
        raise ValueError("artifact_runtime_mismatch")
    method = getattr(controller, _CHANGE_HANDLER_NAMES[phase], None)
    if not callable(method):
        raise ValueError("phase_not_available")
    payload = method(artifact)
    return _change_artifact(
        phase,
        artifact["run"],
        payload,
        workflow_run_id=runtime.workflow_run_id,
        controller_sha=runtime.controller_sha,
        verification_profile=runtime.verification_profile,
        predecessor=artifact,
    )


def _trusted_runtime(controller: object, phase: str):
    from tools.qykw.change_phases import TrustedPhaseRuntime

    runtime = getattr(controller, "runtime", None)
    if type(runtime) is not TrustedPhaseRuntime or runtime.phase != phase:
        raise ValueError("untrusted_phase_runtime")
    return runtime


def _artifact(phase: str, run: object, payload: object) -> dict[str, object]:
    result = {"version": 1, "phase": phase, "run": run, "payload": payload}
    _validate_artifact(result, expected_phase=phase)
    return result


def _change_artifact(
    phase: str,
    run: object,
    payload: object,
    *,
    workflow_run_id: int,
    controller_sha: str,
    verification_profile: str,
    predecessor: dict[str, object] | None,
) -> dict[str, object]:
    return build_change_artifact(
        phase,
        run,
        payload,
        workflow_run_id=workflow_run_id,
        controller_sha=controller_sha,
        verification_profile=verification_profile,
        predecessor=predecessor,
        validate_run=_validate_run,
    )


def _read_artifact(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file() or not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
        raise ValueError("unsafe_artifact_path")
    largest = max(_MAX_ARTIFACT_BYTES, *_CHANGE_ARTIFACT_LIMITS.values())
    if path.stat().st_size > largest:
        raise ValueError("artifact_too_large")
    raw = path.read_bytes()
    if len(raw) > largest:
        raise ValueError("artifact_too_large")
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("invalid_artifact_json") from error
    _validate_artifact(payload)
    phase = payload.get("phase")
    limit = _CHANGE_ARTIFACT_LIMITS.get(phase, _MAX_ARTIFACT_BYTES)
    if len(raw) > limit:
        raise ValueError("artifact_too_large")
    if phase in _CHANGE_CLI_PHASES and path.name != payload["file"]["name"]:
        raise ValueError("artifact_file_mismatch")
    return payload


def _validate_artifact(payload: object, *, expected_phase: str | None = None) -> None:
    if isinstance(payload, dict) and (
        payload.get("phase") in _CHANGE_CLI_PHASES or "schema_version" in payload
    ):
        _validate_change_artifact(payload, expected_phase=expected_phase)
        return
    if not isinstance(payload, dict) or set(payload) != _ARTIFACT_KEYS:
        raise ValueError("invalid_artifact_schema")
    phase = payload.get("phase")
    if phase not in _ARTIFACT_PHASES or (expected_phase is not None and phase != expected_phase):
        raise ValueError("artifact_phase_mismatch")
    if payload.get("version") != 1:
        raise ValueError("unsupported_artifact_version")
    run = payload.get("run")
    if run is None:
        if not isinstance(payload.get("payload"), dict) or payload["payload"].get("status") != "skipped":
            raise ValueError("invalid_run_binding")
        _validate_skipped_payload(payload["payload"])
        return
    _validate_run(run)
    if isinstance(payload.get("payload"), dict) and payload["payload"].get("status") == "skipped":
        _validate_skipped_payload(payload["payload"])
        return
    _validate_payload(phase, payload.get("payload"))


def _validate_change_artifact(
    payload: object, *, expected_phase: str | None = None
) -> None:
    validate_change_artifact(
        payload,
        expected_phase=expected_phase,
        validate_run=_validate_run,
    )


def _unique_object(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    del value
    raise ValueError("invalid_json_constant")


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
    comment_id = run.get("trigger_comment_id")
    comment_kind = run.get("trigger_comment_kind")
    if (comment_id is None) != (comment_kind is None):
        raise ValueError("invalid_run_binding")
    if comment_id is not None and (
        type(comment_id) is not int or comment_id <= 0 or comment_kind not in {"issue", "review"}
    ):
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
        if _valid_structured_analysis(payload):
            return
    if phase == "publish" and payload == {"published": True}:
        return
    if phase == "publish" and set(payload) == {"published", "status"} and type(payload.get("published")) is bool and _text(payload.get("status"), 64):
        return
    if phase == "record-failure" and set(payload) == {"error_code"} and _error_code(payload.get("error_code")):
        return
    raise ValueError("invalid_phase_payload")


def _validate_skipped_payload(payload: object) -> None:
    if not isinstance(payload, dict) or set(payload) != {"status", "reason"}:
        raise ValueError("invalid_phase_payload")
    if payload.get("status") != "skipped" or not _text(payload.get("reason"), 80):
        raise ValueError("invalid_phase_payload")


def _valid_structured_analysis(payload: dict[str, object]) -> bool:
    kind = payload.get("kind")
    status = payload.get("status")
    if kind == "none" and status in {"canceled", "stale"} and set(payload) == {"kind", "status"}:
        return True
    if kind == "advisory" and status == "completed" and set(payload) == {"kind", "status", "advisory"}:
        value = payload.get("advisory")
        return (isinstance(value, dict) and set(value) == {"title", "body", "evidence", "limitations"}
                and _text(value.get("title"), 160) and _text(value.get("body"), 6000)
                and all(isinstance(value[key], list) and len(value[key]) <= 20 and all(_text(item, 2000) for item in value[key]) for key in ("evidence", "limitations")))
    if kind == "review" and status == "completed" and set(payload) == {"kind", "status", "review"}:
        value = payload.get("review")
        if not isinstance(value, dict) or set(value) != {"conclusion", "findings", "coverage", "validation_notes", "limitations"} or not _text(value.get("conclusion"), 500):
            return False
        if not isinstance(value.get("findings"), list) or len(value["findings"]) > 20 or not all(_valid_finding(item) for item in value["findings"]):
            return False
        coverage = value.get("coverage")
        if not isinstance(coverage, dict) or set(coverage) != {"total_files", "reviewed_files", "total_hunks", "reviewed_hunks", "omissions", "explains_every_file"}:
            return False
        numeric = ("total_files", "reviewed_files", "total_hunks", "reviewed_hunks")
        if (any(type(coverage.get(key)) is not int or not 0 <= coverage[key] <= 1_000_000 for key in numeric)
                or coverage["reviewed_files"] > coverage["total_files"] or coverage["reviewed_hunks"] > coverage["total_hunks"]
                or type(coverage.get("explains_every_file")) is not bool
                or not _valid_text_list(coverage.get("omissions"))):
            return False
        return _valid_text_list(value.get("validation_notes")) and _valid_text_list(value.get("limitations"))
    return False


def _valid_text_list(value: object) -> bool:
    return isinstance(value, list) and len(value) <= 20 and all(_text(item, 2000) for item in value)


def _valid_finding(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"path", "line", "side", "severity", "failure_path", "impact", "evidence", "suggestion", "verification", "fingerprint"}:
        return False
    if type(value.get("line")) is not int or not 1 <= value["line"] <= 1_000_000:
        return False
    if value.get("side") not in {"LEFT", "RIGHT"} or value.get("severity") not in {"P0", "P1", "P2"}:
        return False
    maximums = {"path": 1024, "failure_path": 2000, "impact": 2000, "evidence": 2000,
                "suggestion": 2000, "verification": 2000, "fingerprint": 128}
    return all(_text(value.get(key), maximum) for key, maximum in maximums.items())


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
    phase = payload.get("phase")
    limit = _CHANGE_ARTIFACT_LIMITS.get(phase, _MAX_ARTIFACT_BYTES)
    if len(encoded) > limit:
        raise ValueError("artifact_too_large")
    if phase in _CHANGE_CLI_PHASES and path.name != payload["file"]["name"]:
        raise ValueError("artifact_file_mismatch")
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
    from tools.qykw.config import ConfigError
    from tools.qykw.context import ContextError
    from tools.qykw.domain import InferenceError, InferenceErrorCode
    from tools.qykw.github import GitHubError
    from tools.qykw.prompts import PromptError
    from tools.qykw.provider import ProviderError

    if isinstance(error, ProviderError):
        return f"inference_{error.code.value}"
    if isinstance(error, InferenceError) and isinstance(error.failure.code, InferenceErrorCode):
        return f"inference_{error.failure.code.value}"
    for error_type, public_code in (
        (ConfigError, "analysis_config_failed"),
        (ContextError, "analysis_context_failed"),
        (GitHubError, "analysis_github_failed"),
        (PromptError, "analysis_prompt_failed"),
    ):
        if isinstance(error, error_type):
            return public_code
    code = str(error)
    return code if code in _PUBLIC_ERROR_CODES else "phase_failed"


if __name__ == "__main__":
    raise SystemExit(main())
