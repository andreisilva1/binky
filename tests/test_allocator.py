"""Tests for the port allocator (deterministic, collision-proof)."""

import socket

import pytest

from warren import allocator
from warren.registry import Registry


@pytest.fixture
def reg(tmp_path):
    r = Registry.connect(tmp_path / "warren.db")
    yield r
    r.close()


def test_allocates_within_range_and_records(reg: Registry) -> None:
    wt = reg.create_worktree("acme", "a", "/x", "a")
    result = allocator.allocate(
        reg, wt.id, "acme", "a", [("web", ""), ("api", "http")], port_range=(4000, 4100)
    )
    assert len(result) == 2
    assert all(4000 <= p <= 4100 for p in result.values())
    assert reg.all_ports() == set(result.values())


def test_deterministic_same_port_when_free(reg: Registry) -> None:
    wt = reg.create_worktree("acme", "a", "/x", "a")
    first = allocator.allocate(reg, wt.id, "acme", "a", [("web", "")])
    reg.release_ports(wt.id)
    second = allocator.allocate(reg, wt.id, "acme", "a", [("web", "")])
    assert first == second


def test_fallback_when_base_is_used(reg: Registry) -> None:
    other = reg.create_worktree("acme", "other", "/x", "o")
    wt = reg.create_worktree("acme", "b", "/x", "b")
    lo, hi = 4000, 4100
    base = lo + allocator._offset("acme:b:web:", hi - lo + 1)
    reg.allocate_port(other.id, "x", "", base)  # occupy b's deterministic port

    result = allocator.allocate(reg, wt.id, "acme", "b", [("web", "")], port_range=(lo, hi))
    assert result[("web", "")] != base
    assert lo <= result[("web", "")] <= hi


def test_skips_host_bound_port(reg: Registry) -> None:
    wt = reg.create_worktree("acme", "a", "/x", "a")
    lo, hi = 4000, 4100
    base = lo + allocator._offset("acme:a:web:", hi - lo + 1)
    sock = socket.socket()
    sock.bind(("127.0.0.1", base))
    try:
        result = allocator.allocate(reg, wt.id, "acme", "a", [("web", "")], port_range=(lo, hi))
        assert result[("web", "")] != base
    finally:
        sock.close()


def test_exhausted_range_raises(reg: Registry) -> None:
    a = reg.create_worktree("acme", "a", "/x", "a")
    reg.allocate_port(a.id, "x", "", 4000)
    b = reg.create_worktree("acme", "b", "/x", "b")
    with pytest.raises(allocator.NoFreePort):
        allocator.allocate(reg, b.id, "acme", "b", [("web", "")], port_range=(4000, 4000))
