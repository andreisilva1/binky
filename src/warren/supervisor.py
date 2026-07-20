"""Minimal in-daemon process supervision (thin — Phase 3.2).

Holds the running processes of each worktree, keyed by worktree id, for the
daemon's lifetime. This is the *thin* version: plain child processes, stopped by
``terminate``. Detached process groups, PID persistence, health and restart land
in Phase 5; reconciliation/GC in Phase 6.
"""

from __future__ import annotations

import os
import subprocess


class ProcessManager:
    def __init__(self) -> None:
        self._procs: dict[int, list[subprocess.Popen]] = {}

    def is_running(self, worktree_id: int) -> bool:
        return any(p.poll() is None for p in self._procs.get(worktree_id, []))

    def start(self, worktree_id: int, command: str, cwd: str, env: dict[str, str]) -> int:
        proc = subprocess.Popen(command, cwd=cwd, env={**os.environ, **env}, shell=True)
        self._procs.setdefault(worktree_id, []).append(proc)
        return proc.pid

    def stop(self, worktree_id: int) -> int:
        procs = self._procs.pop(worktree_id, [])
        alive = [p for p in procs if p.poll() is None]
        for proc in alive:
            proc.terminate()
        for proc in alive:
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        return len(alive)

    def stop_all(self) -> None:
        for worktree_id in list(self._procs):
            self.stop(worktree_id)
