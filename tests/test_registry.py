"""Tests for the SQLite registry (schema + DAO)."""

import sqlite3
from pathlib import Path

import pytest

from warren.registry import Registry, SliceStatus, WorktreeStatus


@pytest.fixture
def reg(tmp_path: Path) -> Registry:
    r = Registry.connect(tmp_path / "warren.db")
    yield r
    r.close()


def test_wal_and_schema_version(reg: Registry) -> None:
    mode = reg._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"
    ver = reg._conn.execute("PRAGMA user_version").fetchone()[0]
    assert ver == 1


def test_create_and_get_worktree(reg: Registry) -> None:
    wt = reg.create_worktree("acme", "login-fix", "/wt/login-fix", "login-fix")
    assert wt.id > 0
    assert wt.status == WorktreeStatus.provisioning.value

    fetched = reg.get_worktree("acme", "login-fix")
    assert fetched is not None
    assert fetched.id == wt.id
    assert reg.get_worktree("acme", "nope") is None


def test_status_transitions(reg: Registry) -> None:
    wt = reg.create_worktree("acme", "a", "/wt/a", "a")
    reg.set_worktree_status(wt.id, WorktreeStatus.ready)
    assert reg.get_worktree("acme", "a").status == "ready"
    reg.set_worktree_status(wt.id, WorktreeStatus.stopped)
    assert reg.get_worktree("acme", "a").status == "stopped"


def test_unique_worktree_per_project(reg: Registry) -> None:
    reg.create_worktree("acme", "a", "/wt/a", "a")
    with pytest.raises(sqlite3.IntegrityError):
        reg.create_worktree("acme", "a", "/wt/a2", "a")
    # same name in a different project is fine
    reg.create_worktree("other", "a", "/wt/a3", "a")


def test_list_worktrees_by_project(reg: Registry) -> None:
    reg.create_worktree("acme", "a", "/x", "a")
    reg.create_worktree("acme", "b", "/x", "b")
    reg.create_worktree("other", "c", "/x", "c")
    assert {w.name for w in reg.list_worktrees("acme")} == {"a", "b"}
    assert len(reg.list_worktrees()) == 3


def test_ports_allocation_and_uniqueness(reg: Registry) -> None:
    a = reg.create_worktree("acme", "a", "/x", "a")
    b = reg.create_worktree("acme", "b", "/x", "b")
    reg.allocate_port(a.id, "web", "", 4110)
    reg.allocate_port(a.id, "api", "http", 4111)
    assert reg.all_ports() == {4110, 4111}
    assert {p.port for p in reg.ports_for(a.id)} == {4110, 4111}

    # a port is machine-global: no other worktree can reuse it
    with pytest.raises(sqlite3.IntegrityError):
        reg.allocate_port(b.id, "web", "", 4110)


def test_release_ports(reg: Registry) -> None:
    a = reg.create_worktree("acme", "a", "/x", "a")
    reg.allocate_port(a.id, "web", "", 4110)
    reg.release_ports(a.id)
    assert reg.all_ports() == set()


def test_slices(reg: Registry) -> None:
    a = reg.create_worktree("acme", "a", "/x", "a")
    sl = reg.create_slice(a.id, "db", "postgres@16")
    assert sl.status == SliceStatus.provisioning.value
    assert sl.address is None

    reg.set_slice_ready(sl.id, "postgres://localhost/acme_a")
    got = reg.slices_for(a.id)[0]
    assert got.status == "ready"
    assert got.address == "postgres://localhost/acme_a"
    assert len(reg.list_slices(SliceStatus.provisioning)) == 0
    assert len(reg.list_slices(SliceStatus.ready)) == 1


def test_delete_worktree_cascades(reg: Registry) -> None:
    a = reg.create_worktree("acme", "a", "/x", "a")
    reg.allocate_port(a.id, "web", "", 4110)
    reg.create_slice(a.id, "db", "postgres@16")
    reg.delete_worktree(a.id)
    assert reg.get_worktree("acme", "a") is None
    assert reg.all_ports() == set()
    assert reg.list_slices() == []


def test_transaction_rolls_back(reg: Registry) -> None:
    a = reg.create_worktree("acme", "a", "/x", "a")
    with pytest.raises(RuntimeError), reg.transaction():
        reg.allocate_port(a.id, "web", "", 4110)
        raise RuntimeError("boom")
    assert reg.all_ports() == set()  # the whole unit rolled back


def test_persistence_across_reopen(tmp_path: Path) -> None:
    db = tmp_path / "warren.db"
    r1 = Registry.connect(db)
    r1.create_worktree("acme", "a", "/x", "a")
    r1.close()

    r2 = Registry.connect(db)
    assert r2.get_worktree("acme", "a") is not None
    r2.close()


def test_two_connections_see_committed_writes(tmp_path: Path) -> None:
    db = tmp_path / "warren.db"
    r1 = Registry.connect(db)
    r2 = Registry.connect(db)
    r1.create_worktree("acme", "a", "/x", "a")  # autocommitted
    assert r2.get_worktree("acme", "a") is not None
    r1.close()
    r2.close()


def test_default_db_path_respects_warren_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WARREN_HOME", str(tmp_path / "home"))
    from warren.registry import default_db_path

    assert default_db_path() == tmp_path / "home" / "warren.db"
