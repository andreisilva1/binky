"""Admission control.

Bounds how many worktrees run at once so a swarm of agents doesn't melt the
machine. A FIFO queue: an ``up`` waits its turn and is admitted only when there
is capacity — fewer than ``max_parallel`` worktrees running **and** enough free
RAM **and** enough free disk. ``max_parallel`` defaults from the machine's RAM
(never a hardcoded constant). The two floors are *reserves* Binky keeps free, but
they use different models: RAM is a **percentage** of total (leave the OS a
fraction — Binky "sees" that much less), while disk is a **fixed headroom** in
bytes (a percentage of a large disk would balloon past what's free even when
there is plenty of usable space; what matters for disk is never getting within a
few GB of full). We key on memory, not CPU — death is by OOM.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import os
import shutil
from pathlib import Path

import psutil

from binky import paths, settings

# Fallback RAM floor used only when the machine's total can't be read (see make_default);
# in the normal path the RAM floor is a percentage of total RAM from the settings store.
_MIN_FREE_FLOOR = 512 * 1024 * 1024

_GIB = 1024**3  # the disk headroom is stored in GB; convert to bytes with this


def default_max_parallel() -> int:
    total_gb = psutil.virtual_memory().total / (1024**3)
    cores = os.cpu_count() or 4
    return max(2, min(int(total_gb // 2), cores))


def _free_ram() -> int:
    return psutil.virtual_memory().available


def _total_ram() -> int:
    return psutil.virtual_memory().total


def _disk_anchor() -> Path:
    """The nearest existing ancestor of ``BINKY_HOME`` — a path ``disk_usage`` can stat.

    Binky can only honestly measure the disk *it* writes to (``BINKY_HOME`` + the worktree
    checkouts under it); an external DB on another volume is out of scope for the gate."""
    path = paths.binky_home()
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def _free_disk() -> int:
    return shutil.disk_usage(_disk_anchor()).free


def _pct(total: int, pct: int) -> int:
    return int(total * pct / 100)


def _gb(nbytes: int) -> str:
    return f"{nbytes / _GIB:.1f} GB"


class AdmissionController:
    """A FIFO semaphore gated on a worktree count, live free RAM, and live free disk."""

    def __init__(
        self,
        max_parallel: int,
        min_free_bytes: int = _MIN_FREE_FLOOR,
        min_free_disk_bytes: int = 0,
    ) -> None:
        self.max_parallel = max_parallel
        self.min_free_bytes = min_free_bytes  # RAM reserve, in bytes
        self.min_free_disk_bytes = min_free_disk_bytes  # disk reserve, in bytes (0 → no gate)
        self._reserved: set[int] = set()
        self._waiters: collections.deque[tuple[int, asyncio.Future]] = collections.deque()
        self._next = 0

    def _can_admit(self) -> bool:
        if len(self._reserved) >= self.max_parallel:
            return False  # count cap — the avalanche guard
        if _free_ram() < self.min_free_bytes:
            return False  # would eat into the RAM reserve
        return not (self.min_free_disk_bytes and _free_disk() < self.min_free_disk_bytes)

    def rejection(self) -> str | None:
        """A clear message when admission can't succeed *and can't self-resolve*, else None.

        A *count* block clears the moment a running worktree goes down, so it legitimately
        queues (the "fifteen at once" wait) — no message. But a *resource* floor with nothing
        running (``_reserved`` empty) will never free on its own: there is no teardown coming
        to re-pump the queue, so the caller would wait out its whole RPC timeout in silence.
        That is the case we turn into an immediate, actionable refusal instead."""
        if self._reserved or self._can_admit():
            return None  # something running will re-pump on release, or we can admit right now
        free_ram = _free_ram()
        if free_ram < self.min_free_bytes:
            return (
                f"not enough free RAM to start: keeping {_gb(self.min_free_bytes)} in reserve, "
                f"only {_gb(free_ram)} free. Free some up, or lower it: `binky reserve --ram`."
            )
        free_disk = _free_disk()
        if self.min_free_disk_bytes and free_disk < self.min_free_disk_bytes:
            return (
                f"not enough free disk to start: keeping {_gb(self.min_free_disk_bytes)} in "
                f"reserve, only {_gb(free_disk)} free. Free some space, or lower it: "
                f"`binky reserve --disk`."
            )
        return None

    def under_pressure(self) -> bool:
        """Is live free RAM already below the reserve floor?

        Admission only gates *new* ups on this; a worktree that was admitted when RAM
        was fine can still grow the machine into the reserve afterwards. The daemon polls
        this to decide when to evict (C2). ``0`` floor (RAM gate disabled) is never
        pressure."""
        return bool(self.min_free_bytes) and _free_ram() < self.min_free_bytes

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

    def set_reserves(self, ram_pct: int, disk_gb: int) -> None:
        """Update the RAM/disk reserves live (a settings change), then re-pump the queue.

        RAM is a percentage of total; disk is a fixed headroom in GB.
        """
        with contextlib.suppress(Exception):
            self.min_free_bytes = _pct(_total_ram(), ram_pct)
        self.min_free_disk_bytes = max(0, disk_gb) * _GIB
        self.pump()

    @property
    def in_use(self) -> int:
        return len(self._reserved)

    @property
    def queued(self) -> int:
        return len(self._waiters)


def make_default(store: settings.Settings | None = None) -> AdmissionController:
    """Build the controller from the machine's totals and the persisted reserves.

    Zero-config: with no settings file, the store defaults (20% RAM / 5 GB disk) apply.
    The disk headroom is a fixed byte count, so it needs no machine total; only the RAM
    floor does, and if that total can't be read we fall back to a conservative fixed floor.
    """
    store = store or settings.load()
    disk_floor = max(0, store.disk_reserve_gb) * _GIB
    with contextlib.suppress(Exception):
        return AdmissionController(
            default_max_parallel(),
            min_free_bytes=_pct(_total_ram(), store.ram_reserve_pct),
            min_free_disk_bytes=disk_floor,
        )
    return AdmissionController(4, min_free_disk_bytes=disk_floor)
