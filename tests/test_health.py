"""Health checks — the wait loop's liveness shortcut.

check_once itself is exercised end-to-end by the up/down integration tests; here
we pin the one behaviour that is otherwise expensive to observe: wait_healthy must
not burn the whole timeout on a service whose process has already died.
"""

import time

from binky import health


def test_wait_healthy_bails_the_moment_the_process_dies(monkeypatch) -> None:
    # a check that never passes + a process reported dead: we should give up at once,
    # not poll a corpse for the full (here: 10s) timeout.
    monkeypatch.setattr(health, "check_once", lambda *a, **k: False)

    start = time.monotonic()
    ok = health.wait_healthy("web", None, {}, "wt", ".", timeout=10.0, alive=lambda: False)
    elapsed = time.monotonic() - start

    assert ok is False
    assert elapsed < 1.0  # returned immediately, didn't wait out the 10s


def test_wait_healthy_without_liveness_still_honours_the_deadline(monkeypatch) -> None:
    # no liveness callback (the old signature): the deadline is the only exit.
    monkeypatch.setattr(health, "check_once", lambda *a, **k: False)

    ok = health.wait_healthy("web", None, {}, "wt", ".", timeout=0.1)

    assert ok is False


def test_wait_healthy_returns_true_as_soon_as_it_passes(monkeypatch) -> None:
    # a live-but-not-yet-ready service that flips healthy shouldn't be cut short.
    calls = {"n": 0}

    def flips_healthy(*_a, **_k) -> bool:
        calls["n"] += 1
        return calls["n"] >= 2

    monkeypatch.setattr(health, "check_once", flips_healthy)

    ok = health.wait_healthy("web", None, {}, "wt", ".", timeout=10.0, alive=lambda: True)

    assert ok is True
