"""A persistent, least-privilege Docker executor for candidate verification."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import hashlib
import json
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time
from typing import Protocol

from tools.qykw.change import CommandResult


_IMAGE_PATTERN = re.compile(
    r"^[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$"
)
_ENV_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_FORBIDDEN_ENV_FRAGMENT = re.compile(
    r"(?:^QYKW|^GITHUB|TOKEN|KEY|SECRET|PASSWORD)", re.IGNORECASE
)
_FORBIDDEN_PYTHON_ENV = frozenset(
    {"PYTHONHOME", "PYTHONINSPECT", "PYTHONPATH", "PYTHONSTARTUP"}
)
_ALLOWED_PROFILE_ENV = frozenset(
    {"PYTHONHASHSEED", "PYTHONIOENCODING", "PYTHONUTF8", "PYTHONWARNINGS"}
)
_SHELLS = frozenset(
    {"sh", "bash", "dash", "zsh", "fish", "cmd", "cmd.exe", "pwsh", "powershell"}
)
_FIXED_ENV = (
    "PATH=/usr/local/bin:/usr/bin:/bin",
    "HOME=/tmp",
    "TMPDIR=/tmp",
    "PYTHONDONTWRITEBYTECODE=1",
    "PYTHONUNBUFFERED=1",
    "LANG=C.UTF-8",
    "LC_ALL=C.UTF-8",
)
_FIXED_ENV_NAMES = frozenset(item.split("=", 1)[0] for item in _FIXED_ENV)
_MAX_TIMEOUT_SECONDS = 900
_MAX_OUTPUT_LIMIT_BYTES = 1024 * 1024
_DIAGNOSTIC_SAMPLE_BYTES = 64 * 1024
_CONTROL_OUTPUT_LIMIT_BYTES = 4096
class SandboxError(RuntimeError):
    """Stable sandbox failure that is safe to expose outside the runner."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _Process(Protocol):
    stdout: object
    returncode: int

    def poll(self) -> int | None: ...
    def wait(self, timeout: float | None = None) -> int: ...
    def kill(self) -> None: ...


class _Backend(Protocol):
    def control(self, argv: tuple[str, ...], timeout_seconds: int) -> object: ...
    def stream(self, argv: tuple[str, ...]) -> _Process: ...


class _SubprocessBackend:
    """Shell-free subprocess boundary, kept injectable for Docker-less tests."""

    def control(self, argv: tuple[str, ...], timeout_seconds: int) -> object:
        return subprocess.run(
            argv,
            shell=False,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
        )

    def stream(self, argv: tuple[str, ...]) -> _Process:
        return subprocess.Popen(
            argv,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            close_fds=True,
        )


class _OutputAccumulator:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.total = 0
        self.digest = hashlib.sha256()
        self.sample = bytearray()
        self.overflow = threading.Event()
        self.failed = threading.Event()
        self._lock = threading.Lock()

    def consume(self, stream: object) -> None:
        try:
            while True:
                chunk = stream.read(64 * 1024)  # type: ignore[attr-defined]
                if not chunk:
                    return
                if type(chunk) is not bytes:
                    self.failed.set()
                    return
                with self._lock:
                    self.digest.update(chunk)
                    self.total += len(chunk)
                    remaining = _DIAGNOSTIC_SAMPLE_BYTES - len(self.sample)
                    if remaining > 0:
                        self.sample.extend(chunk[:remaining])
                    if self.total > self.limit:
                        self.overflow.set()
        except Exception:  # the raw stream failure must never escape into logs
            self.failed.set()


