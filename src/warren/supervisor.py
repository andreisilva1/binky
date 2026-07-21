"""In-daemon process supervision.

Holds each worktree's running processes for the daemon's lifetime. Each service
runs in its own process group / session with stdout+stderr captured to a log
file, and is stopped by killing the whole process *tree* — services run via a
shell, so terminating just the shell would orphan the real process (and, on
Windows, keep the worktree directory locked so `remove` couldn't delete it).

Health checks and reconciliation live in ``health.py`` and ``ops.reconcile``.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import threading
from pathlib import Path
from typing import IO, TYPE_CHECKING

if TYPE_CHECKING:
    from warren.config import Limits


def _renice() -> None:  # child-side, Unix only
    with contextlib.suppress(OSError):
        os.nice(10)


def _new_group_kwargs() -> dict:
    if os.name == "nt":
        return {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.BELOW_NORMAL_PRIORITY_CLASS
        }
    return {"start_new_session": True, "preexec_fn": _renice}


def _wrap_limits(command: str, limits: Limits | None) -> str:
    """Opt-in hard limits on Linux via systemd-run cgroup scopes (no-op elsewhere)."""
    if limits is None or os.name == "nt" or not shutil.which("systemd-run"):
        return command
    args = ["systemd-run", "--user", "--scope", "--quiet"]
    if limits.memory:
        args += ["-p", f"MemoryMax={limits.memory}"]
    if limits.cpu:
        with contextlib.suppress(ValueError):
            args += ["-p", f"CPUQuota={int(float(limits.cpu) * 100)}%"]
    return " ".join(args) + " " + command


def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass


def kill_pid(pid: int) -> None:
    """Kill a process and its group/tree by pid — best effort (used by reconcile)."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, check=False
        )
    else:
        with contextlib.suppress(ProcessLookupError, OSError):
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError, OSError):
            os.kill(pid, signal.SIGKILL)


class ProcessManager:
    """Thread-safe: the daemon runs ``up``/``down`` on a worker pool, so several
    threads touch this at once. A lock guards the ``_procs`` map; the slow parts
    (kill + wait) run on local references *outside* the lock."""

    def __init__(self) -> None:
        self._procs: dict[int, list[tuple[subprocess.Popen, IO[bytes]]]] = {}
        self._lock = threading.Lock()

    def is_running(self, worktree_id: int) -> bool:
        with self._lock:
            entries = list(self._procs.get(worktree_id, []))
        return any(p.poll() is None for p, _ in entries)

    def start(
        self,
        worktree_id: int,
        command: str,
        cwd: str,
        env: dict[str, str],
        log_file: Path,
        limits: Limits | None = None,
    ) -> int:
        command = _wrap_limits(command, limits)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handle = open(log_file, "ab")  # noqa: SIM115 — closed in stop()
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            env={**os.environ, **env},
            shell=True,
            stdout=handle,
            stderr=subprocess.STDOUT,
            **_new_group_kwargs(),
        )
        with self._lock:
            self._procs.setdefault(worktree_id, []).append((proc, handle))
        return proc.pid

    def stop(self, worktree_id: int) -> int:
        with self._lock:
            entries = self._procs.pop(worktree_id, [])
        alive = [(p, h) for p, h in entries if p.poll() is None]
        for proc, _ in alive:
            _kill_tree(proc)
        for proc, _ in alive:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        for _, handle in entries:
            with contextlib.suppress(Exception):
                handle.close()
        return len(alive)

    def stop_all(self) -> None:
        with self._lock:
            ids = list(self._procs)
        for worktree_id in ids:
            self.stop(worktree_id)
