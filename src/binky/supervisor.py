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

from binky.procs import NO_WINDOW, is_same_process

if TYPE_CHECKING:
    from binky.config import Limits

# restart backoff + give-up thresholds (seconds / counts)
_BACKOFF_BASE = 0.5
_BACKOFF_MAX = 30.0
_GIVE_UP_AFTER = 5  # consecutive restarts before we stop trying
_STABLE_RESET = 60.0  # alive this long → the restart counter resets to zero
_HEALTH_INTERVAL = 5.0  # min seconds between continuous health checks

# per-service log rotation, applied only at the (re)spawn boundary (see `_rotate_log`)
_LOG_MAX_BYTES = 5 * 1024 * 1024  # roll a service log once it grows past 5 MiB
_LOG_KEEP = 3  # keep this many rolled generations (.log.1 … .log.3)


def _renice() -> None:  # child-side, Unix only
    with contextlib.suppress(OSError):
        os.nice(10)


def _new_group_kwargs() -> dict:
    if os.name == "nt":
        return {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.BELOW_NORMAL_PRIORITY_CLASS
            | NO_WINDOW  # no flashing console per service start
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


def _rotate_log(log_file: Path) -> None:
    """Roll a service log if it has grown past the cap — only ever at a (re)spawn boundary.

    Rotation is safe *here* precisely because no process holds the file across a respawn:
    renaming it can't strand a live writer on the moved inode (the POSIX trap that makes
    rotating a running service's log a no-op). A crash-looping service therefore can't fill
    the disk — each restart passes through here and rolls the log once it's too big. A stable
    but noisy service that never restarts is intentionally out of scope (would need a pipe +
    reader thread, a supervisor model change)."""
    with contextlib.suppress(OSError):
        if not log_file.exists() or log_file.stat().st_size < _LOG_MAX_BYTES:
            return

        def rolled(n: int) -> Path:
            return log_file.with_name(f"{log_file.name}.{n}")

        with contextlib.suppress(OSError):
            rolled(_LOG_KEEP).unlink()  # drop the oldest generation
        for n in range(_LOG_KEEP - 1, 0, -1):  # shift .log.(n) → .log.(n+1)
            src = rolled(n)
            if src.exists():
                src.replace(rolled(n + 1))
        log_file.replace(rolled(1))  # current → .log.1, leaving a fresh .log to open


def _spawn(
    command: str, cwd: str, env: dict[str, str], log_file: Path, limits: Limits | None
) -> tuple[subprocess.Popen, IO[bytes]]:
    """Launch one service process, returning it and its (open) log handle."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    _rotate_log(log_file)  # roll before reopening, so a crash-loop can't grow the log forever
    handle = open(log_file, "ab")  # noqa: SIM115 — closed in stop()/on restart
    try:
        proc = subprocess.Popen(
            _wrap_limits(command, limits),
            cwd=cwd,
            env={**os.environ, **env},
            shell=True,
            stdout=handle,
            stderr=subprocess.STDOUT,
            **_new_group_kwargs(),
        )
    except BaseException:
        # Popen failed (bad cwd, exec error, ...): nobody will ever be returned this
        # handle to close it, so close it here before letting the error propagate.
        handle.close()
        raise
    return proc, handle


def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, check=False, creationflags=NO_WINDOW,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass


def kill_pid(pid: int) -> None:
    """Kill a process and its group/tree by pid — best effort (used by reconcile)."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, check=False, creationflags=NO_WINDOW,
        )
    else:
        with contextlib.suppress(ProcessLookupError, OSError):
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError, OSError):
            os.kill(pid, signal.SIGKILL)


