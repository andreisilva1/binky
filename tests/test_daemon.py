"""Integration tests for the daemon lifecycle (spawns a real detached process)."""

import asyncio
import contextlib
import os
import stat
import threading
import time
from pathlib import Path

import pytest

from binky import daemon, ipc, paths, rpc, settings
from binky.registry import WorktreeStatus
from binky.supervisor import TickReport


class _FakeProcman:
    """A stand-in for ProcessManager for the pressure-eviction tests: it reports the
    given worktrees as running and records which ones get stopped, without real children."""

    def __init__(self, running) -> None:
        self.running = set(running)
        self.stopped: list[int] = []

    def is_running(self, wt_id: int) -> bool:
        return wt_id in self.running

    def stop(self, wt_id: int) -> None:
        self.stopped.append(wt_id)
        self.running.discard(wt_id)


def _seed_slotted(d: daemon.Daemon, project: str, name: str, token: int):
    """Register a ready worktree that holds admission slot `token`, as a live `up` would."""
    wt = d.registry.create_worktree(
        project, name, f"/tmp/{name}", name, status=WorktreeStatus.ready
    )
    d._slots[(project, name)] = token
    d._up_params[(project, name)] = {"root": f"/tmp/{name}", "worktree": name}
    return wt

def _write_url_config(path: Path) -> None:
    """A minimal project with one `url` service, so a route can be rebuilt from it."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "binky.toml").write_text(
        '[project]\n'
        'name = "demo"\n'
        '[services.web]\n'
        "cmd = 'python -c \"pass\"'\n"
        'url = "web"\n',
        encoding="utf-8",
    )


unix_only = pytest.mark.skipif(os.name == "nt", reason="Windows uses a loopback TCP port")


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("BINKY_HOME", str(tmp_path))
    # ports live in the settings store now; disable the proxy so tests don't bind :80
    settings.save(settings.Settings(proxy_port=0, https_port=0))
    yield
    # best-effort: never leave a spawned daemon behind
    with contextlib.suppress(Exception):
        daemon.stop(timeout=5)


def test_not_running_initially() -> None:
    assert daemon.is_running() is False
    assert ipc.read_daemon_info() is None
    with pytest.raises(ipc.DaemonNotRunning):
        ipc.call("ping")


def test_a_daemon_that_dies_on_startup_says_why() -> None:
    """A failed start must report the cause, not just that time ran out.

    With stderr going to DEVNULL these two were the same event to the caller,
    which is what made a dead daemon look like a slow one. A directory sitting
    where the registry's SQLite file belongs kills the daemon in its
    constructor, before it writes its endpoint file — so this exercises the path
    where there is nothing to ping and never will be. BINKY_HOME itself stays a
    real directory, so the daemon's stderr log is still captured and returned.
    """
    # occupy the db path with a directory so sqlite3.connect can't open it
    paths.db_path().mkdir(parents=True, exist_ok=True)

    with pytest.raises(RuntimeError) as excinfo:
        daemon.start(timeout=30)

    message = str(excinfo.value)
    assert "exited with code" in message, message  # fast-failed, didn't wait out the timeout
    # and carried the daemon's own error back, not a bare "timed out"
    assert "unable to open database file" in message, message
    assert paths.daemon_log_path().exists()


def test_start_ping_stop() -> None:
    daemon.start(timeout=20)
    assert daemon.is_running()

    result = ipc.call("ping")
    assert result["pong"] is True
    assert isinstance(result["pid"], int)

    with pytest.raises(ipc.RpcError, match="unknown method") as excinfo:
        ipc.call("does-not-exist")
    assert excinfo.value.code == "unknown_method"

    # introspection methods over the wire
    assert set(ipc.call("methods")["methods"]) >= {"ping", "shutdown", "methods", "version"}
    assert ipc.call("version")["version"]

    assert daemon.stop(timeout=10) is True
    assert daemon.is_running() is False
    assert ipc.read_daemon_info() is None


def test_start_is_idempotent() -> None:
    daemon.start(timeout=20)
    first = ipc.read_daemon_info()
    daemon.start(timeout=20)  # already running → no-op
    second = ipc.read_daemon_info()
    assert first.pid == second.pid


@unix_only
def test_a_deep_home_still_gets_a_socket_that_fits(tmp_path, monkeypatch) -> None:
    """`sun_path` is 104 bytes on macOS, and a macOS $TMPDIR spends half of that
    before Binky appends anything — so a deep BINKY_HOME used to kill the daemon
    at bind() with a bare "AF_UNIX path too long"."""
    deep = tmp_path / ("d" * 40) / ("e" * 40) / ("f" * 40)
    monkeypatch.setenv("BINKY_HOME", str(deep))

    sock = paths.socket_path()
    assert len(str(sock).encode()) <= 100, sock
    assert not str(sock).startswith(str(deep))  # fell back, rather than truncating

    info = sock.parent.lstat()
    assert stat.S_ISDIR(info.st_mode)
    assert info.st_uid == os.getuid()
    assert stat.S_IMODE(info.st_mode) == 0o700  # /tmp is shared; this dir is not


@unix_only
def test_a_deep_home_is_keyed_to_its_own_socket(tmp_path, monkeypatch) -> None:
    """Two homes must not land on the same socket just because both were long."""
    long_tail = "g" * 60
    monkeypatch.setenv("BINKY_HOME", str(tmp_path / "one" / long_tail))
    first = paths.socket_path()
    monkeypatch.setenv("BINKY_HOME", str(tmp_path / "two" / long_tail))
    assert paths.socket_path() != first


@unix_only
def test_the_daemon_comes_up_under_a_deep_home(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BINKY_HOME", str(tmp_path / ("h" * 40) / ("i" * 40) / ("j" * 40)))
    daemon.start(timeout=20)
    assert ipc.call("ping")["pong"] is True
    assert daemon.stop(timeout=10) is True


def test_clear_daemon_info_spares_another_daemons_endpoint() -> None:
    """A daemon can take seconds to finish dying — long enough for a replacement to
    have written its own endpoint. Deleting that one leaves a daemon nobody can find."""
    mine = ipc.DaemonInfo(transport="tcp", token="t", pid=os.getpid(), host="127.0.0.1", port=1)
    theirs = ipc.DaemonInfo(
        transport="tcp", token="t", pid=os.getpid() + 1, host="127.0.0.1", port=2
    )

    ipc.write_daemon_info(theirs)
    ipc.clear_daemon_info()
    assert ipc.read_daemon_info() == theirs  # not ours to delete

    ipc.write_daemon_info(mine)
    ipc.clear_daemon_info()
    assert ipc.read_daemon_info() is None

    paths.daemon_info_path().write_text("{not json", encoding="utf-8")
    ipc.clear_daemon_info()
    assert not paths.daemon_info_path().exists()  # unreadable is a leftover either way


def test_apply_report_keeps_registry_io_off_the_event_loop() -> None:
    """A supervision tick's registry writes are blocking SQLite that can wait out the
    busy-timeout; if they ran on the loop they'd freeze the proxy and RPC. They must
    run off it, while the admission/route bookkeeping stays on the loop — and the
    worktree still ends up marked down with its slot released."""
    d = daemon.Daemon()
    try:
        wt = d.registry.create_worktree(
            "proj", "alpha", "/tmp/x", "alpha", status=WorktreeStatus.ready
        )
        # mirror a live `up`: the worktree holds an admission slot and a proxy route
        d._slots[("proj", "alpha")] = 1
        d._routes_by_wt[("proj", "alpha")] = {"alpha.proj.test": 4000}

        seen: dict[str, threading.Thread] = {}
        real_set = d.registry.set_worktree_status

        def recording_set(worktree_id, status):
            seen["thread"] = threading.current_thread()
            return real_set(worktree_id, status)

        d.registry.set_worktree_status = recording_set  # type: ignore[method-assign]
        report = TickReport(worktrees_down=[wt.id])

        async def run():
            loop_thread = threading.current_thread()
            await d._apply_report(report)
            return loop_thread

        loop_thread = asyncio.run(run())

        assert seen["thread"] is not loop_thread  # the write ran off the event loop
        assert d.registry.get_worktree_by_id(wt.id).status == "stopped"  # ...and still applied
        assert ("proj", "alpha") not in d._slots  # slot freed on the loop
        assert d.routes == {}  # route dropped on the loop
    finally:
        d.registry.close()


def test_restart_event_logs_the_worktree_name_not_just_its_id(monkeypatch) -> None:
    """A supervisor auto-restart must log project+worktree *name* like every other event, so a
    `grep <worktree> events.log` surfaces restarts — not a numeric id the user never sees (I10).
    The name is resolved off the event loop, inside `_sync_registry`, to keep registry reads
    off the loop."""
    d = daemon.Daemon()
    try:
        wt = d.registry.create_worktree(
            "proj", "alpha", "/tmp/alpha", "alpha", status=WorktreeStatus.ready
        )
        events: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            daemon.observability, "log_event", lambda name, **kw: events.append((name, kw))
        )
        report = TickReport(restarted=[(wt.id, "api")])

        asyncio.run(d._apply_report(report))

        restarts = [kw for name, kw in events if name == "restart"]
        assert restarts == [{"project": "proj", "worktree": "alpha", "service": "api"}]
        assert "worktree_id" not in restarts[0]  # keyed by name, like up/down/reload/gc
    finally:
        d.registry.close()


def test_reachability_warnings_flag_a_dead_proxy() -> None:
    """`up` must not print a clean success when the proxy that serves its hostnames
    never bound. With routes present and a bind error recorded, a warning appears; with
    the proxy healthy, none does — and https is only warned about when it was asked for."""
    d = daemon.Daemon()
    try:
        routes = {"alpha.proj.test": 4000}
        assert d._reachability_warnings(routes, wants_https=False) == []  # healthy: silent

        d._proxy_error = ":80 — [Errno 13] Permission denied"
        warnings = d._reachability_warnings(routes, wants_https=False)
        assert len(warnings) == 1
        assert "hostnames won't resolve" in warnings[0]
        assert ":port" in warnings[0]  # ...but points at the fallback that still works

        # a worktree with no routes isn't affected by a dead proxy
        assert d._reachability_warnings({}, wants_https=False) == []

        d._https_error = "Caddy is not on PATH"
        both = d._reachability_warnings(routes, wants_https=True)
        assert any("https" in w for w in both)
        # https is only surfaced when the project actually requested it
        assert not any("https" in w for w in d._reachability_warnings(routes, wants_https=False))
    finally:
        d.registry.close()


def test_doctor_rpc_reports_proxy_and_https_state() -> None:
    d = daemon.Daemon()
    try:
        d._proxy_port = 80
        d._https_error = "Caddy is not on PATH"
        report = asyncio.run(d._doctor({}))
        assert report == {
            "proxy_port": 80,
            "proxy_error": None,
            "https_active": False,
            "https_error": "Caddy is not on PATH",
        }
    finally:
        d.registry.close()


def test_up_refuses_fast_when_admission_can_never_succeed() -> None:
    """A resource floor that nothing can free must make `up` fail with an actionable
    message immediately, not hang until the caller's RPC times out."""
    d = daemon.Daemon()
    try:
        d.admission.min_free_disk_bytes = 10**18  # impossible, and nothing is running
        with pytest.raises(rpc.RpcFault) as excinfo:
            asyncio.run(d._up({"root": "/nonexistent", "worktree": "x"}))
        assert excinfo.value.code == "resource_exhausted"
        assert "disk" in excinfo.value.message
        assert d.admission.queued == 0  # refused outright, never enqueued
    finally:
        d.registry.close()


