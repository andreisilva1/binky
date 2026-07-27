"""CLI <-> daemon transport.

One request/response per connection, newline-delimited JSON, with a shared-secret
token. Transport is chosen by OS: a Unix-domain socket on posix, a loopback TCP
port on Windows (asyncio has no Unix sockets there). The client here is
synchronous — a one-shot control call; the server lives in :mod:`binky.daemon`.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
from dataclasses import asdict, dataclass

from binky import paths

USE_UNIX = os.name != "nt"


class DaemonNotRunning(Exception):
    """The daemon could not be reached."""


class RpcError(Exception):
    """The daemon returned a structured error response."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass
class DaemonInfo:
    transport: str  # "unix" | "tcp"
    token: str
    pid: int
    path: str | None = None  # unix socket path
    host: str | None = None  # tcp host
    port: int | None = None  # tcp port


def write_daemon_info(info: DaemonInfo) -> None:
    p = paths.daemon_info_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(asdict(info)), encoding="utf-8")
    # owner-only: on a loopback transport the token in here is the only guard
    with contextlib.suppress(OSError):
        os.chmod(p, 0o600)


def read_daemon_info() -> DaemonInfo | None:
    p = paths.daemon_info_path()
    if not p.exists():
        return None
    try:
        return DaemonInfo(**json.loads(p.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError, TypeError):
        return None


def clear_daemon_info() -> None:
    """Remove the endpoint file — but only if it is still ours.

    Shutting down can take seconds, which is long enough for a replacement daemon
    to have started and written its own endpoint. Deleting that one would leave a
    daemon running that nothing can find, and the symptom (``binky`` says the
    daemon is not running, ``ps`` says it is) points nowhere near here. A file we
    cannot read is a leftover either way, so it goes.
    """
    info = read_daemon_info()
    if info is not None and info.pid != os.getpid():
        return
    paths.daemon_info_path().unlink(missing_ok=True)


def _connect(info: DaemonInfo, timeout: float) -> socket.socket:
    if info.transport == "unix":
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(info.path)
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((info.host, info.port))
    return sock


def _recv_line(sock: socket.socket) -> dict:
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    if not buf:
        raise RpcError("connection", "daemon closed the connection without responding")
    try:
        return json.loads(buf.decode())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # a truncated or garbled line — the daemon spoke, but not something we can trust
        raise RpcError("malformed", "daemon returned an unreadable response") from exc


def call(method: str, params: dict | None = None, *, timeout: float = 5.0) -> dict:
    """Send one request to the daemon and return its ``result``.

    Raises :class:`DaemonNotRunning` if unreachable, :class:`RpcError` on error.
    """
    info = read_daemon_info()
    if info is None:
        raise DaemonNotRunning("daemon is not running")
    try:
        sock = _connect(info, timeout)
    except OSError as exc:
        raise DaemonNotRunning(str(exc)) from exc
    try:
        request = {"token": info.token, "method": method, "params": params or {}}
        sock.sendall((json.dumps(request) + "\n").encode())
        resp = _recv_line(sock)
    except TimeoutError as exc:
        # Connected, but no answer within `timeout`s — the daemon is *busy*, not gone (a long
        # `up` still provisioning + health-checking, say). Route this through RpcError, which
        # every CLI command already renders as a clean "✗ …" line, instead of DaemonNotRunning
        # (which no command catches → a raw traceback for the user). The daemon-side call keeps
        # running and converges on its own; `binky status` shows where it landed.
        raise RpcError(
            "timeout",
            f"'{method}' is taking longer than {timeout:g}s — the daemon is still working "
            f"on it. Check `binky status` in a moment.",
        ) from exc
    except OSError as exc:
        # a reset *after* connecting (the daemon died mid-call) is still "unreachable" —
        # surface it as such, per this function's contract, instead of leaking a raw OSError
        # past callers that only guard against DaemonNotRunning / RpcError.
        raise DaemonNotRunning(str(exc)) from exc
    finally:
        sock.close()
    if not resp.get("ok"):
        err = resp.get("error")
        if isinstance(err, dict):
            raise RpcError(err.get("code", "error"), err.get("message", "unknown error"))
        raise RpcError("error", str(err))
    return resp.get("result", {})
