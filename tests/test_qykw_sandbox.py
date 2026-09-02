"""Security contract tests for qykw's isolated verification runtime."""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from tools.qykw import verify_smoke
from tools.qykw.sandbox import DockerSandboxExecutor, SandboxError


_IMAGE = "qykw-verify@sha256:" + "a" * 64


class _Completed:
    def __init__(self, returncode: int = 0, stdout: bytes = b"container-id\n") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = b""


class _Process:
    def __init__(
        self,
        output: bytes = b"",
        *,
        returncode: int = 0,
        never_finishes: bool = False,
    ) -> None:
        self.stdout = io.BytesIO(output)
        self.returncode = returncode
        self.never_finishes = never_finishes
        self.killed = False

    def poll(self) -> int | None:
        return None if self.never_finishes and not self.killed else self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class _Backend:
    def __init__(self, processes: list[_Process] | None = None) -> None:
        self.control_calls: list[tuple[str, ...]] = []
        self.exec_calls: list[tuple[str, ...]] = []
        self.processes = list(processes or [])

    def control(self, argv: tuple[str, ...], timeout_seconds: int) -> _Completed:
        del timeout_seconds
        self.control_calls.append(argv)
        if argv[:3] == ("docker", "ps", "--all"):
            return _Completed(stdout=b"")
        return _Completed()

    def stream(self, argv: tuple[str, ...]) -> _Process:
        self.exec_calls.append(argv)
        if not self.processes:
            raise AssertionError("unexpected docker exec")
        return self.processes.pop(0)


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += max(seconds, 0.25)


class _BrokenStream:
    def __init__(self, value: object) -> None:
        self.value = value

    def read(self, size: int) -> object:
        del size
        if isinstance(self.value, BaseException):
            raise self.value
        value, self.value = self.value, b""
        return value


