"""Port allocation — resolve-then-launch.

Ports are assigned *before* anything starts, by the single owner of the
registry, so collisions are impossible by construction. Allocation is:

- **deterministic with fallback** — a port derived from ``hash(project:worktree:
  service:label)`` (stable across restarts), falling forward to the next free one;
- **collision-proof** — skips ports already in the registry (Binky-internal) *and*
  ports currently bound on the host (external), then records the choice.

The whole set for a worktree is allocated in one registry transaction.
"""

from __future__ import annotations

import hashlib
import socket

from binky.registry import Registry

DEFAULT_RANGE = (4000, 4999)

# a request for one port: (service, label). label "" is the service's primary port.
PortRequest = tuple[str, str]


class NoFreePort(Exception):
    """The configured port range is exhausted."""


def _offset(key: str, span: int) -> int:
    digest = hashlib.sha256(key.encode()).digest()
    return int.from_bytes(digest[:4], "big") % span


def _host_free(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def is_bound(port: int) -> bool:
    """True if *port* is currently bound on the loopback host — someone is listening.

    Used to re-validate a *reused* port at ``up`` time (C8): a port Binky recorded can be
    grabbed by an outside process while the worktree is down, and reusing it blindly would
    just crash-loop the service. Freshly allocated ports don't need this — allocation already
    skips bound ports."""
    return not _host_free(port)


def allocate(
    reg: Registry,
    worktree_id: int,
    project: str,
    worktree: str,
    requests: list[PortRequest],
    port_range: tuple[int, int] | None = None,
) -> dict[PortRequest, int]:
    """Allocate every requested port atomically. Returns ``{(service, label): port}``."""
    lo, hi = port_range or DEFAULT_RANGE
    span = hi - lo + 1
    result: dict[PortRequest, int] = {}
    with reg.transaction():
        used = reg.all_ports()  # machine-global snapshot
        for service, label in requests:
            base = lo + _offset(f"{project}:{worktree}:{service}:{label}", span)
            chosen: int | None = None
            for i in range(span):
                cand = lo + (base - lo + i) % span
                if cand in used or not _host_free(cand):
                    continue
                chosen = cand
                break
            if chosen is None:
                raise NoFreePort(f"no free port in {lo}-{hi} for {service}:{label}")
            reg.allocate_port(worktree_id, service, label, chosen)
            used.add(chosen)
            result[(service, label)] = chosen
    return result
