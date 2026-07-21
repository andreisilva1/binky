"""Concurrency: the registry and allocator are safe under parallel workers.

The daemon runs `up`/`down` on a thread pool, so these lower layers are hit from
many threads at once. These tests pin down the two properties that make that safe:
writes don't corrupt, and port allocation never hands out the same port twice.
"""

import threading
from pathlib import Path

from warren import allocator
from warren.registry import Registry, WorktreeStatus


def _run_all(target, args_list: list[tuple]) -> list:
    """Run *target* once per args tuple, each on its own thread; return exceptions."""
    errors: list = []

    def wrap(args: tuple) -> None:
        try:
            target(*args)
        except Exception as exc:  # surfacing any failure is the whole point
            errors.append(exc)

    threads = [threading.Thread(target=wrap, args=(a,)) for a in args_list]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def test_registry_concurrent_writes(tmp_path: Path) -> None:
    """Many threads writing at once (own connection each) never corrupt or lose rows."""
    reg = Registry.connect(tmp_path / "warren.db")
    n = 24

    def worker(i: int) -> None:
        wt = reg.create_worktree("proj", f"wt{i}", f"/path/{i}", f"branch{i}")
        reg.add_process(wt.id, 10_000 + i)
        reg.set_worktree_status(wt.id, WorktreeStatus.ready)

    errors = _run_all(worker, [(i,) for i in range(n)])
    assert not errors, errors

    worktrees = reg.list_worktrees("proj")
    assert len(worktrees) == n
    assert all(w.status == WorktreeStatus.ready for w in worktrees)
    reg.close()


def test_allocator_no_collision_under_contention(tmp_path: Path) -> None:
    """Parallel allocations against one registry never hand out a duplicate port.

    This is what `BEGIN IMMEDIATE` buys: writers serialize, so each allocation sees
    the ports committed before it. A plain deferred `BEGIN` would race the snapshot
    (and deadlock on lock upgrade) instead.
    """
    reg = Registry.connect(tmp_path / "warren.db")
    workers = [
        (reg.create_worktree("proj", f"wt{i}", f"/p/{i}", f"b{i}").id, f"wt{i}")
        for i in range(16)
    ]

    got: dict[str, int] = {}
    got_lock = threading.Lock()

    def worker(worktree_id: int, name: str) -> None:
        res = allocator.allocate(reg, worktree_id, "proj", name, [(name, "")], (4000, 4200))
        with got_lock:
            got[name] = res[(name, "")]

    errors = _run_all(worker, workers)
    assert not errors, errors

    ports = list(got.values())
    assert len(ports) == len(workers)
    assert len(set(ports)) == len(ports)  # every port distinct → collision-free
    reg.close()