class DockerSandboxExecutor:
    """Run trusted argv in one persistent, networkless Docker session.

    The controller provides a digest-pinned image and a materialized candidate
    directory.  The directory is the sole host bind and is always mounted at
    ``/workspace``.  A successful command preserves the container (and /tmp)
    for the next profile command; every terminal path tears it down.
    """

    def __init__(
        self,
        workspace_root: Path,
        image_ref: str,
        *,
        backend: _Backend | None = None,
        container_name_factory=None,
        monotonic=time.monotonic,
        sleep=time.sleep,
    ) -> None:
        if type(image_ref) is not str or _IMAGE_PATTERN.fullmatch(image_ref) is None:
            raise SandboxError("invalid_image_digest")
        try:
            root = Path(workspace_root).resolve(strict=True)
        except (OSError, RuntimeError, TypeError):
            raise SandboxError("invalid_workspace_root") from None
        if not root.is_dir():
            raise SandboxError("invalid_workspace_root")
        if "," in str(root) or "\x00" in str(root) or "\n" in str(root):
            raise SandboxError("invalid_workspace_root")
        if (root / ".git").exists() or (root / ".git").is_symlink():
            raise SandboxError("workspace_contains_git")

        factory = container_name_factory or (
            lambda: f"qykw-verify-{secrets.token_hex(12)}"
        )
        name = factory()
        if type(name) is not str or re.fullmatch(r"qykw-[a-z0-9-]{4,48}", name) is None:
            raise SandboxError("invalid_container_name")

        self.workspace_root = root
        self.image_ref = image_ref
        self._actual_image_digest = image_ref.rsplit("@", 1)[1]
        self.container_name = name
        self._backend = backend or _SubprocessBackend()
        self._monotonic = monotonic
        self._sleep = sleep
        self._started = False
        self._closed = False
        self._terminal_error: str | None = None

    @property
    def workspace_read_only(self) -> bool:
        """Declare the enforced candidate mount capability."""

        return True

    @property
    def actual_image_digest(self) -> str:
        """Return the exact digest parsed from the validated image reference."""

        return self._actual_image_digest

    def __enter__(self) -> DockerSandboxExecutor:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self.close()

    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: Mapping[str, str] | Iterable[tuple[str, str]],
        timeout_seconds: int,
        output_limit_bytes: int,
    ) -> CommandResult:
        """Execute one trusted command without a shell or host environment."""

        if self._terminal_error is not None:
            raise SandboxError(self._terminal_error)
        if self._closed:
            raise SandboxError("sandbox_session_closed")
        command = _validate_argv(argv)
        command_env = _validate_env(env)
        try:
            resolved_cwd = Path(cwd).resolve(strict=True)
        except (OSError, RuntimeError, TypeError):
            raise SandboxError("invalid_sandbox_cwd") from None
        if resolved_cwd != self.workspace_root:
            raise SandboxError("invalid_sandbox_cwd")
        if (
            type(timeout_seconds) is not int
            or not 1 <= timeout_seconds <= _MAX_TIMEOUT_SECONDS
        ):
            raise SandboxError("invalid_command_timeout")
        if (
            type(output_limit_bytes) is not int
            or not 1 <= output_limit_bytes <= _MAX_OUTPUT_LIMIT_BYTES
        ):
            raise SandboxError("invalid_output_limit")

        self._ensure_started()
        exec_argv = (
            "docker",
            "exec",
            "--workdir",
            "/workspace",
            "--user",
            "65532:65532",
            self.container_name,
            "env",
            "-i",
            *_FIXED_ENV,
            *command_env,
            *command,
        )
        started_at = self._monotonic()
        accumulator = _OutputAccumulator(output_limit_bytes)
        try:
            process = self._backend.stream(exec_argv)
            if process.stdout is None:
                raise SandboxError("sandbox_stream_unavailable")
            reader = threading.Thread(
                target=accumulator.consume,
                args=(process.stdout,),
                name="qykw-sandbox-output",
                daemon=True,
            )
            reader.start()
            while process.poll() is None:
                if accumulator.overflow.is_set():
                    process.kill()
                    self._abort("sandbox_output_limit_exceeded")
                if self._monotonic() - started_at >= timeout_seconds:
                    process.kill()
                    self._abort("sandbox_command_timeout")
                self._sleep(0.02)
            reader.join(timeout=2.0)
            if reader.is_alive() or accumulator.failed.is_set():
                process.kill()
                self._abort("sandbox_output_stream_failed")
            if accumulator.overflow.is_set():
                process.kill()
                self._abort("sandbox_output_limit_exceeded")
            returncode = process.wait(timeout=1.0)
        except SandboxError as exc:
            if self._started and not self._closed:
                self._terminal_error = exc.code
                self.close()
            raise
        except Exception:
            self._abort("sandbox_execution_failed")

        duration_ms = max(0, int((self._monotonic() - started_at) * 1000))
        result = CommandResult(
            name=Path(command[0]).name,
            argv_digest=_argv_digest(command),
            exit_code=returncode,
            timed_out=False,
            duration_ms=duration_ms,
            output_digest=accumulator.digest.hexdigest(),
            output_excerpt=_safe_excerpt(bytes(accumulator.sample)),
        )
        if returncode != 0:
            self.close()
        return result

    def close(self) -> None:
        """Remove the container and prove absence before claiming closure."""

        if self._closed and not self._started:
            return
        self._closed = True
        if self._started:
            kill_ok, _ = self._checked_control(
                ("docker", "kill", self.container_name)
            )
            remove_ok, _ = self._checked_control(
                ("docker", "rm", "--force", self.container_name)
            )
            absent_ok, output = self._checked_control(
                (
                    "docker",
                    "ps",
                    "--all",
                    "--quiet",
                    "--no-trunc",
                    "--filter",
                    f"name=^/{self.container_name}$",
                )
            )
            confirmed_absent = absent_ok and output.strip() == b""
            if kill_ok and remove_ok and confirmed_absent:
                self._started = False
                return
            self._terminal_error = "sandbox_cleanup_unconfirmed"
            raise SandboxError("sandbox_cleanup_unconfirmed")

    def _ensure_started(self) -> None:
        if self._started:
            return
        mount = f"type=bind,source={self.workspace_root},target=/workspace,readonly"
        run_argv = (
            "docker",
            "run",
            "--detach",
            "--rm",
            "--name",
            self.container_name,
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,noexec,size=64m,mode=1777",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--cpus",
            "1.0",
            "--memory",
            "1024m",
            "--memory-swap",
            "1024m",
            "--pids-limit",
            "256",
            "--user",
            "65532:65532",
            "--workdir",
            "/workspace",
            "--mount",
            mount,
            self.image_ref,
            "env",
            "-i",
            *_FIXED_ENV,
            "python",
            "-c",
            (
                "import signal; "
                "signal.signal(signal.SIGTERM, lambda *_: exit(0)); "
                "signal.pause()"
            ),
        )
        try:
            completed = self._backend.control(run_argv, 30)
            if getattr(completed, "returncode", None) != 0:
                raise SandboxError("sandbox_start_failed")
        except Exception:
            self._started = True
            self._terminal_error = "sandbox_start_failed"
            try:
                self.close()
            except SandboxError:
                raise
            raise SandboxError("sandbox_start_failed") from None
        self._started = True

    def _abort(self, code: str) -> None:
        self._terminal_error = code
        self.close()
        raise SandboxError(code)

    def _checked_control(self, argv: tuple[str, ...]) -> tuple[bool, bytes]:
        try:
            completed = self._backend.control(argv, 15)
        except Exception:
            return False, b""
        output = getattr(completed, "stdout", None)
        if type(output) is not bytes or len(output) > _CONTROL_OUTPUT_LIMIT_BYTES:
            return False, b""
        return getattr(completed, "returncode", None) == 0, output