def test_pressure_suspends_the_newest_worktree() -> None:
    """When free RAM sits under the reserve for a streak of ticks, the daemon pauses the
    newest worktree (highest admission token, LIFO), frees its slot, and parks it to come
    back — leaving older worktrees running."""
    d = daemon.Daemon()
    try:
        d.admission.min_free_disk_bytes = 0  # isolate the RAM path
        alpha = _seed_slotted(d, "proj", "alpha", token=1)
        beta = _seed_slotted(d, "proj", "beta", token=2)  # newest
        d.procman = _FakeProcman({alpha.id, beta.id})
        d.admission.min_free_bytes = 10**18  # under pressure, and can't self-admit yet

        async def run():
            for _ in range(daemon.PRESSURE_TICKS):
                await d._relieve_pressure()
            captured = {
                "stopped": list(d.procman.stopped),
                "beta_slotted": ("proj", "beta") in d._slots,
                "alpha_slotted": ("proj", "alpha") in d._slots,
                "beta_status": d.registry.get_worktree_by_id(beta.id).status,
                "parked": ("proj", "beta") in d._resume_tasks,
            }
            for t in list(d._resume_tasks.values()):  # tidy the parked waiter in-loop
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t
            return captured

        out = asyncio.run(run())
        assert out["stopped"] == [beta.id]  # newest, and only it
        assert not out["beta_slotted"]  # its RAM slot was released
        assert out["alpha_slotted"]  # the older one keeps running
        assert out["beta_status"] == "suspended"
        assert out["parked"]  # queued to resume itself
    finally:
        d.registry.close()


