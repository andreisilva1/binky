"""In-daemon process supervision.

Holds each worktree's running processes for the daemon's lifetime and keeps them
honest *while they run*, not just at startup. Each service runs in its own process
group / session with stdout+stderr captured to a log file, and is stopped by killing
the whole process *tree* — services run via a shell, so terminating just the shell
would orphan the real process (and, on Windows, keep the worktree directory locked
so `remove` couldn't delete it).

`tick()` is the live half: called periodically by the daemon, it notices exits,
applies each service's `restart` policy (with backoff), runs continuous health
checks, and reports worktrees that have gone fully down so their ports/slots can be
reclaimed without a daemon restart. Startup convergence still lives in `ops.reconcile`.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, TYPE_CHECKING

if TYPE_CHECKING:
    from binky.config import Limits

# restart backoff + give-up thresholds (seconds / counts)
_BACKOFF_BASE = 0.5
_BACKOFF_MAX = 30.0
_GIVE_UP_AFTER = 5  # consecutive restarts before we stop trying
_STABLE_RESET = 60.0  # alive this long → the restart counter resets to zero
_HEALTH_INTERVAL = 5.0  # min seconds between continuous health checks


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


def _spawn(
    command: str, cwd: str, env: dict[str, str], log_file: Path, limits: Limits | None
) -> tuple[subprocess.Popen, IO[bytes]]:
    """Launch one service process, returning it and its (open) log handle."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handle = open(log_file, "ab")  # noqa: SIM115 — closed in stop()/on restart
    proc = subprocess.Popen(
        _wrap_limits(command, limits),
        cwd=cwd,
        env={**os.environ, **env},
        shell=True,
        stdout=handle,
        stderr=subprocess.STDOUT,
        **_new_group_kwargs(),
    )
    return proc, handle


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


def _should_restart(policy: str, returncode: int | None) -> bool:
    if policy == "always":
        return True
    if policy == "never":
        return False
    return returncode not in (0, None)  # on-failure: only on a non-zero exit


def _backoff(restarts: int) -> float:
    return min(_BACKOFF_BASE * (2 ** (restarts - 1)), _BACKOFF_MAX)


@dataclass
class _Managed:
    """One supervised service: how to (re)spawn it, plus its live process + state."""

    worktree_id: int
    service: str
    command: str  # resolved, pre-limits — re-wrapped on every (re)spawn
    cwd: str
    env: dict[str, str]
    log_file: Path
    limits: Limits | None
    restart: str  # "on-failure" | "always" | "never"
    health_check: Callable[[], bool] | None
    proc: subprocess.Popen
    handle: IO[bytes]
    restarts: int = 0
    backoff_until: float = 0.0
    started_at: float = 0.0
    healthy: bool = True
    last_health: float = 0.0

    def wants_restart(self) -> bool:
        return (
            _should_restart(self.restart, self.proc.returncode)
            and self.restarts < _GIVE_UP_AFTER
        )


@dataclass
class TickReport:
    """What one supervision tick changed, for the daemon to sync to the registry."""

    restarted: list[tuple[int, str]] = field(default_factory=list)  # (worktree_id, service)
    changed_pids: dict[int, list[int]] = field(default_factory=dict)  # worktree_id -> live pids
    worktrees_down: list[int] = field(default_factory=list)  # exited for good; reclaim them


