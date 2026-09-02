"""Trusted, controller-selected verification command profiles."""

from __future__ import annotations

from dataclasses import dataclass


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
