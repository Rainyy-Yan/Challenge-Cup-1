"""Credential-separated controllers for the authorized-change workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
from types import MappingProxyType
from typing import Callable, Mapping, Protocol

from tools.qykw.verification import get_verification_profile


CHANGE_PHASES = frozenset(
    {
        "authorize-change",
        "prepare-change",
        "verify-change",
        "publish-change",
        "record-change-result",
    }
)
CHANGE_HANDLER_NAMES = {
    "authorize-change": "authorize_change",
    "prepare-change": "prepare_change",
    "verify-change": "verify_change",
    "publish-change": "publish_change",
    "record-change-result": "record_change_result",
}
CHANGE_PREDECESSORS = {
    "authorize-change": frozenset(),
    "prepare-change": frozenset({"authorize-change"}),
    "verify-change": frozenset({"prepare-change"}),
    "publish-change": frozenset({"verify-change"}),
    "record-change-result": frozenset(
        {"authorize-change", "prepare-change", "verify-change", "publish-change"}
    ),
}
CHANGE_ARTIFACT_LIMITS = {
    "authorize-change": 64 * 1024,
    "prepare-change": 3 * 1024 * 1024,
    "verify-change": 3 * 1024 * 1024,
    "publish-change": 256 * 1024,
    "record-change-result": 256 * 1024,
}
CHANGE_FILE_PURPOSES = {
    "authorize-change": "authorized-change-request",
    "prepare-change": "prepared-change-manifest",
    "verify-change": "verified-change-attestation",
    "publish-change": "change-publication-result",
    "record-change-result": "recorded-change-result",
}
CHANGE_ARTIFACT_KEYS = frozenset(
    {
        "schema_version",
        "phase",
        "workflow_run_id",
        "run",
        "context_digest",
        "runtime",
        "predecessor",
        "file",
        "payload",
        "digest",
    }
)

_OID = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMON_ENVIRONMENT = frozenset(
    {
        "GITHUB_ACTIONS",
        "GITHUB_API_URL",
        "GITHUB_EVENT_NAME",
        "GITHUB_EVENT_PATH",
        "GITHUB_REPOSITORY",
        "GITHUB_REPOSITORY_ID",
        "GITHUB_RUN_ID",
        "GITHUB_SHA",
        "QYKW_CONFIG_PATH",
        "QYKW_VERIFICATION_PROFILE",
    }
)
_INFERENCE_ENVIRONMENT = frozenset(
    {
        "GITHUB_TOKEN",
        "QYKW_INFERENCE_API_KEY",
        "QYKW_INFERENCE_BASE_URL",
        "QYKW_INFERENCE_MODEL",
        "QYKW_INFERENCE_ALLOWED_HOSTS",
        "QYKW_INFERENCE_CONTEXT_WINDOW",
        "QYKW_INFERENCE_MAX_OUTPUT_TOKENS",
        "QYKW_INFERENCE_TIMEOUT_SECONDS",
    }
)
_ALLOWED_ENVIRONMENT = {
    "authorize-change": _COMMON_ENVIRONMENT | {"QYKW_REVIEW_TOKEN"},
    "prepare-change": _COMMON_ENVIRONMENT | _INFERENCE_ENVIRONMENT,
    "verify-change": _COMMON_ENVIRONMENT
    | {"GITHUB_TOKEN", "QYKW_VERIFICATION_IMAGE_DIGEST"},
    "publish-change": _COMMON_ENVIRONMENT
    | {"QYKW_PUBLISH_TOKEN", "QYKW_VERIFICATION_IMAGE_DIGEST", "RUNNER_TEMP"},
    "record-change-result": _COMMON_ENVIRONMENT | {"QYKW_REVIEW_TOKEN"},
}
_REQUIRED_CREDENTIALS = {
    "authorize-change": frozenset({"QYKW_REVIEW_TOKEN"}),
    "prepare-change": frozenset({"QYKW_INFERENCE_API_KEY"}),
    "verify-change": frozenset(),
    "publish-change": frozenset({"QYKW_PUBLISH_TOKEN"}),
    "record-change-result": frozenset({"QYKW_REVIEW_TOKEN"}),
}


@dataclass(frozen=True)
class TrustedPhaseRuntime:
    """Controller-owned facts; artifacts may only be compared with these facts."""

    phase: str
    workflow_run_id: int
    controller_sha: str
    verification_profile: str
    image_digest: str | None
    runner_temp: Path | None

    def __post_init__(self) -> None:
        if self.phase not in CHANGE_PHASES:
            raise ValueError("invalid_change_phase")
        if type(self.workflow_run_id) is not int or self.workflow_run_id <= 0:
            raise ValueError("invalid_workflow_run_id")
        if type(self.controller_sha) is not str or _OID.fullmatch(self.controller_sha) is None:
            raise ValueError("invalid_controller_sha")
        get_verification_profile(self.verification_profile)
        needs_image = self.phase in {"verify-change", "publish-change"}
        if needs_image != (self.image_digest is not None):
            raise ValueError("verification_image_digest_unavailable")
        if self.image_digest is not None and _IMAGE_DIGEST.fullmatch(self.image_digest) is None:
            raise ValueError("invalid_verification_image_digest")
        if self.runner_temp is not None and (
            self.phase != "publish-change"
            or not self.runner_temp.is_absolute()
        ):
            raise ValueError("invalid_publication_journal_root")


class ChangePhaseServices(Protocol):
    """Narrow integration seam; implementations live with their owning adapters."""

    def authorize_change(self, runtime: TrustedPhaseRuntime) -> dict[str, object]: ...

    def prepare_change(
        self, artifact: dict[str, object], runtime: TrustedPhaseRuntime
    ) -> dict[str, object]: ...

    def verify_change(
        self, artifact: dict[str, object], runtime: TrustedPhaseRuntime
    ) -> dict[str, object]: ...

    def publish_change(
        self, artifact: dict[str, object], runtime: TrustedPhaseRuntime
    ) -> dict[str, object]: ...

    def record_change_result(
        self, artifact: dict[str, object], runtime: TrustedPhaseRuntime
    ) -> dict[str, object]: ...


class ChangePhaseServicesFactory(Protocol):
    def __call__(
        self,
        phase: str,
        environment: Mapping[str, str],
        runtime: TrustedPhaseRuntime,
    ) -> ChangePhaseServices: ...


@dataclass
class _UnavailableServices:
    def _fail(self) -> dict[str, object]:
        raise ValueError("change_phase_dependencies_unavailable")

    def authorize_change(self, runtime: TrustedPhaseRuntime) -> dict[str, object]:
        del runtime
        return self._fail()

    def prepare_change(
        self, artifact: dict[str, object], runtime: TrustedPhaseRuntime
    ) -> dict[str, object]:
        del artifact, runtime
        return self._fail()

    def verify_change(
        self, artifact: dict[str, object], runtime: TrustedPhaseRuntime
    ) -> dict[str, object]:
        del artifact, runtime
        return self._fail()

    def publish_change(
        self, artifact: dict[str, object], runtime: TrustedPhaseRuntime
    ) -> dict[str, object]:
        del artifact, runtime
        return self._fail()

    def record_change_result(
        self, artifact: dict[str, object], runtime: TrustedPhaseRuntime
    ) -> dict[str, object]:
        del artifact, runtime
        return self._fail()


@dataclass
class ChangePhaseController:
    """One instance owns one phase, one credential set, and one handler."""

    phase: str
    source_environment: Mapping[str, str] | None = field(default=None, repr=False)
    services: ChangePhaseServices | None = field(default=None, repr=False, kw_only=True)
    runtime: TrustedPhaseRuntime | None = field(default=None, kw_only=True)
    factory: ChangePhaseServicesFactory | None = field(default=None, repr=False, kw_only=True)

    def __post_init__(self) -> None:
        if self.phase not in CHANGE_PHASES:
            raise ValueError("invalid_change_phase")
        source = os.environ if self.source_environment is None else self.source_environment
        narrow = _validate_and_narrow_environment(self.phase, source)
        runtime = self.runtime or _runtime_from_environment(self.phase, narrow)
        if runtime.phase != self.phase:
            raise ValueError("phase_runtime_mismatch")
        if self.services is not None and self.factory is not None:
            raise ValueError("ambiguous_change_phase_dependencies")
        services = self.services
        if services is None and self.factory is not None:
            services = self.factory(self.phase, MappingProxyType(narrow), runtime)
        self.runtime = runtime
        self.services = services or _UnavailableServices()
        self.source_environment = None

    def authorize_change(self) -> dict[str, object]:
        self._require_phase("authorize-change")
        return self.services.authorize_change(self.runtime)  # type: ignore[union-attr]

    def prepare_change(self, artifact: dict[str, object]) -> dict[str, object]:
        self._require_phase("prepare-change")
        return self.services.prepare_change(artifact, self.runtime)  # type: ignore[union-attr]

    def verify_change(self, artifact: dict[str, object]) -> dict[str, object]:
        self._require_phase("verify-change")
        return self.services.verify_change(artifact, self.runtime)  # type: ignore[union-attr]

    def publish_change(self, artifact: dict[str, object]) -> dict[str, object]:
        self._require_phase("publish-change")
        return self.services.publish_change(artifact, self.runtime)  # type: ignore[union-attr]

    def record_change_result(self, artifact: dict[str, object]) -> dict[str, object]:
        self._require_phase("record-change-result")
        return self.services.record_change_result(artifact, self.runtime)  # type: ignore[union-attr]

    def _require_phase(self, expected: str) -> None:
        if self.phase != expected:
            raise ValueError("change_handler_phase_mismatch")


def build_change_controller(
    phase: str,
    *,
    environment: Mapping[str, str] | None = None,
    services: ChangePhaseServices | None = None,
    runtime: TrustedPhaseRuntime | None = None,
    factory: ChangePhaseServicesFactory | None = None,
) -> ChangePhaseController:
    """Build a fail-closed controller; workflows inject the narrow adapters."""

    return ChangePhaseController(
        phase,
        environment,
        services=services,
        runtime=runtime,
        factory=factory,
    )


def build_change_artifact(
    phase: str,
    run: object,
    payload: object,
    *,
    workflow_run_id: int,
    controller_sha: str,
    verification_profile: str,
    predecessor: dict[str, object] | None,
    validate_run: Callable[[object], None],
) -> dict[str, object]:
    """Create one canonical envelope and bind it to its exact predecessor."""

    if phase not in CHANGE_PHASES:
        raise ValueError("invalid_change_phase")
    if predecessor is None:
        predecessor_binding = None
    else:
        validate_change_artifact(predecessor, validate_run=validate_run)
        if predecessor["phase"] not in CHANGE_PREDECESSORS[phase]:
            raise ValueError("artifact_phase_mismatch")
        predecessor_binding = {
            "phase": predecessor["phase"],
            "digest": predecessor["digest"],
        }
    context_digest = None if run is None else _context_digest(run, validate_run)
    result: dict[str, object] = {
        "schema_version": 1,
        "phase": phase,
        "workflow_run_id": workflow_run_id,
        "run": run,
        "context_digest": context_digest,
        "runtime": {
            "controller_sha": controller_sha,
            "verification_profile": verification_profile,
        },
        "predecessor": predecessor_binding,
        "file": {
            "name": f"{phase}.json",
            "purpose": CHANGE_FILE_PURPOSES[phase],
        },
        "payload": payload,
    }
    result["digest"] = _artifact_digest(result)
    validate_change_artifact(result, expected_phase=phase, validate_run=validate_run)
    return result


def validate_change_artifact(
    payload: object,
    *,
    expected_phase: str | None = None,
    validate_run: Callable[[object], None],
) -> None:
    """Validate the complete versioned envelope without trusting its claims."""

    if not isinstance(payload, dict) or set(payload) != CHANGE_ARTIFACT_KEYS:
        raise ValueError("invalid_artifact_schema")
    phase = payload.get("phase")
    if phase not in CHANGE_PHASES or (
        expected_phase is not None and phase != expected_phase
    ):
        raise ValueError("artifact_phase_mismatch")
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported_artifact_version")
    if type(payload.get("workflow_run_id")) is not int or payload["workflow_run_id"] <= 0:
        raise ValueError("invalid_workflow_run_id")
    run = payload.get("run")
    if run is not None:
        validate_run(run)
    context_digest = payload.get("context_digest")
    if context_digest != (None if run is None else _context_digest(run, validate_run)):
        raise ValueError("invalid_context_digest")
    runtime = payload.get("runtime")
    if (
        not isinstance(runtime, dict)
        or set(runtime) != {"controller_sha", "verification_profile"}
        or not _hex_digest(runtime.get("controller_sha"), 40)
        or runtime.get("verification_profile") not in {"backend", "frontend", "full"}
    ):
        raise ValueError("invalid_artifact_runtime")
    predecessor = payload.get("predecessor")
    if phase == "authorize-change":
        if predecessor is not None:
            raise ValueError("invalid_artifact_predecessor")
    elif (
        not isinstance(predecessor, dict)
        or set(predecessor) != {"phase", "digest"}
        or predecessor.get("phase") not in CHANGE_PREDECESSORS[phase]
        or not _hex_digest(predecessor.get("digest"), 64)
    ):
        raise ValueError("invalid_artifact_predecessor")
    if payload.get("file") != {
        "name": f"{phase}.json",
        "purpose": CHANGE_FILE_PURPOSES[phase],
    }:
        raise ValueError("invalid_artifact_file_binding")
    _validate_change_payload(phase, run, payload.get("payload"))
    if payload.get("digest") != _artifact_digest(payload):
        raise ValueError("invalid_artifact_digest")


def _validate_change_payload(phase: str, run: object, payload: object) -> None:
    if not isinstance(payload, dict) or set(payload) != {"status", "data"}:
        raise ValueError("invalid_phase_payload")
    status = payload.get("status")
    data = payload.get("data")
    if status == "skipped":
        if (
            not isinstance(data, dict)
            or set(data) != {"reason"}
            or not _text(data.get("reason"), 80)
        ):
            raise ValueError("invalid_phase_payload")
        return
    schemas = {
        "authorize-change": ({"accepted"}, {"request"}),
        "prepare-change": ({"prepared"}, {"request", "manifest"}),
        "verify-change": (
            {"verified", "failed", "canceled"},
            {"request", "manifest", "attestation"},
        ),
        "publish-change": (
            {"completed", "partial", "failed", "canceled"},
            {"publication"},
        ),
        "record-change-result": (
            {"completed", "partial", "failed", "canceled"},
            {"outcome"},
        ),
    }
    statuses, keys = schemas[phase]
    if run is None or status not in statuses or not isinstance(data, dict) or set(data) != keys:
        raise ValueError("invalid_phase_payload")
    _validate_json_value(data)


def _validate_json_value(value: object, *, depth: int = 0) -> None:
    if depth > 32:
        raise ValueError("invalid_phase_payload")
    if value is None or type(value) in {str, int, bool}:
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    if isinstance(value, dict) and all(
        isinstance(key, str) and len(key) <= 128 for key in value
    ):
        for item in value.values():
            _validate_json_value(item, depth=depth + 1)
        return
    raise ValueError("invalid_phase_payload")


def _context_digest(run: object, validate_run: Callable[[object], None]) -> str:
    validate_run(run)
    return hashlib.sha256(
        b"qykw-change-context-v1\0" + _canonical_json(run)
    ).hexdigest()


def _artifact_digest(payload: dict[str, object]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "digest"}
    return hashlib.sha256(
        b"qykw-change-artifact-v1\0" + _canonical_json(unsigned)
    ).hexdigest()


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ValueError("invalid_artifact_schema") from None


def _hex_digest(value: object, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _text(value: object, maximum: int) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= maximum


def _validate_and_narrow_environment(
    phase: str, source: Mapping[str, str]
) -> dict[str, str]:
    allowed = _ALLOWED_ENVIRONMENT[phase]
    qykw_keys = {key for key in source if key.startswith("QYKW_")}
    allowed_qykw = {key for key in allowed if key.startswith("QYKW_")}
    if qykw_keys - allowed_qykw:
        raise ValueError("unexpected_qykw_environment")
    if any(not source.get(key) for key in _REQUIRED_CREDENTIALS[phase]):
        raise ValueError("phase_credentials_unavailable")
    return {key: value for key, value in source.items() if key in allowed}


def _runtime_from_environment(
    phase: str, environment: Mapping[str, str]
) -> TrustedPhaseRuntime:
    try:
        workflow_run_id = int(environment.get("GITHUB_RUN_ID", ""))
    except ValueError:
        raise ValueError("invalid_workflow_run_id") from None
    image_digest = (
        environment.get("QYKW_VERIFICATION_IMAGE_DIGEST")
        if phase in {"verify-change", "publish-change"}
        else None
    )
    runner_temp: Path | None = None
    if phase == "publish-change" and environment.get("RUNNER_TEMP"):
        candidate = Path(environment["RUNNER_TEMP"])
        if candidate.is_symlink() or not candidate.is_dir():
            raise ValueError("invalid_publication_journal_root")
        runner_temp = candidate.resolve()
    return TrustedPhaseRuntime(
        phase=phase,
        workflow_run_id=workflow_run_id,
        controller_sha=environment.get("GITHUB_SHA", ""),
        verification_profile=environment.get("QYKW_VERIFICATION_PROFILE", ""),
        image_digest=image_digest,
        runner_temp=runner_temp,
    )
