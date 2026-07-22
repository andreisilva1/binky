"""Structured event log + point-in-time metrics for the daemon.

Two cheap, high-value observability surfaces (no metrics-server slop):

- ``log_event`` appends one JSON object per line to ``$BINKY_HOME/events.log`` —
  greppable and ingestible by any log pipeline.
- ``collect_metrics`` returns a point-in-time snapshot for ``binky metrics`` /
  ``binky doctor``.
"""

from __future__ import annotations

import contextlib
import json
import time
from collections import Counter

from binky import paths

# roll events.log at this size, keeping one previous generation (events.log.1)
MAX_EVENTS_BYTES = 5 * 1024 * 1024


def events_path():
    return paths.binky_home() / "events.log"


def _rotate_if_big(path) -> None:
    with contextlib.suppress(OSError):
        if path.exists() and path.stat().st_size >= MAX_EVENTS_BYTES:
            backup = path.parent / (path.name + ".1")
            with contextlib.suppress(OSError):
                backup.unlink()
            path.rename(backup)


def log_event(event: str, **fields: object) -> None:
    """Append one structured event as a JSON line (best-effort; never raises)."""
    record = {"ts": round(time.time(), 3), "event": event, **fields}
    with contextlib.suppress(Exception):
        path = events_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_big(path)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")


def collect_metrics(reg, procman, admission) -> dict:
    """A point-in-time snapshot: worktrees, ports, slices, verify verdicts, admission."""
    worktrees = reg.list_worktrees()
    by_status = Counter(w.status for w in worktrees)
    verdicts = Counter(w.verify_status for w in worktrees if w.verify_status)
    running = sum(1 for w in worktrees if procman.is_running(w.id))
    return {
        "worktrees": {
            "total": len(worktrees),
            "running": running,
            "ready": by_status.get("ready", 0),
            "stopped": by_status.get("stopped", 0),
            "provisioning": by_status.get("provisioning", 0),
        },
        "ports_allocated": len(reg.all_ports()),
        "slices": len(reg.list_slices()),
        "verify": {"passing": verdicts.get("passing", 0), "failing": verdicts.get("failing", 0)},
        "admission": {
            "capacity": admission.max_parallel,
            "in_use": admission.in_use,
            "queued": admission.queued,
        },
    }
