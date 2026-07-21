"""The always-alive Warren daemon.

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

from warren import admission, ipc, ops, paths, proxy, rpc
from warren.registry import Registry
from warren.supervisor import ProcessManager


class Daemon:
    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self._token = secrets.token_hex(16)
        self.registry = Registry.connect()
        self.procman = ProcessManager()
        self.admission = admission.make_default()
        self._slots: dict[tuple[str, str], int] = {}
        self.routes: dict[str, int] = {}
        self._routes_by_wt: dict[tuple[str, str], dict[str, int]] = {}
        self.proxy = proxy.Proxy(lambda host: self.routes.get(host))
        ops.reconcile(self.registry)  # clean up whatever a previous daemon left behind
        self.router = rpc.Router()
        self.router.add("ping", self._ping)
        self.router.add("shutdown", self._shutdown)
        self.router.add("methods", self._list_methods)
        self.router.add("version", self._version)
        self.router.add("up", self._up)
        self.router.add("down", self._down)
        self.router.add("remove", self._remove)
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
        with contextlib.suppress(Exception):  # apply the project's max_parallel override
            cfg = ops._load(params.get("root"))
            if cfg.project.max_parallel:
                self.admission.set_max_parallel(cfg.project.max_parallel)
        token = await self.admission.acquire()  # FIFO — waits for capacity
        try:
            result = ops.up(self.registry, self.procman, params)
        except BaseException:
            self.admission.release(token)
            raise
        self._slots[(result["project"], result["worktree"])] = token
        self._routes_by_wt[(result["project"], result["worktree"])] = result.get("routes", {})
        self._rebuild_routes()
        return result

    async def _down(self, params: dict) -> dict:
        result = ops.down(self.registry, self.procman, params)
        self._release_slot(result["project"], result["worktree"])
        return result

    async def _remove(self, params: dict) -> dict:
        result = ops.remove(self.registry, self.procman, params)
        self._release_slot(result["project"], result["worktree"])
        return result

    def _release_slot(self, project: str, worktree: str) -> None:
        token = self._slots.pop((project, worktree), None)
        if token is not None:
            self.admission.release(token)
        self._routes_by_wt.pop((project, worktree), None)
        self._rebuild_routes()

    def _rebuild_routes(self) -> None:
        self.routes = {
            host: port for routes in self._routes_by_wt.values() for host, port in routes.items()
        }

    async def _pump_loop(self) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            while not self._stop.is_set():
                await asyncio.sleep(1.0)
                self.admission.pump()

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

        proxy_port = int(os.environ.get("WARREN_PROXY_PORT", "80"))
        if proxy_port > 0:
            with contextlib.suppress(OSError):
                await self.proxy.start(proxy_port)
        pump_task = asyncio.create_task(self._pump_loop())
        ipc.write_daemon_info(info)
        try:
            async with server:
                await self._stop.wait()
        finally:
            with contextlib.suppress(Exception):
                await self.proxy.stop()
            pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump_task
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
