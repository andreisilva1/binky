"""In-daemon process supervision (Phase 3.2, hardened in 4.2, logs in 5.1).

Holds each worktree's running processes for the daemon's lifetime. Each service
runs in its own process group / session with stdout+stderr captured to a log
file, and is stopped by killing the whole process *tree* — services run via a
shell, so terminating just the shell would orphan the real process (and, on
Windows, keep the worktree directory locked so `remove` couldn't delete it).

Health, restart, PID persistence and reconciliation still land in Phases 5.2-6.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from pathlib import Path
from typing import IO


def _new_group_kwargs() -> dict:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


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
    def __init__(self) -> None:
        self._procs: dict[int, list[tuple[subprocess.Popen, IO[bytes]]]] = {}

    def is_running(self, worktree_id: int) -> bool:
        return any(p.poll() is None for p, _ in self._procs.get(worktree_id, []))

    def start(
        self, worktree_id: int, command: str, cwd: str, env: dict[str, str], log_file: Path
    ) -> int:
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
        self._procs.setdefault(worktree_id, []).append((proc, handle))
        return proc.pid

    def stop(self, worktree_id: int) -> int:
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
        for worktree_id in list(self._procs):
            self.stop(worktree_id)