def test_a_suspended_worktree_resumes_itself_when_pressure_clears(monkeypatch) -> None:
    """A parked worktree waits in the admission FIFO; once RAM frees, the pump admits it and
    it re-ups (re-spawn, no re-provision) back to ready, restoring its routes and slot."""
    d = daemon.Daemon()
    try:
        d.admission.min_free_disk_bytes = 0
        beta = _seed_slotted(d, "proj", "beta", token=2)
        d.procman = _FakeProcman({beta.id})

        def fake_up(reg, procman, params):  # resume re-ups via ops.up; stub the heavy part
            reg.set_worktree_status(beta.id, WorktreeStatus.ready)
            return {"project": "proj", "worktree": "beta", "routes": {"beta.proj.test": 4100}}

        monkeypatch.setattr(daemon.ops, "up", fake_up)

        async def run():
            d.admission.min_free_bytes = 10**18  # pressure on
            for _ in range(daemon.PRESSURE_TICKS):
                await d._relieve_pressure()
            assert d.registry.get_worktree_by_id(beta.id).status == "suspended"
            task = d._resume_tasks[("proj", "beta")]
            d.admission.min_free_bytes = 0  # pressure clears
            d.admission.pump()  # admits the parked waiter
            await asyncio.wait_for(task, timeout=2)
            return {
                "status": d.registry.get_worktree_by_id(beta.id).status,
                "slotted": ("proj", "beta") in d._slots,
                "routes": dict(d.routes),
                "resumed": ("proj", "beta") in d._resumed_at,
                "still_parked": bool(d._resume_tasks),
            }

        out = asyncio.run(run())
        assert out["status"] == "ready"  # brought back up on its own
        assert out["slotted"]
        assert out["routes"] == {"beta.proj.test": 4100}  # routing restored
        assert out["resumed"]  # timestamped, so it isn't the next to be evicted
        assert not out["still_parked"]  # the task cleared itself on success
    finally:
        d.registry.close()


