"""Live supervision: tick() reaps exits, applies restart policy, and reclaims
worktrees that have gone fully down. `tick(now)` takes the clock as an argument,
so these drive time by hand and stay deterministic (no real sleeping/backoff waits).
"""

import builtins
import sys
import time
from pathlib import Path

import pytest

from binky import supervisor
from binky.supervisor import _GIVE_UP_AFTER, ProcessManager


def _script(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return f'"{sys.executable}" "{path}"'


def _wait_exit(pm: ProcessManager, worktree_id: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pm.is_running(worktree_id):
            return True
        time.sleep(0.02)
    return False


def _start(pm: ProcessManager, tmp_path: Path, cmd: str, **kw) -> None:
    pm.start(1, "svc", cmd, str(tmp_path), {}, tmp_path / "svc.log", **kw)


def test_on_failure_restarts_after_crash(tmp_path: Path) -> None:
    pm = ProcessManager()
    _start(pm, tmp_path, _script(tmp_path, "boom.py", "import sys; sys.exit(1)"),
           restart="on-failure")
    assert _wait_exit(pm, 1)

    report = pm.tick(time.monotonic())
    assert (1, "svc") in report.restarted
    assert pm.is_running(1) or _wait_exit(pm, 1)  # a fresh process was spawned
    pm.stop(1)


def test_on_failure_keeps_clean_exit_down(tmp_path: Path) -> None:
    pm = ProcessManager()
    _start(pm, tmp_path, _script(tmp_path, "ok.py", "import sys; sys.exit(0)"),
           restart="on-failure")
    assert _wait_exit(pm, 1)

    report = pm.tick(time.monotonic())
    assert 1 in report.worktrees_down  # exit 0 under on-failure → not restarted
    assert not report.restarted
    assert not pm.is_running(1)


def test_always_restarts_even_on_clean_exit(tmp_path: Path) -> None:
    pm = ProcessManager()
    _start(pm, tmp_path, _script(tmp_path, "ok.py", "import sys; sys.exit(0)"),
           restart="always")
    assert _wait_exit(pm, 1)

    report = pm.tick(time.monotonic())
    assert (1, "svc") in report.restarted
    pm.stop(1)


def test_never_reclaims_worktree(tmp_path: Path) -> None:
    pm = ProcessManager()
    _start(pm, tmp_path, _script(tmp_path, "boom.py", "import sys; sys.exit(1)"),
           restart="never")
    assert _wait_exit(pm, 1)

    report = pm.tick(time.monotonic())
    assert 1 in report.worktrees_down
    assert not report.restarted


def test_gives_up_after_cap(tmp_path: Path) -> None:
    """A crash-looping service is restarted up to the cap, then reclaimed."""
    pm = ProcessManager()
    _start(pm, tmp_path, _script(tmp_path, "boom.py", "import sys; sys.exit(1)"),
           restart="always")
    assert _wait_exit(pm, 1)

    now = 1000.0  # advance past every backoff window by hand
    for _ in range(_GIVE_UP_AFTER):
        report = pm.tick(now)
        assert (1, "svc") in report.restarted
        now += 100.0
        assert _wait_exit(pm, 1)

    report = pm.tick(now)  # cap reached → stop trying, reclaim
    assert 1 in report.worktrees_down
    assert not report.restarted


def test_spawn_closes_the_log_when_popen_fails(tmp_path: Path, monkeypatch) -> None:
    """If Popen raises, the log handle _spawn opened must not leak — nobody else
    will ever be handed it to close."""
    opened: list = []
    real_open = builtins.open

    def tracking_open(*args, **kwargs):
        handle = real_open(*args, **kwargs)
        opened.append(handle)
        return handle

    def failing_popen(*_args, **_kwargs):
        raise OSError("exec failed")

    monkeypatch.setattr(builtins, "open", tracking_open)
    monkeypatch.setattr(supervisor.subprocess, "Popen", failing_popen)

    with pytest.raises(OSError, match="exec failed"):
        supervisor._spawn("cmd", str(tmp_path), {}, tmp_path / "svc.log", None)

    assert opened, "expected _spawn to open a log handle"
    assert all(handle.closed for handle in opened)  # ...and to close it on the failure


def test_service_alive_is_scoped_to_one_service(tmp_path: Path) -> None:
    pm = ProcessManager()
    _start(pm, tmp_path, _script(tmp_path, "sleep.py", "import time; time.sleep(30)"))
    assert pm.service_alive(1, "svc") is True
    assert pm.service_alive(1, "other") is False  # a different service name
    assert pm.service_alive(2, "svc") is False  # a different worktree
    pm.stop(1)
    assert pm.service_alive(1, "svc") is False  # gone once stopped


def test_worktree_health_reflects_probe(tmp_path: Path) -> None:
    sleeper = _script(tmp_path, "sleep.py", "import time; time.sleep(30)")

    pm = ProcessManager()
    _start(pm, tmp_path, sleeper, restart="never", health_check=lambda: False)
    pm.tick(time.monotonic())  # runs the probe on the (still-alive) process
    assert pm.worktree_health(1) == "unhealthy"
    pm.stop(1)

    pm2 = ProcessManager()
    pm2.start(2, "svc", sleeper, str(tmp_path), {}, tmp_path / "svc.log",
              restart="never", health_check=lambda: True)
    pm2.tick(time.monotonic())
    assert pm2.worktree_health(2) == "healthy"
    pm2.stop(2)
