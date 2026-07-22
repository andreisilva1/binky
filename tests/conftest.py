"""Shared fixtures."""

from __future__ import annotations

import pytest

from warren import adapters


@pytest.fixture(autouse=True)
def _isolate_adapter_registry():
    """Undo any adapter a test registers.

    ``adapters._REGISTRY`` is module-global and also doubles as the lazy-import
    cache, so a fake registered by one test stays visible to every test that runs
    after it — including tests that ask the registry what Warren actually ships.
    Snapshotting it here keeps that leak from becoming a load-order puzzle.
    """
    saved = dict(adapters._REGISTRY)
    yield
    adapters._REGISTRY.clear()
    adapters._REGISTRY.update(saved)
