"""Admission control (THESIS §8.6, §8.7).

Bounds how many worktrees run at once so a swarm of agents doesn't melt the
machine. A FIFO queue: an ``up`` waits its turn and is admitted only when there
is capacity — fewer than ``max_parallel`` worktrees running **and** enough free
RAM. ``max_parallel`` defaults from the machine's RAM (never a hardcoded
constant) and is overridable per project. We key on memory, not CPU — death is
by OOM (§8.7).
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import os

import psutil

_MIN_FREE_FLOOR = 512 * 1024 * 1024  # never admit with less than this free


def default_max_parallel() -> int:
    total_gb = psutil.virtual_memory().total / (1024**3)
    cores = os.cpu_count() or 4
    return max(2, min(int(total_gb // 2), cores))


def _free_ram() -> int:
    return psutil.virtual_memory().available


class AdmissionController:
    """A FIFO semaphore gated on a worktree count and live free RAM."""

    def __init__(self, max_parallel: int, min_free_bytes: int = _MIN_FREE_FLOOR) -> None:
        self.max_parallel = max_parallel
        self.min_free_bytes = min_free_bytes
        self._reserved: set[int] = set()
        self._waiters: collections.deque[tuple[int, asyncio.Future]] = collections.deque()
        self._next = 0

    def _can_admit(self) -> bool:
        return len(self._reserved) < self.max_parallel and _free_ram() >= self.min_free_bytes

    def pump(self) -> None:
        """Admit as many queued waiters as capacity allows, in FIFO order."""
        while self._waiters and self._can_admit():
            token, fut = self._waiters.popleft()
            if not fut.done():
                self._reserved.add(token)
                fut.set_result(token)

    async def acquire(self) -> int:
        """Wait for a slot; returns a token to pass back to :meth:`release`."""
        self._next += 1
        token = self._next
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters.append((token, fut))
        self.pump()
        try:
            return await fut
        except asyncio.CancelledError:
            self._waiters = collections.deque(w for w in self._waiters if w[0] != token)
            self._reserved.discard(token)
            raise

    def release(self, token: int) -> None:
        self._reserved.discard(token)
        self.pump()

    def set_max_parallel(self, value: int) -> None:
        self.max_parallel = max(1, value)
        self.pump()


def make_default() -> AdmissionController:
    with contextlib.suppress(Exception):
        return AdmissionController(default_max_parallel())
    return AdmissionController(4)
