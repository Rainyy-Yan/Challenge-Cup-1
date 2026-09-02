"""Trusted, controller-selected verification command profiles."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Protocol

from tools.qykw.change import (
    ChangeRequest,
    CommandResult,
    FileDigest,
    PatchManifest,
    PreparedWorkspace,
    VerificationAttestation,
)
from tools.qykw.patches import (
    _require_trusted_workspace,
    apply_patch_manifest,
    compute_workspace_tree_digest,
)
from tools.qykw.state import RunStateStore


_IMAGE_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_TIMEOUT_SECONDS = 900
_MAX_OUTPUT_LIMIT_BYTES = 1024 * 1024
_EXCERPT_LIMIT_BYTES = 2048


@dataclass(frozen=True)
class VerificationCommand:
    """One literal argv invocation; executors must always use ``shell=False``."""

    name: str
    argv: tuple[str, ...]
    env: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class VerificationProfile:
    """An immutable sequence selected only by its trusted profile name."""

    name: str
    commands: tuple[VerificationCommand, ...]


@dataclass(frozen=True)
class VerificationRuntimeMetadata:
    """Controller-only facts that candidate artifacts cannot self-assert."""

    workflow_run_id: int
    image_digest: str
    timeout_seconds: int
    output_limit_bytes: int

    def __post_init__(self) -> None:
        if (
            type(self.workflow_run_id) is not int
            or self.workflow_run_id <= 0
            or type(self.image_digest) is not str
            or _IMAGE_DIGEST_PATTERN.fullmatch(self.image_digest) is None
            or type(self.timeout_seconds) is not int
            or not 1 <= self.timeout_seconds <= _MAX_TIMEOUT_SECONDS
            or type(self.output_limit_bytes) is not int
            or not 1 <= self.output_limit_bytes <= _MAX_OUTPUT_LIMIT_BYTES
        ):
            raise ValueError("invalid_verification_runtime")


class CommandExecutor(Protocol):
    """Persistent sandbox session owned and removed by one verification run."""

    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: int,
        output_limit_bytes: int,
    ) -> CommandResult: ...

    def close(self) -> None: ...


_BACKEND_COMMANDS = (
    VerificationCommand(
        "backend-compile",
        (
            "python",
            "-m",
            "compileall",
            "-q",
            "agents",
            "core",
            "evalkit",
            "tools",
            "build_showcase.py",
            "cli.py",
            "config.py",
            "orchestrator.py",
            "server.py",
        ),
    ),
    VerificationCommand(
        "backend-tests",
        ("python", "-m", "unittest", "discover", "-s", "tests", "-v"),
    ),
)

_FRONTEND_COMMANDS = (
    VerificationCommand(
        "frontend-node-syntax", ("node", "--check", "web/engine.js")
    ),
    VerificationCommand(
        "frontend-parity",
        ("python", "-m", "unittest", "tests.test_parity", "-v"),
    ),
    VerificationCommand(
        "frontend-snapshot",
        (
            "python",
            "-m",
            "evalkit.snapshot",
            "--out",
            "/tmp/qykw-snapshot.json",
        ),
    ),
    VerificationCommand(
        "frontend-snapshot-assert",
        (
            "python",
            "-c",
            "import json; data=json.load(open('/tmp/qykw-snapshot.json', encoding='utf-8')); "
            "assert set(data['sessions']) == {'P-A', 'P-B', 'P-C'}; "
            "assert data['items'] and data['kb']",
        ),
    ),
    VerificationCommand(
        "frontend-showcase", ("python", "build_showcase.py")
    ),
)

_SMOKE_COMMAND = VerificationCommand(
    "full-smoke", ("/opt/qykw/verify_smoke.py", "/workspace", "8765")
)

_PROFILES = {
    "backend": VerificationProfile("backend", _BACKEND_COMMANDS),
    "frontend": VerificationProfile("frontend", _FRONTEND_COMMANDS),
    "full": VerificationProfile(
        "full", _BACKEND_COMMANDS + _FRONTEND_COMMANDS + (_SMOKE_COMMAND,)
    ),
}


def get_verification_profile(name: str) -> VerificationProfile:
    """Return a fixed profile without interpreting caller-provided commands."""

    if type(name) is not str or name not in _PROFILES:
        raise ValueError("unknown_verification_profile")
    return _PROFILES[name]


def verify_change(
    request: ChangeRequest,
    manifest: PatchManifest,
    workspace: PreparedWorkspace,
    executor: CommandExecutor,
    state_store: RunStateStore,
    *,
    runtime: VerificationRuntimeMetadata,
) -> VerificationAttestation:
    """Apply and verify one manifest without trusting candidate-owned state."""

    closed = False
    pending: dict[str, object] | None = None
    try:
        _validate_verification_inputs(request, manifest, runtime)
        root, source_files = _require_trusted_workspace(workspace)
        baseline_files = tuple(source_files)
        baseline_digest = compute_workspace_tree_digest(baseline_files)

        results: list[CommandResult] = []
        canceled = _cancel_requested(state_store, request)
        output_files: tuple[FileDigest, ...] = ()
        output_tree_digest = compute_workspace_tree_digest(())
        workspace_tree_digest = baseline_digest
        expected_files = baseline_files

        if not canceled:
            applied = apply_patch_manifest(manifest, workspace)
            output_files = applied.files
            output_tree_digest = applied.output_tree_digest
            workspace_tree_digest = applied.workspace_tree_digest
            expected_files = _patched_tree(baseline_files, output_files)
            _verify_workspace(root, expected_files, output_files)
            profile = get_verification_profile(request.verification_profile)

            failed = False
            for command in profile.commands:
                if _cancel_requested(state_store, request):
                    canceled = True
                    break
                _verify_workspace(root, expected_files, output_files)
                raw_result = executor.run(
                    command.argv,
                    cwd=root,
                    env=dict(command.env),
                    timeout_seconds=runtime.timeout_seconds,
                    output_limit_bytes=runtime.output_limit_bytes,
                )
                result = _trusted_command_result(command, raw_result)
                results.append(result)
                _verify_workspace(root, expected_files, output_files)
                if _cancel_requested(state_store, request):
                    canceled = True
                    break
                if result.timed_out or result.exit_code != 0:
                    failed = True
                    break

            if not canceled:
                _verify_workspace(root, expected_files, output_files)
                canceled = _cancel_requested(state_store, request)

            success = (
                not canceled
                and not failed
                and len(results) == len(profile.commands)
                and all(
                    result.exit_code == 0 and not result.timed_out
                    for result in results
                )
            )
        else:
            _verify_workspace(root, baseline_files, ())
            success = False

        # Stop all candidate processes before the last host-side tree proof.
        executor.close()
        closed = True
        _verify_workspace(root, expected_files, output_files)
        pending = {
            "schema_version": 1,
            "workflow_run_id": runtime.workflow_run_id,
            "run_id": request.context.run_id,
            "source_repository": request.source_repository,
            "source_head_sha": request.source_head_sha,
            "target_repository": request.target_repository,
            "target_base_sha": request.target_base_sha,
            "target_base_ref": request.target_base_ref,
            "manifest_digest": manifest.digest,
            "profile": request.verification_profile,
            "image_digest": runtime.image_digest,
            "output_tree_digest": output_tree_digest,
            "workspace_tree_digest": workspace_tree_digest,
            "output_files": output_files,
            "success": success,
            "canceled": canceled,
            "results": tuple(results),
        }
    finally:
        if not closed:
            executor.close()

    if pending is None:  # pragma: no cover - defensive, exceptions leave above
        raise RuntimeError("verification_attestation_unavailable")
    return VerificationAttestation(**pending)  # type: ignore[arg-type]


def _validate_verification_inputs(
    request: ChangeRequest,
    manifest: PatchManifest,
    runtime: VerificationRuntimeMetadata,
) -> None:
    if type(runtime) is not VerificationRuntimeMetadata:
        raise ValueError("invalid_verification_runtime")
    if type(request) is not ChangeRequest or type(manifest) is not PatchManifest:
        raise ValueError("invalid_verification_input")
    context = request.context
    if (
        context.run_id != manifest.run_id
        or context.pr_number != manifest.source_pr_number
        or context.source_repository != manifest.source_repository
        or context.source_head_sha != manifest.source_head_sha
        or context.target_base_sha != manifest.target_base_sha
        or context.target_base_ref != manifest.target_base_ref
        or request.source_repository != manifest.source_repository
        or request.target_repository != manifest.target_repository
        or request.source_head_sha != manifest.source_head_sha
        or request.target_base_sha != manifest.target_base_sha
        or request.target_base_ref != manifest.target_base_ref
        or request.verification_profile != manifest.verification_profile
    ):
        raise ValueError("verification_request_mismatch")
    get_verification_profile(request.verification_profile)


def _cancel_requested(state_store: RunStateStore, request: ChangeRequest) -> bool:
    value = state_store.is_cancel_requested(
        request.context.pr_number, request.context.run_id
    )
    if type(value) is not bool:
        raise ValueError("invalid_cancel_state")
    return value


def _patched_tree(
    source_files: tuple[FileDigest, ...], output_files: tuple[FileDigest, ...]
) -> tuple[FileDigest, ...]:
    combined = {item.path: item for item in source_files}
    combined.update({item.path: item for item in output_files})
    return tuple(combined[path] for path in sorted(combined))


def _verify_workspace(
    root: Path,
    expected_files: tuple[FileDigest, ...],
    output_files: tuple[FileDigest, ...],
) -> None:
    actual_files = _snapshot_workspace(root, expected_files)
    if actual_files != expected_files:
        raise ValueError("verification_workspace_changed")
    if compute_workspace_tree_digest(actual_files) != compute_workspace_tree_digest(
        expected_files
    ):
        raise ValueError("verification_workspace_changed")
    actual_by_path = {item.path: item for item in actual_files}
    actual_outputs = tuple(actual_by_path[item.path] for item in output_files)
    if actual_outputs != output_files:
        raise ValueError("verification_output_changed")


def _snapshot_workspace(
    root: Path, expected_files: tuple[FileDigest, ...]
) -> tuple[FileDigest, ...]:
    expected_by_path = {item.path: item for item in expected_files}
    paths: list[str] = []

    def visit(directory: Path, prefix: str = "") -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError:
            raise ValueError("verification_workspace_unreadable") from None
        for entry in entries:
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            if entry.name == ".git":
                raise ValueError("verification_git_metadata_created")
            try:
                if entry.is_symlink():
                    raise ValueError("verification_workspace_symlink_created")
                if entry.is_dir(follow_symlinks=False):
                    visit(Path(entry.path), relative)
                elif entry.is_file(follow_symlinks=False):
                    paths.append(relative)
                else:
                    raise ValueError("verification_workspace_special_file_created")
            except OSError:
                raise ValueError("verification_workspace_unreadable") from None

    visit(root)
    if tuple(paths) != tuple(sorted(expected_by_path)):
        raise ValueError("verification_workspace_file_list_changed")
    return tuple(
        _hash_workspace_file(root / path, expected_by_path[path]) for path in paths
    )


def _hash_workspace_file(path: Path, expected: FileDigest) -> FileDigest:
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("verification_workspace_changed")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(64 * 1024):
                digest.update(chunk)
        after = path.lstat()
    except ValueError:
        raise
    except OSError:
        raise ValueError("verification_workspace_unreadable") from None
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or not stat.S_ISREG(after.st_mode)
    ):
        raise ValueError("verification_workspace_changed")
    mode = expected.mode
    if os.name == "posix":
        mode = "100755" if stat.S_IMODE(after.st_mode) & 0o111 else "100644"
    return FileDigest(expected.path, mode, digest.hexdigest())


def _trusted_command_result(
    command: VerificationCommand, result: CommandResult
) -> CommandResult:
    expected_argv_digest = _argv_digest(command.argv)
    if (
        type(result) is not CommandResult
        or result.argv_digest != expected_argv_digest
        or (result.exit_code is not None and type(result.exit_code) is not int)
        or type(result.timed_out) is not bool
        or (result.exit_code is None and not result.timed_out)
        or type(result.duration_ms) is not int
        or result.duration_ms < 0
        or type(result.output_digest) is not str
        or _DIGEST_PATTERN.fullmatch(result.output_digest) is None
        or type(result.output_excerpt) is not str
    ):
        raise ValueError("invalid_command_result")
    return CommandResult(
        name=command.name,
        argv_digest=expected_argv_digest,
        exit_code=result.exit_code,
        timed_out=result.timed_out,
        duration_ms=result.duration_ms,
        output_digest=result.output_digest,
        output_excerpt=_sanitize_excerpt(result.output_excerpt),
    )


def _argv_digest(argv: tuple[str, ...]) -> str:
    encoded = json.dumps(
        argv, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(b"qykw-command-argv-v1\0" + encoded).hexdigest()


def _sanitize_excerpt(value: str) -> str:
    safe: list[str] = []
    patterns = (
        re.compile(r"^Ran \d+ tests?$"),
        re.compile(r"^OK(?: \(skipped=\d+\))?$"),
        re.compile(
            r"^FAILED \((?:(?:failures|errors|skipped)=\d+)(?:, (?:failures|errors|skipped)=\d+)*\)$"
        ),
        re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:Error|Exception)$"),
    )
    for line in value.splitlines():
        stripped = line.strip()
        if any(pattern.fullmatch(stripped) for pattern in patterns):
            safe.append(stripped)
    return "\n".join(safe).encode("ascii")[:_EXCERPT_LIMIT_BYTES].decode("ascii")
