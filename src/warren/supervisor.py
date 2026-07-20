"""In-daemon process supervision (Phase 3.2, hardened in 4.2).

Holds each worktree's running processes for the daemon's lifetime and stops them
by killing the whole process *tree* — services run via a shell, so terminating
just the shell would orphan the real process (and, on Windows, keep the worktree
directory locked so `remove` couldn't delete it). Each service is started in its
own process group / session; stop kills the group.

Health, restart, PID persistence and reconciliation still land in Phases 5-6.
"""

from __future__ import annotations

import os
import signal
import subprocess


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


class ProcessManager:
    def __init__(self) -> None:
        self._procs: dict[int, list[subprocess.Popen]] = {}

    def is_running(self, worktree_id: int) -> bool:
        return any(p.poll() is None for p in self._procs.get(worktree_id, []))

    def start(self, worktree_id: int, command: str, cwd: str, env: dict[str, str]) -> int:
        proc = subprocess.Popen(
            command, cwd=cwd, env={**os.environ, **env}, shell=True, **_new_group_kwargs()
        )
        self._procs.setdefault(worktree_id, []).append(proc)
        return proc.pid

    def stop(self, worktree_id: int) -> int:
        procs = self._procs.pop(worktree_id, [])
        alive = [p for p in procs if p.poll() is None]
        for proc in alive:
            _kill_tree(proc)
        for proc in alive:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        return len(alive)

    def stop_all(self) -> None:
        for worktree_id in list(self._procs):
            self.stop(worktree_id)
