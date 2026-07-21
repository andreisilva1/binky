"""Tests for the FIFO admission controller (capacity + RAM gate)."""

import asyncio

from warren.admission import AdmissionController


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