def _validate_argv(argv: object) -> tuple[str, ...]:
    if (
        type(argv) is not tuple
        or not argv
        or any(
            type(item) is not str
            or not item
            or "\x00" in item
            or "\n" in item
            or "\r" in item
            for item in argv
        )
    ):
        raise SandboxError("invalid_command_argv")
    executable = Path(argv[0]).name.lower()
    if executable in _SHELLS:
        raise SandboxError("shell_command_forbidden")
    return argv


def _validate_env(
    env: Mapping[str, str] | Iterable[tuple[str, str]],
) -> tuple[str, ...]:
    try:
        pairs = tuple(env.items()) if isinstance(env, Mapping) else tuple(env)
    except (AttributeError, TypeError, ValueError):
        raise SandboxError("invalid_environment") from None
    seen: set[str] = set()
    normalized: list[tuple[str, str]] = []
    for pair in pairs:
        if type(pair) is not tuple or len(pair) != 2:
            raise SandboxError("invalid_environment")
        name, value = pair
        if type(name) is not str or type(value) is not str:
            raise SandboxError("invalid_environment")
        if _ENV_NAME_PATTERN.fullmatch(name) is None:
            raise SandboxError("invalid_environment_name")
        if _FORBIDDEN_ENV_FRAGMENT.search(name):
            raise SandboxError("forbidden_environment_name")
        if name in _FORBIDDEN_PYTHON_ENV:
            raise SandboxError("forbidden_environment_name")
        if name in _FIXED_ENV_NAMES:
            raise SandboxError("reserved_environment_name")
        if name not in _ALLOWED_PROFILE_ENV:
            raise SandboxError("untrusted_environment_name")
        if name in seen or "\x00" in value or "\n" in value or len(value) > 1024:
            raise SandboxError("invalid_environment")
        seen.add(name)
        normalized.append((name, value))
    return tuple(f"{name}={value}" for name, value in sorted(normalized))


def _argv_digest(argv: tuple[str, ...]) -> str:
    encoded = json.dumps(
        argv, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(b"qykw-command-argv-v1\0" + encoded).hexdigest()


def _safe_excerpt(sample: bytes) -> str:
    """Collapse candidate output to one fixed, non-parameterized status."""

    text = sample.decode("utf-8", errors="replace")
    if re.search(r"(?m)^FAILED(?:\s|\()", text):
        return "failed"
    if re.search(
        r"\b[A-Za-z_][A-Za-z0-9_]*(?:Error|Exception)\s*:", text
    ):
        return "error"
    if re.search(r"(?m)^OK(?:\s+\(skipped=\d+\))?\s*$", text):
        return "ok"
    return ""
