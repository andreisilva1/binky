"""Creation flags for the child processes the daemon spawns.

The daemon itself runs detached with no console (see ``daemon.start``). On
Windows a console-subsystem child launched from a console-less parent gets a
*new* console allocated for it — which flashes up on screen and vanishes, once
per service start, health probe, git call, adapter probe, and taskkill. Passing
``CREATE_NO_WINDOW`` suppresses that console without detaching the child (so the
supervisor still keeps it in a killable process group). The flag does not exist
off Windows, where ``creationflags=0`` is simply the default and a no-op.
"""

from __future__ import annotations

import subprocess

# 0 everywhere but Windows, so it can be passed (or OR'd) unconditionally.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def process_create_time(pid: int) -> float | None:
    """The process's creation time (epoch seconds), or ``None`` if unreadable.

    A pid on its own is not an identity: once a process exits, the OS is free to
    hand its number to something unrelated. The *(pid, create_time)* pair is —
    the number can be recycled, but a new tenant has a different birth time. We
    return ``None`` when we can't read it (process gone, or psutil unavailable),
    which callers must treat as "not verifiably ours".
    """
    try:
        import psutil

        return psutil.Process(pid).create_time()
    except Exception:
        return None


def is_same_process(pid: int, create_time: float | None) -> bool:
    """True only if *pid* is live *and* was born at *create_time* — i.e. still the
    exact process we recorded, not a stranger that inherited a recycled pid.

    Without a recorded ``create_time`` (``None``) we cannot prove ownership, so we
    answer False: the safe default is to leave an unverifiable pid alone rather
    than risk ``SIGKILL``-ing whatever now happens to hold that number.
    """
    if create_time is None:
        return False
    live = process_create_time(pid)
    # a whole-second slop absorbs float-repr wobble; a recycled pid differs by the
    # entire age of the previous tenant, never by under a second.
    return live is not None and abs(live - create_time) < 1.0
