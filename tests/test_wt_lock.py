"""The per-worktree lock (C10): two ops on the *same* worktree serialize, while ops on
*different* worktrees run in parallel.

The daemon keys one ``asyncio.Lock`` per ``(root, worktree)`` (``Daemon._wt_lock``), which is
what lets a slow ``up``/``reload`` on one worktree run without blocking anything on another —
the property the fire-test observed live but that nothing pinned down. These tests hold a lock
open and prove, by whether a second coroutine can enter its critical section, that a different
worktree is admitted concurrently while the same worktree is made to wait.
"""

import asyncio

import pytest

from binky import daemon, settings


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("BINKY_HOME", str(tmp_path))
    settings.save(settings.Settings(proxy_port=0, https_port=0))  # don't bind :80 in tests


def test_same_worktree_returns_one_lock_distinct_worktrees_get_distinct_locks() -> None:
    d = daemon.Daemon()
    a1 = d._wt_lock("/root", "alpha")
    a2 = d._wt_lock("/root", "alpha")
    b = d._wt_lock("/root", "beta")
    other_root = d._wt_lock("/other", "alpha")

    assert a1 is a2  # same worktree -> same lock -> serialized
    assert a1 is not b  # different worktree, same root -> parallel
    assert a1 is not other_root  # same name under a different root is a different worktree


def test_different_worktrees_do_not_block_each_other() -> None:
    """Two ops on different worktrees can hold their locks at the same time."""

    async def scenario() -> None:
        d = daemon.Daemon()
        release = asyncio.Event()
        inside_a = asyncio.Event()
        inside_b = asyncio.Event()

        async def hold(worktree: str, inside: asyncio.Event) -> None:
            async with d._wt_lock("/root", worktree):
                inside.set()
                await release.wait()  # keep the lock until the test lets go

        ta = asyncio.create_task(hold("alpha", inside_a))
        tb = asyncio.create_task(hold("beta", inside_b))
        # If beta were forced to wait on alpha's lock, this would time out.
        await asyncio.wait_for(inside_a.wait(), timeout=1.0)
        await asyncio.wait_for(inside_b.wait(), timeout=1.0)
        release.set()
        await asyncio.gather(ta, tb)

    asyncio.run(scenario())


def test_same_worktree_serializes() -> None:
    """A second op on the same worktree cannot enter until the first releases the lock."""

    async def scenario() -> None:
        d = daemon.Daemon()
        release = asyncio.Event()
        first_inside = asyncio.Event()
        second_inside = asyncio.Event()

        async def first() -> None:
            async with d._wt_lock("/root", "alpha"):
                first_inside.set()
                await release.wait()

        async def second() -> None:
            await first_inside.wait()  # ensure `first` holds the lock before we try
            async with d._wt_lock("/root", "alpha"):
                second_inside.set()

        t1 = asyncio.create_task(first())
        t2 = asyncio.create_task(second())
        await asyncio.wait_for(first_inside.wait(), timeout=1.0)
        # The same-worktree lock is held, so `second` must not get in yet.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(second_inside.wait()), timeout=0.2)
        release.set()  # let `first` out; now `second` may proceed
        await asyncio.gather(t1, t2)
        assert second_inside.is_set()

    asyncio.run(scenario())
