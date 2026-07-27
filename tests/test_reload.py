"""`binky reload` — down+up in one atomic step under the worktree lock.

Exercised at the daemon-handler level with ``ops.up``/``ops.down`` stubbed, so the composition
is proven without spawning real services: the order (stop then start), that admission is released
and re-acquired rather than double-counted, that the routes end up as the fresh ``up`` left them,
and that a restart that fails to come back leaks no slot. That ``_reload`` returns at all proves it
doesn't re-enter the (non-reentrant) worktree lock the way calling ``_up``/``_down`` would.
"""

import asyncio

import pytest

from binky import daemon, rpc, settings
from binky.registry import WorktreeStatus


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("BINKY_HOME", str(tmp_path))
    settings.save(settings.Settings(proxy_port=0, https_port=0))  # don't bind :80 in tests


def test_reload_runs_down_then_up_reusing_one_admission_slot(monkeypatch) -> None:
    d = daemon.Daemon()
    try:
        d.admission.min_free_disk_bytes = 0
        d.admission.min_free_bytes = 0  # never under pressure; isolate the reload path
        # a live worktree, as `up` would have left it: a real admission slot and a route
        token = asyncio.run(d.admission.acquire())
        d.registry.create_worktree(
            "proj", "alpha", "/tmp/alpha", "alpha", status=WorktreeStatus.ready
        )
        d._slots[("proj", "alpha")] = token
        d._routes_by_wt[("proj", "alpha")] = {"alpha.proj.test": 4000}
        d._rebuild_routes()

        calls: list[str] = []

        def fake_down(reg, procman, params):
            calls.append("down")
            return {"project": "proj", "worktree": "alpha", "stopped": 3}

        def fake_up(reg, procman, params):
            calls.append("up")
            return {
                "project": "proj", "worktree": "alpha",
                "routes": {"alpha.proj.test": 4200},  # a fresh port after restart
                "services": [{"service": "web", "pid": 11, "port": 4200}],
            }

        monkeypatch.setattr(daemon.ops, "down", fake_down)
        monkeypatch.setattr(daemon.ops, "up", fake_up)

        res = asyncio.run(d._reload({"root": "/tmp/alpha", "worktree": "alpha"}))

        assert calls == ["down", "up"]  # stop first, then start — never interleaved
        assert res["stopped"] == 3  # carried up from the down half
        assert len(res["services"]) == 1  # reported by the up half
        assert d.admission.in_use == 1  # one slot released then re-acquired, not two held
        assert ("proj", "alpha") in d._slots
        assert d.routes == {"alpha.proj.test": 4200}  # routing refreshed to what `up` returned
    finally:
        d.registry.close()


def test_reload_that_fails_to_come_back_up_leaks_no_slot(monkeypatch) -> None:
    """If the restart can't come healthy, the token grabbed for the up half is released — the
    worktree is left down (as a plain `down` would), not counted against capacity forever."""
    d = daemon.Daemon()
    try:
        d.admission.min_free_disk_bytes = 0
        d.admission.min_free_bytes = 0
        monkeypatch.setattr(
            daemon.ops, "down",
            lambda reg, pm, p: {"project": "proj", "worktree": "a", "stopped": 1},
        )

        def boom(reg, pm, p):
            raise rpc.RpcFault("boot_failed", "'web' did not become healthy")

        monkeypatch.setattr(daemon.ops, "up", boom)

        with pytest.raises(rpc.RpcFault) as excinfo:
            asyncio.run(d._reload({"root": "/tmp/a", "worktree": "a"}))
        assert excinfo.value.code == "boot_failed"
        assert d.admission.in_use == 0  # the up-half token was released on the way out
        assert ("proj", "a") not in d._slots
    finally:
        d.registry.close()
