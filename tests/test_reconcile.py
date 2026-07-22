"""Tests for the startup reconciler (orphan cleanup + status correction)."""

import contextlib
import subprocess
import sys
from pathlib import Path

from binky import ops, supervisor
from binky.registry import Registry, WorktreeStatus


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
