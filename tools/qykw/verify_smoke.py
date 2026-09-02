"""Trusted loopback-only smoke check for the candidate demo server."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request


_PORT = 8765
_DEADLINE_SECONDS = 8.0
_RESPONSE_LIMIT_BYTES = 64 * 1024
_CLEAN_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/tmp",
    "TMPDIR": "/tmp",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        del request, file_pointer, code, message, headers, new_url
        raise RuntimeError("smoke_redirect_forbidden")


_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    _NoRedirect(),
)


def verify(workspace: Path, port: int = _PORT) -> None:
    """Start ``server.py`` and require a bounded HTML DOCTYPE response."""

    try:
        root = workspace.resolve(strict=True)
    except (OSError, RuntimeError):
        raise RuntimeError("smoke_invalid_workspace") from None
    server = root / "server.py"
    if port != _PORT or not root.is_dir() or not server.is_file() or server.is_symlink():
        raise RuntimeError("smoke_invalid_workspace")

    process = subprocess.Popen(
        (sys.executable, str(server), str(port)),
        cwd=root,
        env=_CLEAN_ENV,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    deadline = time.monotonic() + _DEADLINE_SECONDS
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("smoke_server_exited")
            remaining = max(0.05, min(0.5, deadline - time.monotonic()))
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/",
                    method="GET",
                    headers={"Connection": "close"},
                )
                with _OPENER.open(request, timeout=remaining) as response:
                    if response.geturl() != f"http://127.0.0.1:{port}/":
                        raise RuntimeError("smoke_redirect_forbidden")
                    body = response.read(_RESPONSE_LIMIT_BYTES + 1)
                if len(body) > _RESPONSE_LIMIT_BYTES:
                    raise RuntimeError("smoke_response_too_large")
                if not body.lstrip().lower().startswith(b"<!doctype html"):
                    raise RuntimeError("smoke_invalid_response")
                return
            except RuntimeError:
                raise
            except (OSError, urllib.error.URLError):
                time.sleep(0.05)
        raise RuntimeError("smoke_deadline_exceeded")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)
        # Close the process handle as well as waiting; Windows otherwise keeps
        # the candidate working directory busy briefly after a fast smoke run.
        process.__exit__(None, None, None)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("smoke_invalid_arguments", file=sys.stderr)
        return 2
    try:
        port = int(args[1])
    except ValueError:
        print("smoke_invalid_arguments", file=sys.stderr)
        return 2
    try:
        verify(Path(args[0]), port)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