def test_a_just_resumed_worktree_is_protected_from_immediate_re_eviction() -> None:
    """Anti-flap: the newest worktree is skipped while inside its post-resume grace window,
    so a return of pressure evicts an older, unprotected one instead of thrashing the same."""
    d = daemon.Daemon()
    try:
        d.admission.min_free_disk_bytes = 0
        alpha = _seed_slotted(d, "proj", "alpha", token=1)
        beta = _seed_slotted(d, "proj", "beta", token=2)  # newest, but just resumed
        d.procman = _FakeProcman({alpha.id, beta.id})
        d._resumed_at[("proj", "beta")] = time.monotonic()  # inside the grace window
        d.admission.min_free_bytes = 10**18

        async def run():
            evicted = await d._evict_one_under_pressure()
            captured = {"evicted": evicted, "stopped": list(d.procman.stopped)}
            for t in list(d._resume_tasks.values()):
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t
            return captured

        out = asyncio.run(run())
        assert out["evicted"] is True
        assert out["stopped"] == [alpha.id]  # the older, unprotected one — not the newest
    finally:
        d.registry.close()


def test_taking_a_suspended_worktree_down_cancels_its_pending_resume() -> None:
    """A real `down`/`remove` outranks a pending resume: releasing the slot cancels the
    parked waiter and forgets its up-params, so freed RAM never resurrects it."""
    d = daemon.Daemon()
    try:
        d.admission.min_free_disk_bytes = 0
        beta = _seed_slotted(d, "proj", "beta", token=2)
        d.procman = _FakeProcman({beta.id})
        d.admission.min_free_bytes = 10**18

        async def run():
            for _ in range(daemon.PRESSURE_TICKS):
                await d._relieve_pressure()
            task = d._resume_tasks[("proj", "beta")]
            d._release_slot("proj", "beta")  # what _down/_remove drive
            for _ in range(3):
                await asyncio.sleep(0)  # let the cancellation settle
            return {
                "done": task.done(),
                "tasks": bool(d._resume_tasks),
                "params": bool(d._up_params),
            }

        out = asyncio.run(run())
        assert out["done"]  # the parked waiter stopped
        assert not out["tasks"]  # and was forgotten
        assert not out["params"]  # along with its resume context
    finally:
        d.registry.close()