class ProcessManager:
    """Thread-safe process supervision. The daemon runs up/down on a worker pool and
    a periodic tick() supervises children, so several threads touch this at once; a
    lock guards the ``_procs`` map. The slow parts (kill/wait, health checks) run on
    local references *outside* the lock."""

    def __init__(self) -> None:
        self._procs: dict[int, list[_Managed]] = {}
        self._lock = threading.Lock()

    def _entries(self, worktree_id: int) -> list[_Managed]:
        with self._lock:
            return list(self._procs.get(worktree_id, []))

    def is_running(self, worktree_id: int) -> bool:
        return any(m.proc.poll() is None for m in self._entries(worktree_id))

    def pids(self, worktree_id: int) -> list[int]:
        return [m.proc.pid for m in self._entries(worktree_id) if m.proc.poll() is None]

    def worktree_health(self, worktree_id: int) -> str | None:
        """'healthy'/'unhealthy' across the health-tracked services, or None if none."""
        tracked = [m for m in self._entries(worktree_id) if m.health_check is not None]
        if not tracked:
            return None
        return "healthy" if all(m.healthy for m in tracked) else "unhealthy"

    def start(
        self,
        worktree_id: int,
        service: str,
        command: str,
        cwd: str,
        env: dict[str, str],
        log_file: Path,
        limits: Limits | None = None,
        restart: str = "on-failure",
        health_check: Callable[[], bool] | None = None,
    ) -> int:
        proc, handle = _spawn(command, cwd, env, log_file, limits)
        managed = _Managed(
            worktree_id, service, command, cwd, env, log_file, limits, restart,
            health_check, proc, handle, started_at=time.monotonic(),
        )
        with self._lock:
            self._procs.setdefault(worktree_id, []).append(managed)
        return proc.pid

    def stop(self, worktree_id: int) -> int:
        # remove from supervision first, so tick() won't try to restart what we kill
        with self._lock:
            entries = self._procs.pop(worktree_id, [])
        alive = [m for m in entries if m.proc.poll() is None]
        for m in alive:
            _kill_tree(m.proc)
        for m in alive:
            try:
                m.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                m.proc.kill()
        for m in entries:
            with contextlib.suppress(Exception):
                m.handle.close()
        return len(alive)

    def stop_all(self) -> None:
        with self._lock:
            ids = list(self._procs)
        for worktree_id in ids:
            self.stop(worktree_id)

    def tick(self, now: float) -> TickReport:
        """Reap exits, apply restart policy, and flag fully-down worktrees.

        Structural work (respawn/retire) happens under the lock; the slow health
        probes run afterwards on the surviving processes, unlocked.
        """
        report = TickReport()
        health_due: list[_Managed] = []
        with self._lock:
            for wt_id in list(self._procs):
                survivors: list[_Managed] = []
                changed = False
                for m in self._procs[wt_id]:
                    if m.proc.poll() is None:  # alive
                        if m.restarts and now - m.started_at >= _STABLE_RESET:
                            m.restarts = 0
                        if m.health_check is not None and now - m.last_health >= _HEALTH_INTERVAL:
                            health_due.append(m)
                        survivors.append(m)
                    elif m.wants_restart():
                        if now >= m.backoff_until:  # respawn now
                            with contextlib.suppress(Exception):
                                m.handle.close()
                            m.proc, m.handle = _spawn(m.command, m.cwd, m.env, m.log_file, m.limits)
                            m.restarts += 1
                            m.backoff_until = now + _backoff(m.restarts)
                            m.started_at = now
                            m.healthy = True
                            m.last_health = 0.0
                            changed = True
                            report.restarted.append((wt_id, m.service))
                        survivors.append(m)  # else: still waiting out its backoff
                    else:  # retire: restart=never, or gave up after too many failures
                        with contextlib.suppress(Exception):
                            m.handle.close()
                        changed = True

                if not survivors or all(
                    m.proc.poll() is not None and not m.wants_restart() for m in survivors
                ):
                    del self._procs[wt_id]
                    report.worktrees_down.append(wt_id)
                else:
                    self._procs[wt_id] = survivors
                    if changed:
                        report.changed_pids[wt_id] = [
                            m.proc.pid for m in survivors if m.proc.poll() is None
                        ]
        for m in health_due:
            try:
                m.healthy = bool(m.health_check())  # type: ignore[misc]
            except Exception:
                m.healthy = False
            m.last_health = now
        return report
