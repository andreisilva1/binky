"""Tests for the FIFO admission controller (capacity + RAM gate)."""

import asyncio

from binky.admission import AdmissionController


def _run(coro):
    return asyncio.run(coro)


def test_capacity_blocks_then_admits() -> None:
    async def scenario():
        ac = AdmissionController(max_parallel=2, min_free_bytes=0)  # RAM never blocks
        t1 = await ac.acquire()
        await ac.acquire()  # now at capacity (2 of 2)
        third = asyncio.ensure_future(ac.acquire())
        await asyncio.sleep(0.05)
        assert not third.done()  # queued behind capacity
        ac.release(t1)  # free one slot
        await asyncio.wait_for(third, timeout=1)  # third is admitted

    _run(scenario())


def test_fifo_order() -> None:
    async def scenario():
        ac = AdmissionController(max_parallel=1, min_free_bytes=0)
        first = await ac.acquire()
        order: list[int] = []

        async def waiter(n: int) -> None:
            token = await ac.acquire()
            order.append(n)
            ac.release(token)

        w1 = asyncio.ensure_future(waiter(1))
        await asyncio.sleep(0.01)
        w2 = asyncio.ensure_future(waiter(2))
        await asyncio.sleep(0.01)
        ac.release(first)  # admits w1, which releases and lets w2 in
        await asyncio.wait_for(asyncio.gather(w1, w2), timeout=1)
        assert order == [1, 2]

    _run(scenario())


def test_ram_gate() -> None:
    async def scenario():
        ac = AdmissionController(max_parallel=10, min_free_bytes=10**18)  # impossible RAM req
        waiter = asyncio.ensure_future(ac.acquire())
        await asyncio.sleep(0.05)
        assert not waiter.done()  # blocked on RAM, not capacity
        ac.min_free_bytes = 0
        ac.pump()
        await asyncio.wait_for(waiter, timeout=1)  # now admitted

    _run(scenario())


def test_set_max_parallel_admits() -> None:
    async def scenario():
        ac = AdmissionController(max_parallel=1, min_free_bytes=0)
        await ac.acquire()  # fills the single slot
        waiter = asyncio.ensure_future(ac.acquire())
        await asyncio.sleep(0.05)
        assert not waiter.done()
        ac.set_max_parallel(2)  # raise the cap
        await asyncio.wait_for(waiter, timeout=1)

    _run(scenario())


def test_rejection_only_fires_when_the_wait_could_never_end() -> None:
    """A resource floor with nothing running is unsatisfiable → an actionable message.
    A count block, or a floor while something runs, legitimately queues → None."""
    # nothing reserved + an impossible disk floor → refuse, with a message that says why
    ac = AdmissionController(max_parallel=10, min_free_bytes=0, min_free_disk_bytes=10**18)
    msg = ac.rejection()
    assert msg is not None
    assert "disk" in msg
    assert "binky reserve --disk" in msg  # points at the knob

    # admittable → no refusal
    ac.min_free_disk_bytes = 0
    assert ac.rejection() is None


def test_rejection_is_silent_while_something_runs() -> None:
    """With a slot held, a resource shortfall will re-pump on that worktree's release,
    so it queues rather than refusing — release is the event that can free the resource."""

    async def scenario():
        ac = AdmissionController(max_parallel=10, min_free_bytes=0, min_free_disk_bytes=0)
        await ac.acquire()  # hold a slot
        ac.min_free_disk_bytes = 10**18  # now impossible, but something is running
        assert ac.rejection() is None  # queues instead of refusing

    _run(scenario())


def test_disk_gate() -> None:
    """A disk reserve blocks admission independently of RAM and capacity: with an
    impossible free-disk floor nothing is admitted; drop the floor and it flows."""

    async def scenario():
        # RAM and capacity are wide open, so only the disk floor can block here.
        ac = AdmissionController(
            max_parallel=10, min_free_bytes=0, min_free_disk_bytes=10**18
        )
        waiter = asyncio.ensure_future(ac.acquire())
        await asyncio.sleep(0.05)
        assert not waiter.done()  # blocked on disk
        ac.min_free_disk_bytes = 0  # 0 disables the disk gate
        ac.pump()
        await asyncio.wait_for(waiter, timeout=1)  # now admitted

    _run(scenario())
