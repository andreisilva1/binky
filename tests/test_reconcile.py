"""Tests for the startup reconciler (orphan cleanup + status correction)."""

import contextlib
import subprocess
import sys
import time
from pathlib import Path
from typing import ClassVar

from binky import adapters, ops, supervisor
from binky.registry import Registry, WorktreeStatus


class _FakeAdapter(adapters.Adapter):
    """Records the slices it holds in a class-level set so a test can assert teardown."""

    store: ClassVar[set[str]] = set()

    def provision(self, slice_name: str) -> None:
        _FakeAdapter.store.add(slice_name)

    def resolve(self, slice_name: str) -> str:
        return f"fake://{slice_name}"

    def teardown(self, slice_name: str) -> None:
        _FakeAdapter.store.discard(slice_name)


def test_reconcile_kills_orphans_and_sweeps(tmp_path: Path) -> None:
    reg = Registry.connect(tmp_path / "binky.db")
    orphan_proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], **supervisor._new_group_kwargs()
    )
    try:
        # a "ready" worktree left by a crashed daemon: a live orphan + a kept port
        ready = reg.create_worktree("p", "a", "/x", "a", status=WorktreeStatus.ready)
        reg.allocate_port(ready.id, "web", "", 4500)
        reg.add_process(ready.id, orphan_proc.pid)

        # a half-created "provisioning" worktree with a port
        prov = reg.create_worktree("p", "b", "/y", "b", status=WorktreeStatus.provisioning)
        reg.allocate_port(prov.id, "x", "", 4501)

        ops.reconcile(reg)

        a = reg.get_worktree("p", "a")
        assert a is not None
        assert a.status == "stopped"  # ready → stopped
        assert reg.processes_for(a.id) == []  # pids cleared
        assert reg.all_ports() == {4500}  # a's port kept, b's released
        assert reg.get_worktree("p", "b") is None  # provisioning orphan swept

        orphan_proc.wait(timeout=5)
        assert orphan_proc.poll() is not None  # orphan killed
    finally:
        with contextlib.suppress(Exception):
            orphan_proc.kill()
        reg.close()


def test_reconcile_spares_a_recycled_pid(tmp_path: Path) -> None:
    """A recorded pid whose birth time no longer matches is a stranger the OS handed
    the recycled number to — reconcile must drop the stale row but never kill it."""
    reg = Registry.connect(tmp_path / "binky.db")
    bystander = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], **supervisor._new_group_kwargs()
    )
    try:
        wt = reg.create_worktree("p", "a", "/x", "a", status=WorktreeStatus.ready)
        # recorded with a birth time from long before this pid's real one → identity
        # mismatch, i.e. "the process we knew is gone; something else holds its pid now".
        reg.add_process(wt.id, bystander.pid, create_time=1.0)

        ops.reconcile(reg)

        assert reg.processes_for(wt.id) == []  # the stale row is dropped either way
        time.sleep(0.3)
        assert bystander.poll() is None  # ...but the innocent bystander was spared
    finally:
        with contextlib.suppress(Exception):
            bystander.kill()
        reg.close()


def test_reconcile_tears_down_provisioning_slice_before_cascade(tmp_path: Path) -> None:
    """A crash mid-provision leaves a `provisioning` worktree with a write-ahead slice row
    and maybe a half-created external resource. Reconcile must tear that resource down before
    delete_worktree cascades the row away — otherwise the resource leaks with no trace."""
    adapters.register("reconcilefake", _FakeAdapter)  # undone by conftest's registry snapshot
    _FakeAdapter.store.clear()
    reg = Registry.connect(tmp_path / "binky.db")
    try:
        prov = reg.create_worktree("p", "b", "/y", "b", status=WorktreeStatus.provisioning)
        reg.create_slice(prov.id, "db", "reconcilefake")
        _FakeAdapter.store.add(adapters.slice_name("p", "b"))  # resource half-created

        ops.reconcile(reg)

        assert reg.get_worktree("p", "b") is None  # orphan worktree swept
        assert adapters.slice_name("p", "b") not in _FakeAdapter.store  # ...resource torn down
    finally:
        reg.close()


def test_gc_sweeps_stale_provisioning_slice_only(tmp_path: Path) -> None:
    """`gc` tears down + drops a stale `provisioning` slice (write-ahead orphan) while
    leaving `ready` slices — and slices younger than the TTL — untouched."""
    adapters.register("reconcilefake", _FakeAdapter)  # undone by conftest's registry snapshot
    _FakeAdapter.store.clear()
    reg = Registry.connect(tmp_path / "binky.db")
    try:
        wt = reg.create_worktree("p", "a", "/x", "a", status=WorktreeStatus.ready)
        reg.create_slice(wt.id, "db", "reconcilefake")  # provisioning (write-ahead)
        _FakeAdapter.store.add(adapters.slice_name("p", "a"))
        ready = reg.create_slice(wt.id, "cache", "reconcilefake")
        reg.set_slice_ready(ready.id, "fake://cache")  # a healthy slice — must survive

        # a fresh provisioning slice is protected by the TTL (an `up` may be in flight)
        assert ops.gc(reg, {})["count"] == 0
        assert {s.service for s in reg.slices_for(wt.id)} == {"db", "cache"}

        # ttl=0 → the write-ahead row is now fair game
        res = ops.gc(reg, {"ttl": 0})

        assert res["count"] == 1
        assert res["swept"][0]["service"] == "db"
        assert adapters.slice_name("p", "a") not in _FakeAdapter.store  # resource torn down
        remaining = {s.service: s.status for s in reg.slices_for(wt.id)}
        assert "db" not in remaining  # provisioning row swept
        assert remaining.get("cache") == "ready"  # ready slice left intact
    finally:
        reg.close()