class TestDockerSandboxExecutor(unittest.TestCase):
    def _workspace(self, root: str) -> Path:
        workspace = Path(root, "candidate")
        workspace.mkdir()
        return workspace

    def test_rejects_unpinned_image_and_workspace_with_git_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            workspace = self._workspace(root)
            with self.assertRaisesRegex(SandboxError, "invalid_image_digest"):
                DockerSandboxExecutor(workspace, "qykw-verify:latest")

            (workspace / ".git").mkdir()
            with self.assertRaisesRegex(SandboxError, "workspace_contains_git"):
                DockerSandboxExecutor(workspace, _IMAGE)

    def test_run_uses_one_hardened_bind_and_clean_environments(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            workspace = self._workspace(root)
            backend = _Backend([_Process(b"Ran 2 tests in 0.1s\nOK\n")])
            with DockerSandboxExecutor(
                workspace,
                _IMAGE,
                backend=backend,
                container_name_factory=lambda: "qykw-test-session",
            ) as executor:
                result = executor.run(
                    ("python", "-m", "unittest"),
                    cwd=workspace,
                    env=(("PYTHONHASHSEED", "0"),),
                    timeout_seconds=30,
                    output_limit_bytes=4096,
                )

            run = backend.control_calls[0]
            self.assertEqual(run[:3], ("docker", "run", "--detach"))
            for pair in (
                ("--network", "none"),
                ("--read-only",),
                ("--cap-drop", "ALL"),
                ("--security-opt", "no-new-privileges"),
                ("--cpus", "1.0"),
                ("--memory", "1024m"),
                ("--memory-swap", "1024m"),
                ("--pids-limit", "256"),
                ("--user", "65532:65532"),
                ("--workdir", "/workspace"),
            ):
                self.assertIn(pair, tuple(zip(run, run[1:]))) if len(pair) == 2 else self.assertIn(pair[0], run)
            tmpfs = run[run.index("--tmpfs") + 1]
            self.assertEqual(tmpfs, "/tmp:rw,nosuid,nodev,noexec,size=64m,mode=1777")
            mounts = [run[index + 1] for index, item in enumerate(run) if item == "--mount"]
            self.assertEqual(
                mounts,
                [f"type=bind,source={workspace.resolve()},target=/workspace,readonly"],
            )
            self.assertEqual(run.count("--mount"), 1)
            self.assertIs(executor.workspace_read_only, True)
            self.assertEqual(executor.actual_image_digest, "sha256:" + "a" * 64)
            with self.assertRaises(AttributeError):
                executor.workspace_read_only = False  # type: ignore[misc]
            with self.assertRaises(AttributeError):
                executor.actual_image_digest = "sha256:" + "2" * 64  # type: ignore[misc]
            self.assertEqual(run[run.index(_IMAGE) + 1], "env")
            self.assertEqual(run[run.index(_IMAGE) + 2], "-i")
            forbidden = (
                "--privileged",
                "--device",
                "--pid=host",
                "--ipc=host",
                "--uts=host",
                "--env-file",
                "--volume",
                "docker.sock",
                "GITHUB_TOKEN",
                "QYKW_API_KEY",
            )
            joined = "\0".join(run)
            self.assertTrue(all(value not in joined for value in forbidden))

            command = backend.exec_calls[0]
            self.assertEqual(command[:4], ("docker", "exec", "--workdir", "/workspace"))
            self.assertIn(("--user", "65532:65532"), tuple(zip(command, command[1:])))
            marker = command.index("qykw-test-session")
            self.assertEqual(command[marker + 1 : marker + 3], ("env", "-i"))
            self.assertIn("PYTHONHASHSEED=0", command)
            self.assertNotIn(os.environ.get("PATH", "host-path-never-present"), command)
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.output_excerpt, "ok")

    def test_session_starts_once_and_reuses_tmp_until_close(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            workspace = self._workspace(root)
            backend = _Backend([_Process(), _Process()])
            executor = DockerSandboxExecutor(
                workspace,
                _IMAGE,
                backend=backend,
                container_name_factory=lambda: "qykw-persistent",
            )
            for argv in (("python", "first.py"), ("python", "second.py")):
                executor.run(
                    argv,
                    cwd=workspace,
                    env=(),
                    timeout_seconds=10,
                    output_limit_bytes=4096,
                )
            executor.close()

            self.assertEqual(sum(call[:2] == ("docker", "run") for call in backend.control_calls), 1)
            self.assertEqual(len(backend.exec_calls), 2)
            self.assertEqual(
                [call[call.index("env") - 1] for call in backend.exec_calls],
                ["qykw-persistent", "qykw-persistent"],
            )
            self.assertIn(("docker", "kill", "qykw-persistent"), backend.control_calls)
            self.assertIn(("docker", "rm", "--force", "qykw-persistent"), backend.control_calls)

    def test_rejects_untrusted_cwd_argv_and_environment(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            workspace = self._workspace(root)
            executor = DockerSandboxExecutor(workspace, _IMAGE, backend=_Backend())
            cases = (
                (("python",), Path(root), (), "invalid_sandbox_cwd"),
                (("sh", "-c", "id"), workspace, (), "shell_command_forbidden"),
                (("python\x00bad",), workspace, (), "invalid_command_argv"),
                (("python",), workspace, (("GITHUB_TOKEN", "x"),), "forbidden_environment_name"),
                (("python",), workspace, (("QYKW_MODE", "x"),), "forbidden_environment_name"),
                (("python",), workspace, (("SERVICE_KEY", "x"),), "forbidden_environment_name"),
                (("python",), workspace, (("LANG", "en_US"),), "reserved_environment_name"),
                (("python",), workspace, (("CUSTOM", "x"),), "untrusted_environment_name"),
            )
            for argv, cwd, env, message in cases:
                with self.subTest(message=message), self.assertRaisesRegex(SandboxError, message):
                    executor.run(
                        argv,
                        cwd=cwd,
                        env=env,
                        timeout_seconds=10,
                        output_limit_bytes=4096,
                    )

    def test_timeout_kills_session_and_blocks_later_commands(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            workspace = self._workspace(root)
            process = _Process(never_finishes=True)
            backend = _Backend([process])
            clock = _Clock()
            executor = DockerSandboxExecutor(
                workspace,
                _IMAGE,
                backend=backend,
                container_name_factory=lambda: "qykw-timeout",
                monotonic=clock.monotonic,
                sleep=clock.sleep,
            )
            with self.assertRaisesRegex(SandboxError, "sandbox_command_timeout"):
                executor.run(
                    ("python", "slow.py"),
                    cwd=workspace,
                    env=(),
                    timeout_seconds=1,
                    output_limit_bytes=4096,
                )
            self.assertTrue(process.killed)
            self.assertIn(("docker", "kill", "qykw-timeout"), backend.control_calls)
            with self.assertRaisesRegex(SandboxError, "sandbox_command_timeout"):
                executor.run(
                    ("python", "later.py"),
                    cwd=workspace,
                    env=(),
                    timeout_seconds=1,
                    output_limit_bytes=4096,
                )
            self.assertEqual(len(backend.exec_calls), 1)

    def test_output_limit_kills_session_and_blocks_later_commands(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            workspace = self._workspace(root)
            process = _Process(b"x" * 5000)
            backend = _Backend([process])
            executor = DockerSandboxExecutor(
                workspace,
                _IMAGE,
                backend=backend,
                container_name_factory=lambda: "qykw-output-limit",
            )
            with self.assertRaisesRegex(SandboxError, "sandbox_output_limit_exceeded"):
                executor.run(
                    ("python", "noisy.py"),
                    cwd=workspace,
                    env=(),
                    timeout_seconds=10,
                    output_limit_bytes=1024,
                )
            self.assertIn(("docker", "kill", "qykw-output-limit"), backend.control_calls)
            with self.assertRaisesRegex(SandboxError, "sandbox_output_limit_exceeded"):
                executor.run(
                    ("python", "later.py"),
                    cwd=workspace,
                    env=(),
                    timeout_seconds=10,
                    output_limit_bytes=1024,
                )
            self.assertEqual(len(backend.exec_calls), 1)

    def test_output_digest_is_complete_but_excerpt_is_safe_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            workspace = self._workspace(root)
            secret = "super-secret-value"
            source = "print('private source text')"
            raw = (
                f"{workspace / 'server.py'}\n{secret}\n{source}\n"
                "ValueError: should not expose this message\n"
                "Ran 123 tests in 9.99s\nFAILED (failures=2, errors=1)\n"
            ).encode()
            backend = _Backend([_Process(raw)])
            executor = DockerSandboxExecutor(workspace, _IMAGE, backend=backend)
            result = executor.run(
                ("python", "tests.py"),
                cwd=workspace,
                env=(("PYTHONWARNINGS", secret),),
                timeout_seconds=10,
                output_limit_bytes=65536,
            )
            self.assertEqual(result.output_digest, hashlib.sha256(raw).hexdigest())
            self.assertLessEqual(len(result.output_excerpt.encode("utf-8")), 2048)
            self.assertEqual(result.output_excerpt, "failed")
            for leaked in (secret, source, str(workspace), "should not expose"):
                self.assertNotIn(leaked, result.output_excerpt)

    def test_unknown_error_identifier_cannot_encode_a_sentinel(self) -> None:
        sentinel = "X73656e74696e656cError"
        raw = f"{sentinel}: private\nUnknownPayloadException: private\n".encode()
        with tempfile.TemporaryDirectory() as root:
            workspace = self._workspace(root)
            result = DockerSandboxExecutor(
                workspace, _IMAGE, backend=_Backend([_Process(raw)])
            ).run(
                ("python", "tests.py"),
                cwd=workspace,
                env=(),
                timeout_seconds=10,
                output_limit_bytes=4096,
            )
        self.assertEqual(result.output_excerpt, "error")
        self.assertNotIn("73656e74696e656c", result.output_excerpt)

    def test_binary_output_cannot_break_excerpt_or_digest(self) -> None:
        raw = b"\xff\xfe\x00\x80Ran 1 test in 0.1s\nOK\n"
        with tempfile.TemporaryDirectory() as root:
            workspace = self._workspace(root)
            result = DockerSandboxExecutor(
                workspace, _IMAGE, backend=_Backend([_Process(raw)])
            ).run(
                ("python", "tests.py"),
                cwd=workspace,
                env=(),
                timeout_seconds=10,
                output_limit_bytes=4096,
            )
            self.assertEqual(result.output_digest, hashlib.sha256(raw).hexdigest())
            self.assertEqual(result.output_excerpt, "ok")

    def test_nonzero_exit_returns_result_and_cleans_container(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            workspace = self._workspace(root)
            backend = _Backend([_Process(b"SyntaxError: private details\n", returncode=2)])
            executor = DockerSandboxExecutor(
                workspace,
                _IMAGE,
                backend=backend,
                container_name_factory=lambda: "qykw-failed",
            )
            result = executor.run(
                ("python", "bad.py"),
                cwd=workspace,
                env=(),
                timeout_seconds=10,
                output_limit_bytes=4096,
            )
            self.assertEqual(result.exit_code, 2)
            self.assertEqual(result.output_excerpt, "error")
            self.assertIn(("docker", "kill", "qykw-failed"), backend.control_calls)
            self.assertIn(("docker", "rm", "--force", "qykw-failed"), backend.control_calls)

    def test_excerpt_cannot_encode_unbounded_candidate_numbers(self) -> None:
        sentinel = "7" * 10000
        raw = (
            f"Ran {sentinel} tests in 1.0s\n"
            f"FAILED (failures={sentinel}, errors={sentinel})\n"
        ).encode()
        with tempfile.TemporaryDirectory() as root:
            workspace = self._workspace(root)
            result = DockerSandboxExecutor(
                workspace, _IMAGE, backend=_Backend([_Process(raw)])
            ).run(
                ("python", "tests.py"),
                cwd=workspace,
                env=(),
                timeout_seconds=10,
                output_limit_bytes=len(raw) + 1,
            )
        self.assertEqual(result.output_excerpt, "failed")
        self.assertNotRegex(result.output_excerpt, r"\d")
        self.assertNotIn(sentinel, result.output_excerpt)

        with tempfile.TemporaryDirectory() as root:
            workspace = self._workspace(root)
            benign = DockerSandboxExecutor(
                workspace,
                _IMAGE,
                backend=_Backend([_Process(b"candidate arbitrary text 123456\n")]),
            ).run(
                ("python", "tests.py"),
                cwd=workspace,
                env=(),
                timeout_seconds=10,
                output_limit_bytes=4096,
            )
        self.assertEqual("", benign.output_excerpt)

    def test_context_manager_cleans_container_when_caller_raises(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            workspace = self._workspace(root)
            backend = _Backend([_Process()])
            with self.assertRaisesRegex(RuntimeError, "caller failed"):
                with DockerSandboxExecutor(
                    workspace,
                    _IMAGE,
                    backend=backend,
                    container_name_factory=lambda: "qykw-context",
                ) as executor:
                    executor.run(
                        ("python", "ok.py"),
                        cwd=workspace,
                        env=(),
                        timeout_seconds=10,
                        output_limit_bytes=4096,
                    )
                    raise RuntimeError("caller failed")
            self.assertIn(("docker", "kill", "qykw-context"), backend.control_calls)
            self.assertIn(("docker", "rm", "--force", "qykw-context"), backend.control_calls)

    def test_cleanup_requires_kill_remove_and_bounded_absence_confirmation(self) -> None:
        class CleanupBackend(_Backend):
            def __init__(self, failing_prefix: tuple[str, ...]) -> None:
                super().__init__([_Process()])
                self.failing_prefix = failing_prefix

            def control(self, argv: tuple[str, ...], timeout_seconds: int) -> _Completed:
                result = super().control(argv, timeout_seconds)
                if argv[: len(self.failing_prefix)] == self.failing_prefix:
                    result.returncode = 1
                return result

        failures = (
            ("docker", "kill"),
            ("docker", "rm", "--force"),
            ("docker", "ps", "--all"),
        )
        for failure in failures:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as root:
                workspace = self._workspace(root)
                backend = CleanupBackend(failure)
                executor = DockerSandboxExecutor(
                    workspace,
                    _IMAGE,
                    backend=backend,
                    container_name_factory=lambda: "qykw-cleanup-check",
                )
                executor.run(
                    ("python", "tests.py"),
                    cwd=workspace,
                    env=(),
                    timeout_seconds=10,
                    output_limit_bytes=4096,
                )
                with self.assertRaisesRegex(SandboxError, "sandbox_cleanup_unconfirmed"):
                    executor.close()
                with self.assertRaisesRegex(SandboxError, "sandbox_cleanup_unconfirmed"):
                    executor.run(
                        ("python", "later.py"),
                        cwd=workspace,
                        env=(),
                        timeout_seconds=10,
                        output_limit_bytes=4096,
                    )
                self.assertIn(
                    (
                        "docker",
                        "ps",
                        "--all",
                        "--quiet",
                        "--no-trunc",
                        "--filter",
                        "name=^/qykw-cleanup-check$",
                    ),
                    backend.control_calls,
                )

    def test_timeout_cleanup_failure_overrides_timeout_success_claim(self) -> None:
        class FailedRemoveBackend(_Backend):
            def control(self, argv: tuple[str, ...], timeout_seconds: int) -> _Completed:
                result = super().control(argv, timeout_seconds)
                if argv[:3] == ("docker", "rm", "--force"):
                    result.returncode = 1
                return result

        with tempfile.TemporaryDirectory() as root:
            workspace = self._workspace(root)
            backend = FailedRemoveBackend([_Process(never_finishes=True)])
            clock = _Clock()
            executor = DockerSandboxExecutor(
                workspace,
                _IMAGE,
                backend=backend,
                container_name_factory=lambda: "qykw-timeout-cleanup",
                monotonic=clock.monotonic,
                sleep=clock.sleep,
            )
            with self.assertRaisesRegex(SandboxError, "sandbox_cleanup_unconfirmed"):
                executor.run(
                    ("python", "slow.py"),
                    cwd=workspace,
                    env=(),
                    timeout_seconds=1,
                    output_limit_bytes=4096,
                )

    def test_default_backend_never_invokes_a_shell(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            workspace = self._workspace(root)

            def completed(argv, **kwargs):
                del kwargs
                return _Completed(stdout=b"" if argv[:3] == ("docker", "ps", "--all") else b"container-id\n")

            with (
                patch("tools.qykw.sandbox.subprocess.run", side_effect=completed) as run,
                patch(
                    "tools.qykw.sandbox.subprocess.Popen",
                    return_value=_Process(b"Ran 1 test in 0.1s\nOK\n"),
                ) as popen,
            ):
                with DockerSandboxExecutor(workspace, _IMAGE) as executor:
                    executor.run(
                        ("python", "tests.py"),
                        cwd=workspace,
                        env={"PYTHONHASHSEED": "0"},
                        timeout_seconds=10,
                        output_limit_bytes=4096,
                    )
            self.assertTrue(run.call_args_list)
            self.assertTrue(all(call.kwargs["shell"] is False for call in run.call_args_list))
            self.assertIs(popen.call_args.kwargs["shell"], False)

    def test_start_and_stream_failures_are_stable_and_cleaned(self) -> None:
        class FailingStart(_Backend):
            def control(self, argv: tuple[str, ...], timeout_seconds: int) -> _Completed:
                result = super().control(argv, timeout_seconds)
                if argv[:2] == ("docker", "run"):
                    result.returncode = 125
                return result

        class RaisingStart(_Backend):
            def control(self, argv: tuple[str, ...], timeout_seconds: int) -> _Completed:
                self.control_calls.append(argv)
                if argv[:2] == ("docker", "run"):
                    raise OSError("private daemon detail")
                if argv[:3] == ("docker", "rm", "--force"):
                    raise OSError("private cleanup detail")
                return _Completed()

        with tempfile.TemporaryDirectory() as root:
            workspace = self._workspace(root)
            for backend, expected in (
                (FailingStart(), "sandbox_start_failed"),
                (RaisingStart(), "sandbox_cleanup_unconfirmed"),
            ):
                with self.subTest(backend=type(backend).__name__):
                    executor = DockerSandboxExecutor(workspace, _IMAGE, backend=backend)
                    with self.assertRaisesRegex(SandboxError, expected) as caught:
                        executor.run(
                            ("python", "tests.py"),
                            cwd=workspace,
                            env=(),
                            timeout_seconds=10,
                            output_limit_bytes=4096,
                        )
                    self.assertNotIn("private", str(caught.exception))

            no_stdout = _Process()
            no_stdout.stdout = None
            executor = DockerSandboxExecutor(
                workspace, _IMAGE, backend=_Backend([no_stdout])
            )
            with self.assertRaisesRegex(SandboxError, "sandbox_stream_unavailable"):
                executor.run(
                    ("python", "tests.py"),
                    cwd=workspace,
                    env=(),
                    timeout_seconds=10,
                    output_limit_bytes=4096,
                )

            class RaisingStream(_Backend):
                def stream(self, argv: tuple[str, ...]) -> _Process:
                    self.exec_calls.append(argv)
                    raise OSError("private stream detail")

            executor = DockerSandboxExecutor(
                workspace, _IMAGE, backend=RaisingStream()
            )
            with self.assertRaisesRegex(SandboxError, "sandbox_execution_failed"):
                executor.run(
                    ("python", "tests.py"),
                    cwd=workspace,
                    env=(),
                    timeout_seconds=10,
                    output_limit_bytes=4096,
                )

    def test_invalid_stream_values_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            workspace = self._workspace(root)
            for value in ("not-bytes", OSError("private stream detail")):
                process = _Process()
                process.stdout = _BrokenStream(value)
                executor = DockerSandboxExecutor(
                    workspace, _IMAGE, backend=_Backend([process])
                )
                with self.subTest(value=type(value).__name__), self.assertRaisesRegex(
                    SandboxError, "sandbox_output_stream_failed"
                ):
                    executor.run(
                        ("python", "tests.py"),
                        cwd=workspace,
                        env=(),
                        timeout_seconds=10,
                        output_limit_bytes=4096,
                    )

    def test_input_capacity_and_shape_errors_fail_before_docker(self) -> None:
        class BrokenEnvironment:
            def __iter__(self):
                raise TypeError("private")

        with tempfile.TemporaryDirectory() as root:
            workspace = self._workspace(root)
            backend = _Backend()
            executor = DockerSandboxExecutor(workspace, _IMAGE, backend=backend)
            cases = (
                (("python",), (), 0, 100, "invalid_command_timeout"),
                (("python",), (), 901, 100, "invalid_command_timeout"),
                (("python",), (), 10, 0, "invalid_output_limit"),
                (("python",), (), 10, 1024 * 1024 + 1, "invalid_output_limit"),
                (("python",), BrokenEnvironment(), 10, 100, "invalid_environment"),
                (("python",), (("PYTHONPATH",),), 10, 100, "invalid_environment"),
                (("python",), (("PYTHONPATH", 1),), 10, 100, "invalid_environment"),
                (("python",), (("pythonpath", "x"),), 10, 100, "invalid_environment_name"),
                (
                    ("python",),
                    (("PYTHONWARNINGS", "a"), ("PYTHONWARNINGS", "b")),
                    10,
                    100,
                    "invalid_environment",
                ),
                (("python",), (("PYTHONWARNINGS", "x\n"),), 10, 100, "invalid_environment"),
                (("python",), (("PYTHONPATH", "x"),), 10, 100, "forbidden_environment_name"),
                (("python",), (("PYTHONHOME", "x"),), 10, 100, "forbidden_environment_name"),
                (("python",), (("PYTHONSTARTUP", "x"),), 10, 100, "forbidden_environment_name"),
                (("python",), (("PYTHONINSPECT", "1"),), 10, 100, "forbidden_environment_name"),
                (("python",), (("PYTHONARBITRARY", "x"),), 10, 100, "untrusted_environment_name"),
            )
            for argv, env, timeout, limit, code in cases:
                with self.subTest(code=code), self.assertRaisesRegex(SandboxError, code):
                    executor.run(
                        argv,
                        cwd=workspace,
                        env=env,
                        timeout_seconds=timeout,
                        output_limit_bytes=limit,
                    )
            self.assertEqual(backend.control_calls, [])
            executor.close()
            executor.close()
            with self.assertRaisesRegex(SandboxError, "sandbox_session_closed"):
                executor.run(
                    ("python",),
                    cwd=workspace,
                    env=(),
                    timeout_seconds=10,
                    output_limit_bytes=100,
                )

    def test_invalid_workspace_and_container_names_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            missing = Path(root, "missing")
            with self.assertRaisesRegex(SandboxError, "invalid_workspace_root"):
                DockerSandboxExecutor(missing, _IMAGE)
            file_root = Path(root, "file")
            file_root.write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(SandboxError, "invalid_workspace_root"):
                DockerSandboxExecutor(file_root, _IMAGE)
            comma_root = Path(root, "comma,path")
            comma_root.mkdir()
            with self.assertRaisesRegex(SandboxError, "invalid_workspace_root"):
                DockerSandboxExecutor(comma_root, _IMAGE)
            workspace = self._workspace(root)
            with self.assertRaisesRegex(SandboxError, "invalid_container_name"):
                DockerSandboxExecutor(
                    workspace,
                    _IMAGE,
                    container_name_factory=lambda: "../../bad",
                )


class TestVerificationDockerfile(unittest.TestCase):
    def test_supply_chain_and_runtime_are_pinned_and_minimal(self) -> None:
        dockerfile = Path("tools/qykw/verify.Dockerfile").read_text(encoding="utf-8")
        from_lines = [line for line in dockerfile.splitlines() if line.startswith("FROM ")]
        self.assertEqual(len(from_lines), 2)
        expected = {
            "node:24.7.0-bookworm-slim@sha256:0104d9447ea3ddf7373643be7f9915fc7b7c896e41d0d33229338e457217cd78",
            "python:3.11.13-slim-bookworm@sha256:86adf8dbadc3d6e82ee5dd2c74bec2e1c2467cdad47886280501df722372d2e1",
        }
        references = {line.split()[1] for line in from_lines}
        self.assertEqual(references, expected)
        self.assertTrue(all(re.search(r"@sha256:[0-9a-f]{64}$", ref) for ref in references))
        lowered = dockerfile.lower()
        self.assertIn("snapshot.debian.org/archive/debian/20251020t000000z", lowered)
        self.assertIn("git=1:2.39.5-0+deb12u2", dockerfile)
        self.assertIn("--no-install-recommends", dockerfile)
        self.assertNotIn("latest", lowered)
        self.assertNotRegex(lowered, r"curl\s.*\|\s*(?:ba)?sh")
        self.assertNotRegex(lowered, r"\b(?:pip|npm|yarn|pnpm)\s+install\b")
        self.assertIn(
            "LABEL org.opencontainers.image.source=https://github.com/qiyuankaiwu/Challenge-Cup",
            dockerfile,
        )
        copy_lines = [line for line in dockerfile.splitlines() if line.startswith("COPY ")]
        self.assertTrue(any("--from=node_runtime" in line for line in copy_lines))
        self.assertIn("COPY tools/qykw/verify_smoke.py /opt/qykw/verify_smoke.py", copy_lines)
        self.assertFalse(any(re.match(r"COPY\s+\.\s", line) for line in copy_lines))
        self.assertRegex(dockerfile, r"(?m)^USER 65532:65532$")


class TestVerifySmoke(unittest.TestCase):
    def _write_server(self, root: Path, body: bytes, log: str) -> None:
        encoded = repr(body)
        source = f"""
            from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
            import sys

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass
                def do_GET(self):
                    payload = {encoded}
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

            print({log!r}, flush=True)
            ThreadingHTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()
        """
        (root / "server.py").write_text(textwrap.dedent(source), encoding="utf-8")

    def _run_helper(self, workspace: Path) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [
                sys.executable,
                "tools/qykw/verify_smoke.py",
                str(workspace),
                "8765",
            ],
            cwd=Path.cwd(),
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )

    def _write_redirect_server(self, root: Path) -> None:
        source = """
            from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
            import sys

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass
                def do_GET(self):
                    self.send_response(302)
                    self.send_header('Location', 'http://127.0.0.1:8765/target')
                    self.end_headers()

            ThreadingHTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()
        """
        (root / "server.py").write_text(textwrap.dedent(source), encoding="utf-8")

    @unittest.skipIf(os.name == "nt", "trusted verification image is Linux")
    def test_smoke_accepts_doctype_without_echoing_server_logs(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            self._write_server(workspace, b"<!DOCTYPE html><title>ok</title>", "private-server-log")
            result = self._run_helper(workspace)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            self.assertNotIn(b"private-server-log", result.stdout + result.stderr)

    @unittest.skipIf(os.name == "nt", "trusted verification image is Linux")
    def test_smoke_rejects_missing_doctype_with_stable_safe_error(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            self._write_server(workspace, b"<html>no doctype</html>", "sensitive-log")
            result = self._run_helper(workspace)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, b"")
            self.assertEqual(result.stderr.splitlines(), [b"smoke_invalid_response"])
            self.assertNotIn(b"sensitive-log", result.stdout + result.stderr)

    def test_direct_smoke_success_and_response_boundaries(self) -> None:
        platform_env = {"SYSTEMROOT": r"C:\Windows"} if os.name == "nt" else {}
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root:
            workspace = Path(root)
            self._write_server(workspace, b"  <!DOCTYPE html><title>ok</title>", "private")
            with patch.dict(verify_smoke._CLEAN_ENV, platform_env):
                verify_smoke.verify(workspace)

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root:
            workspace = Path(root)
            self._write_server(workspace, b"<html>bad</html>", "private")
            with patch.dict(verify_smoke._CLEAN_ENV, platform_env):
                with self.assertRaisesRegex(RuntimeError, "smoke_invalid_response"):
                    verify_smoke.verify(workspace)

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root:
            workspace = Path(root)
            self._write_server(workspace, b"x" * (64 * 1024 + 1), "private")
            with patch.dict(verify_smoke._CLEAN_ENV, platform_env):
                with self.assertRaisesRegex(RuntimeError, "smoke_response_too_large"):
                    verify_smoke.verify(workspace)

    def test_direct_smoke_rejects_bad_workspace_and_early_exit(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            with self.assertRaisesRegex(RuntimeError, "smoke_invalid_workspace"):
                verify_smoke.verify(workspace)
            (workspace / "server.py").write_text("raise SystemExit(7)\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "smoke_server_exited"):
                verify_smoke.verify(workspace)
            with self.assertRaisesRegex(RuntimeError, "smoke_invalid_workspace"):
                verify_smoke.verify(workspace, 8000)

    def test_direct_smoke_rejects_redirects_without_following_them(self) -> None:
        platform_env = {"SYSTEMROOT": r"C:\Windows"} if os.name == "nt" else {}
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root:
            workspace = Path(root)
            self._write_redirect_server(workspace)
            with patch.dict(verify_smoke._CLEAN_ENV, platform_env):
                with self.assertRaisesRegex(RuntimeError, "smoke_redirect_forbidden"):
                    verify_smoke.verify(workspace)

    def test_smoke_main_has_stable_argument_and_runtime_errors(self) -> None:
        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            self.assertEqual(verify_smoke.main([]), 2)
            self.assertEqual(verify_smoke.main(["workspace", "not-a-port"]), 2)
            self.assertEqual(verify_smoke.main(["missing", "8765"]), 1)
        self.assertEqual(
            stderr.getvalue().splitlines(),
            [
                "smoke_invalid_arguments",
                "smoke_invalid_arguments",
                "smoke_invalid_workspace",
            ],
        )


if __name__ == "__main__":
    unittest.main()
