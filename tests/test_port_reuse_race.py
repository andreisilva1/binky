"""Regression: a reload must not mistake its own just-stopped service for a squatter.

`reload` is down+up under one worktree lock. On POSIX a service is stopped with SIGTERM
and shuts down gracefully, so for a sliver it still holds the listening socket `up` is
about to reuse — and connections it just served sit in TIME_WAIT on that port. The reused-
port guard (C8) used to bind-probe once, without SO_REUSEADDR, and fail instantly: a normal
reload surfaced as "another process took it". Two fixes, pinned here:

* ``_host_free`` probes with SO_REUSEADDR on POSIX (as every server binds), so a port merely
  in TIME_WAIT reads as free — while an *active listener* (a real squatter) still reads bound.
* ``wait_free`` waits a short beat, so our own draining service releasing the port lets ``up``
  proceed, while a genuine squatter that stays bound past the window still fails.
"""

import socket
import threading
import time

from binky import allocator


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _listener(port: int) -> socket.socket:
    """A socket actively LISTENing on *port* — stands in for a real external squatter."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen()
    return s


def test_an_active_listener_still_reads_as_bound_on_every_os() -> None:
    # The SO_REUSEADDR probe must not blind us to a genuine conflict: a socket actively
    # listening on the port is a real squatter on Windows and POSIX alike. (This also guards
    # that SO_REUSEADDR stays OFF on Windows, where it would let two sockets share the port.)
    port = _free_port()
    squatter = _listener(port)
    try:
        assert allocator._host_free(port) is False
        assert allocator.is_bound(port) is True
    finally:
        squatter.close()


def test_a_free_port_settles_without_waiting() -> None:
    port = _free_port()
    started = time.monotonic()
    assert allocator.wait_free(port, timeout=3.0) is True
    assert time.monotonic() - started < 0.5  # a free port is not made to wait


def test_a_persistent_squatter_still_fails_after_the_window() -> None:
    port = _free_port()
    squatter = _listener(port)
    try:
        started = time.monotonic()
        assert allocator.wait_free(port, timeout=0.4) is False
        assert time.monotonic() - started >= 0.4  # gave the full window before giving up
    finally:
        squatter.close()


def test_a_service_releasing_mid_wait_lets_up_proceed() -> None:
    # The reload case: the port is held now (our own service still closing its socket) but
    # freed shortly after. wait_free must notice the release and return True — not fail as if
    # an outside process had taken it.
    port = _free_port()
    ours = _listener(port)

    def _release_soon() -> None:
        time.sleep(0.3)
        ours.close()

    threading.Thread(target=_release_soon, daemon=True).start()
    assert allocator.wait_free(port, timeout=3.0) is True