def _sleeper() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], **supervisor._new_group_kwargs()
    )


def _tick_until_down(
    procman: supervisor.ProcessManager, wt_id: int, timeout: float = 5.0
) -> supervisor.TickReport:
    """Tick until ``wt_id`` is reported down, the way the real supervisor loop would.

    A single tick right after an *external* kill is racy on Windows: the test still
    holds the victim's ``Popen`` handle, which keeps the just-killed pid readable for
    a beat, so the first ``is_same_process`` check can still read it as alive. The
    daemon ticks on an interval and catches the exit on a later pass — do the same
    here instead of demanding the very first tick sees it.
    """
    deadline = time.monotonic() + timeout
    while True:
        report = procman.tick(time.monotonic())
        if wt_id in report.worktrees_down or time.monotonic() >= deadline:
            return report
        time.sleep(0.05)


def test_reconcile_adopts_a_live_ready_worktree(tmp_path: Path) -> None:
    """C3: a restart that finds a `ready` worktree whose processes are still alive adopts
    them — they keep running, the worktree stays `ready`, and they're now supervised here
    (the old behaviour killed them and dropped the worktree to `stopped`)."""
    reg = Registry.connect(tmp_path / "binky.db")
    procman = supervisor.ProcessManager()
    survivor = _sleeper()
    try:
        wt = reg.create_worktree("p", "a", "/x", "a", status=WorktreeStatus.ready)
        reg.allocate_port(wt.id, "web", "", 4500)
        reg.add_process(wt.id, survivor.pid)

        ops.reconcile(reg, procman)

        a = reg.get_worktree("p", "a")
        assert a is not None
        assert a.status == "ready"  # not dropped to stopped
        time.sleep(0.2)
        assert survivor.poll() is None  # not killed
        assert procman.is_running(wt.id)  # taken under supervision
        assert survivor.pid in procman.pids(wt.id)
        assert reg.processes_for(wt.id) == [survivor.pid]  # its row kept
        assert reg.all_ports() == {4500}  # and its port
    finally:
        procman.stop_all()
        with contextlib.suppress(Exception):
            survivor.kill()
        reg.close()


def test_an_adopted_process_can_be_stopped(tmp_path: Path) -> None:
    """An adopted process must go down through the normal stop path (what down/remove call),
    even though the daemon never owned a Popen for it."""
    reg = Registry.connect(tmp_path / "binky.db")
    procman = supervisor.ProcessManager()
    survivor = _sleeper()
    try:
        wt = reg.create_worktree("p", "a", "/x", "a", status=WorktreeStatus.ready)
        reg.add_process(wt.id, survivor.pid)
        ops.reconcile(reg, procman)
        assert procman.is_running(wt.id)

        assert procman.stop(wt.id) == 1  # one live process killed
        survivor.wait(timeout=5)  # reap it (its real parent is this test process)
        assert survivor.poll() is not None  # the adopted process actually terminated
        assert not procman.is_running(wt.id)
    finally:
        with contextlib.suppress(Exception):
            survivor.kill()
        reg.close()


def test_a_dead_adopted_process_takes_its_worktree_down(tmp_path: Path) -> None:
    """An adopted process carries no restart policy: when it exits, the next tick reports the
    worktree down (cleanly, not `failed`) so its slot and ports can be reclaimed."""
    reg = Registry.connect(tmp_path / "binky.db")
    procman = supervisor.ProcessManager()
    victim = _sleeper()
    try:
        wt = reg.create_worktree("p", "a", "/x", "a", status=WorktreeStatus.ready)
        reg.add_process(wt.id, victim.pid)
        ops.reconcile(reg, procman)
        assert procman.is_running(wt.id)

        victim.kill()
        victim.wait(timeout=5)  # reap, so the identity reads as truly gone
        report = _tick_until_down(procman, wt.id)

        assert wt.id in report.worktrees_down  # reclaimed
        assert wt.id not in report.failed  # a clean down, not a crash-loop give-up
        assert not procman.is_running(wt.id)
    finally:
        with contextlib.suppress(Exception):
            victim.kill()
        reg.close()


def test_reconcile_leaves_stopped_untouched(tmp_path: Path) -> None:
    reg = Registry.connect(tmp_path / "binky.db")
    try:
        wt = reg.create_worktree("p", "a", "/x", "a", status=WorktreeStatus.stopped)
        reg.allocate_port(wt.id, "web", "", 4500)
        ops.reconcile(reg)
        a = reg.get_worktree("p", "a")
        assert a is not None
        assert a.status == "stopped"  # a paused world is never GC'd
        assert reg.all_ports() == {4500}
    finally:
        reg.close()
