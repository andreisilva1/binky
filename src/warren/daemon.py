"""The always-alive Warren daemon (THESIS §8.4, §8.7).

An asyncio server that owns the machine-global state and answers CLI requests.
It is spawned lazily and detached, so it outlives the CLI process; it writes its
endpoint + token to ``daemon.json`` when ready and clears it on shutdown.

This part (2.2) sets up the process, transport, and lifecycle with two built-in
methods (``ping``, ``shutdown``). The real RPC surface lands in 2.3.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import signal
import subprocess
import sys
import time

from warren import ipc, ops, paths, rpc
from warren.registry import Registry
from warren.supervisor import ProcessManager


class Daemon:
    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self._token = secrets.token_hex(16)
        self.registry = Registry.connect()
        self.procman = ProcessManager()
        self.router = rpc.Router()
        self.router.add("ping", self._ping)
        self.router.add("shutdown", self._shutdown)
        self.router.add("methods", self._list_methods)
        self.router.add("version", self._version)
        self.router.add("up", self._up)
        self.router.add("down", self._down)
        self.router.add("status", self._status)

    async def _ping(self, _params: dict) -> dict:
        return {"pong": True, "pid": os.getpid()}

    async def _shutdown(self, _params: dict) -> dict:
        self._stop.set()
        return {"stopping": True}

    async def _list_methods(self, _params: dict) -> dict:
        return {"methods": self.router.names()}

    async def _version(self, _params: dict) -> dict:
        from warren import __version__

        return {"version": __version__}

    async def _up(self, params: dict) -> dict:
        return ops.up(self.registry, self.procman, params)

    async def _down(self, params: dict) -> dict:
        return ops.down(self.registry, self.procman, params)

    async def _status(self, params: dict) -> dict:
        return ops.status(self.registry, self.procman, params)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            if not line:
                return
            req = json.loads(line.decode())
            if req.get("token") != self._token:
                resp = {"ok": False, "error": {"code": "unauthorized", "message": "bad token"}}
            else:
                resp = await self.router.dispatch(req.get("method"), req.get("params") or {})
            writer.write((json.dumps(resp) + "\n").encode())
            await writer.drain()
        except (json.JSONDecodeError, ConnectionError):
            pass
        finally:
            writer.close()

    async def serve(self) -> None:
        if ipc.USE_UNIX:
            sock_path = paths.socket_path()
            sock_path.parent.mkdir(parents=True, exist_ok=True)
            _unlink(sock_path)
            server = await asyncio.start_unix_server(self._handle, path=str(sock_path))
            info = ipc.DaemonInfo(
                transport="unix", token=self._token, pid=os.getpid(), path=str(sock_path)
            )
        else:
            server = await asyncio.start_server(self._handle, host="127.0.0.1", port=0)
            port = server.sockets[0].getsockname()[1]
            info = ipc.DaemonInfo(
                transport="tcp", token=self._token, pid=os.getpid(), host="127.0.0.1", port=port
            )

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            # signals aren't settable on some platforms (e.g. Windows)
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.add_signal_handler(sig, self._stop.set)

        ipc.write_daemon_info(info)
        try:
            async with server:
                await self._stop.wait()
        finally:
            self.procman.stop_all()
            self.registry.close()
            ipc.clear_daemon_info()
            if ipc.USE_UNIX:
                _unlink(paths.socket_path())


def _unlink(path) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)


def run() -> None:
    """Foreground serve — the entry point for the spawned daemon process."""
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(Daemon().serve())


# --------------------------------------------------------------------------- #
#  lifecycle (called from the CLI process)
# --------------------------------------------------------------------------- #
def is_running() -> bool:
    if ipc.read_daemon_info() is None:
        return False
    try:
        ipc.call("ping", timeout=1.0)
        return True
    except (ipc.DaemonNotRunning, ipc.RpcError, OSError):
        return False


def start(timeout: float = 10.0) -> None:
    """Spawn the daemon detached and wait until it is reachable. Idempotent."""
    if is_running():
        return
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, "-m", "warren", "daemon", "serve"], **kwargs)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_running():
            return
        time.sleep(0.05)
    raise RuntimeError("daemon did not become ready in time")


def stop(timeout: float = 5.0) -> bool:
    """Ask the daemon to shut down. Returns True if it was running."""
    if not is_running():
        return False
    with contextlib.suppress(ipc.DaemonNotRunning, ipc.RpcError, OSError):
        ipc.call("shutdown", timeout=2.0)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_running():
            return True
        time.sleep(0.05)
    return True


def ensure() -> None:
    """Start the daemon if it isn't already running (lazy auto-start)."""
    start()