class _Adopted:
    """A live process the daemon did **not** spawn, tracked by ``(pid, create_time)``.

    On a daemon restart the previous daemon's children are still running (C3); rather than
    kill them, ``ProcessManager.adopt`` wraps each in one of these. It mimics the small slice
    of ``subprocess.Popen`` the supervisor actually reads — ``pid``/``poll``/``returncode``/
    ``wait``/``kill`` — so an adopted service flows through the same code as a spawned one,
    minus what a non-child can't provide: there is no ``waitpid`` for someone else's process,
    so we have no real exit code and detect death by the identity going stale (a recycled pid
    with a new birth time reads as *gone*, never as *still alive*)."""

    def __init__(self, pid: int, create_time: float | None) -> None:
        self.pid = pid
        self._create_time = create_time
        self.returncode: int | None = None  # not our child → never a real code

    def poll(self) -> int | None:
        if is_same_process(self.pid, self._create_time):
            return None  # still the process we adopted
        if self.returncode is None:
            self.returncode = -1  # gone (or recycled into a stranger); code is unknowable
        return self.returncode

    def wait(self, timeout: float | None = None) -> int | None:
        deadline = time.monotonic() + (timeout or 0.0)
        while self.poll() is None:
            if timeout is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.pid, timeout)
            time.sleep(0.05)
        return self.returncode

    def kill(self) -> None:
        kill_pid(self.pid)


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
    proc: subprocess.Popen | _Adopted  # _Adopted for processes reconcile took over (C3)
    handle: IO[bytes] | None  # None for an adopted process — we don't own its log handle
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
    failed: set[int] = field(default_factory=set)  # subset of down: crash-looped past give-up


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

    def service_alive(self, worktree_id: int, service: str) -> bool:
        """True if *service* has a live process — lets a startup wait notice a crash."""
        return any(
            m.service == service and m.proc.poll() is None
            for m in self._entries(worktree_id)
        )

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

    def adopt(self, worktree_id: int, identities: list[tuple[int, float | None]]) -> list[int]:
        """Take over processes still alive from a previous daemon, by ``(pid, create_time)``.

        The point of C3: a daemon restart must not tear down running worktrees. Adopted
        processes are supervised for **liveness and stoppability only** — we lack their spawn
        recipe and even which service each pid is (the registry records pids, not services),
        so they carry no restart policy and no health check. If an adopted process dies, its
        worktree simply goes down on the next tick, exactly as an un-restarted service would —
        the difference from before is only that we no longer kill it to get there. Returns the
        pids actually adopted (a pid that raced away before we wrapped it is skipped)."""
        managed: list[_Managed] = []
        adopted: list[int] = []
        for pid, create_time in identities:
            proc = _Adopted(pid, create_time)
            if proc.poll() is not None:
                continue  # gone between reconcile's check and here
            managed.append(
                _Managed(
                    worktree_id, "", "", "", {}, Path(), None, "never", None, proc, None,
                    started_at=time.monotonic(),
                )
            )
            adopted.append(pid)
        if managed:
            with self._lock:
                self._procs.setdefault(worktree_id, []).extend(managed)
        return adopted

    def stop(self, worktree_id: int) -> int:
        # remove from supervision first, so tick() won't try to restart what we kill
        with self._lock:
            entries = self._procs.pop(worktree_id, [])
        alive = [m for m in entries if m.proc.poll() is None]
        for m in alive:
            _kill_tree(m.proc)  # SIGTERM the whole group first (graceful)
        for m in alive:
            try:
                m.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # Escalate to the whole group, not just the shell leader: a plain
                # proc.kill() SIGKILLs only the pid we spawned (the shell), orphaning
                # the real service and any grandchildren still in the group (C9).
                kill_pid(m.proc.pid)
                with contextlib.suppress(Exception):
                    m.proc.wait(timeout=2)  # reap the leader we just killed
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
                gave_up = False  # a service exhausted its restarts (crash loop), not a clean exit
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
                    else:  # retire: restart=never (clean), or gave up after too many failures
                        if _should_restart(m.restart, m.proc.returncode):
                            gave_up = True  # policy wanted a restart but the cap was hit
                        with contextlib.suppress(Exception):
                            m.handle.close()
                        changed = True

                if not survivors or all(
                    m.proc.poll() is not None and not m.wants_restart() for m in survivors
                ):
                    del self._procs[wt_id]
                    report.worktrees_down.append(wt_id)
                    if gave_up:
                        report.failed.add(wt_id)
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