def test_adopt_state_rebuilds_routes_slot_and_resume_context(tmp_path: Path) -> None:
    """C3 + I6: after `reconcile` adopts a still-live worktree, the daemon must rebuild the
    in-memory state a normal `up` produces — proxy routes, admission slot, resume context — or
    the process survives the restart but comes back unreachable (dead hostnames) and uncounted."""
    d = daemon.Daemon()
    try:
        wt_path = tmp_path / "checkout"
        _write_url_config(wt_path)
        wt = d.registry.create_worktree(
            "demo", "feat", str(wt_path), "feat", status=WorktreeStatus.ready
        )
        d.registry.allocate_port(wt.id, "web", "", 4700)
        d.registry.set_up_params(
            wt.id, {"root": str(wt_path), "worktree": "feat", "group": None}
        )
        d.procman = _FakeProcman({wt.id})  # reconcile just adopted it → reports running

        d._adopt_state()

        assert d.routes == {"web.feat.demo.localhost": 4700}  # hostname routing restored
        assert ("demo", "feat") in d._slots  # admission slot restored...
        assert d.admission.in_use == 1  # ...and counted, so the next `up` can't over-admit
        assert d._up_params[("demo", "feat")]["worktree"] == "feat"  # resume context restored
    finally:
        d.registry.close()


def test_adopt_state_ignores_worktrees_not_actually_running(tmp_path: Path) -> None:
    """Only worktrees `reconcile` truly adopted (ready + live) get state rebuilt; a `ready`
    row whose processes are gone (reconcile would drop it to `stopped`) must not be routed."""
    d = daemon.Daemon()
    try:
        wt_path = tmp_path / "checkout"
        _write_url_config(wt_path)
        wt = d.registry.create_worktree(
            "demo", "feat", str(wt_path), "feat", status=WorktreeStatus.ready
        )
        d.registry.allocate_port(wt.id, "web", "", 4700)
        d.procman = _FakeProcman(set())  # nothing alive to adopt

        d._adopt_state()

        assert d.routes == {}
        assert d._slots == {}
        assert d.admission.in_use == 0
    finally:
        d.registry.close()


def test_repair_routes_reinstalls_missing_routes_only(tmp_path: Path) -> None:
    """The idempotent safety net: `up` on an already-up worktree heals *missing* routes but
    never clobbers routes that are already installed."""
    d = daemon.Daemon()
    try:
        wt_path = tmp_path / "checkout"
        _write_url_config(wt_path)
        wt = d.registry.create_worktree(
            "demo", "feat", str(wt_path), "feat", status=WorktreeStatus.ready
        )
        d.registry.allocate_port(wt.id, "web", "", 4700)
        params = {"root": str(wt_path), "worktree": "feat"}

        d._repair_routes(params)  # routes were missing → installed
        assert d.routes == {"web.feat.demo.localhost": 4700}

        d._routes_by_wt[("demo", "feat")] = {"sentinel.host": 9999}  # pretend already routed
        d._rebuild_routes()
        d._repair_routes(params)
        assert d.routes == {"sentinel.host": 9999}  # untouched — it already had routes
    finally:
        d.registry.close()


def test_bad_token_rejected() -> None:
    daemon.start(timeout=20)
    info = ipc.read_daemon_info()
    tampered = ipc.DaemonInfo(
        transport=info.transport,
        token="wrong-token",
        pid=info.pid,
        path=info.path,
        host=info.host,
        port=info.port,
    )
    ipc.write_daemon_info(tampered)
    with pytest.raises(ipc.RpcError, match="bad token"):
        ipc.call("ping")
    # restore the real token so the fixture teardown can shut the daemon down
    ipc.write_daemon_info(info)
