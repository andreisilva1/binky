"""The machine-global registry — the daemon's source of truth.

SQLite at ``~/.warren/warren.db`` (WAL, foreign keys, busy-timeout). Holds the
*desired state* the reconciler converges against: worktrees, their allocated
ports, and their resource slices. Everything the daemon owns lives here — which
is also what makes teardown ownership-safe: teardown only touches rows
in this table.

Thin, typed data-access layer over stdlib ``sqlite3`` — no ORM.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from warren import paths

SCHEMA_VERSION = 1


class WorktreeStatus(StrEnum):
    provisioning = "provisioning"
    ready = "ready"
    stopped = "stopped"  # paused via `warren down` — the reconciler must not GC these


class SliceStatus(StrEnum):
    provisioning = "provisioning"
    ready = "ready"


@dataclass(frozen=True)
class Worktree:
    id: int
    project: str
    name: str
    path: str
    branch: str
    status: str
    created_at: int
    updated_at: int


@dataclass(frozen=True)
class Port:
    id: int
    worktree_id: int
    service: str
    label: str
    port: int


@dataclass(frozen=True)
class Slice:
    id: int
    worktree_id: int
    service: str
    adapter: str
    status: str
    address: str | None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS worktrees (
    id         INTEGER PRIMARY KEY,
    project    TEXT NOT NULL,
    name       TEXT NOT NULL,
    path       TEXT NOT NULL,
    branch     TEXT NOT NULL,
    status     TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE (project, name)
);

CREATE TABLE IF NOT EXISTS ports (
    id          INTEGER PRIMARY KEY,
    worktree_id INTEGER NOT NULL REFERENCES worktrees(id) ON DELETE CASCADE,
    service     TEXT NOT NULL,
    label       TEXT NOT NULL DEFAULT '',
    port        INTEGER NOT NULL UNIQUE,
    UNIQUE (worktree_id, service, label)
);

CREATE TABLE IF NOT EXISTS slices (
    id          INTEGER PRIMARY KEY,
    worktree_id INTEGER NOT NULL REFERENCES worktrees(id) ON DELETE CASCADE,
    service     TEXT NOT NULL,
    adapter     TEXT NOT NULL,
    status      TEXT NOT NULL,
    address     TEXT,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL,
    UNIQUE (worktree_id, service)
);

CREATE TABLE IF NOT EXISTS processes (
    id          INTEGER PRIMARY KEY,
    worktree_id INTEGER NOT NULL REFERENCES worktrees(id) ON DELETE CASCADE,
    pid         INTEGER NOT NULL
);
"""


def default_db_path() -> Path:
    """``$WARREN_HOME/warren.db`` (default ``~/.warren/warren.db``)."""
    return paths.db_path()


def _now() -> int:
    return int(time.time())


class Registry:
    """Typed access to the SQLite registry. Use :meth:`connect` to open."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # -- lifecycle -------------------------------------------------------- #
    @classmethod
    def connect(cls, path: str | Path | None = None) -> Registry:
        db_path = Path(path) if path is not None else default_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # autocommit per statement; transaction() groups a set of ops atomically.
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        reg = cls(conn)
        reg._ensure_schema()
        return reg

    def _ensure_schema(self) -> None:
        self._conn.executescript(_SCHEMA)
        self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Registry:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Atomic unit of work: commit on success, roll back on error."""
        self._conn.execute("BEGIN")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # -- worktrees -------------------------------------------------------- #
    def create_worktree(
        self,
        project: str,
        name: str,
        path: str,
        branch: str,
        status: WorktreeStatus = WorktreeStatus.provisioning,
    ) -> Worktree:
        ts = _now()
        cur = self._conn.execute(
            "INSERT INTO worktrees (project, name, path, branch, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (project, name, path, branch, status.value, ts, ts),
        )
        return Worktree(cur.lastrowid, project, name, path, branch, status.value, ts, ts)

    def get_worktree(self, project: str, name: str) -> Worktree | None:
        row = self._conn.execute(
            "SELECT * FROM worktrees WHERE project=? AND name=?", (project, name)
        ).fetchone()
        return _worktree(row) if row else None

    def list_worktrees(self, project: str | None = None) -> list[Worktree]:
        if project is None:
            rows = self._conn.execute("SELECT * FROM worktrees ORDER BY project, name").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM worktrees WHERE project=? ORDER BY name", (project,)
            ).fetchall()
        return [_worktree(r) for r in rows]

    def set_worktree_status(self, worktree_id: int, status: WorktreeStatus) -> None:
        self._conn.execute(
            "UPDATE worktrees SET status=?, updated_at=? WHERE id=?",
            (status.value, _now(), worktree_id),
        )

    def delete_worktree(self, worktree_id: int) -> None:
        # cascades to ports + slices
        self._conn.execute("DELETE FROM worktrees WHERE id=?", (worktree_id,))

    # -- ports ------------------------------------------------------------ #
    def allocate_port(self, worktree_id: int, service: str, label: str, port: int) -> Port:
        """Record a port assignment. Raises on collision (UNIQUE port)."""
        cur = self._conn.execute(
            "INSERT INTO ports (worktree_id, service, label, port) VALUES (?, ?, ?, ?)",
            (worktree_id, service, label, port),
        )
        return Port(cur.lastrowid, worktree_id, service, label, port)

    def ports_for(self, worktree_id: int) -> list[Port]:
        rows = self._conn.execute(
            "SELECT * FROM ports WHERE worktree_id=? ORDER BY service, label", (worktree_id,)
        ).fetchall()
        return [_port(r) for r in rows]

    def all_ports(self) -> set[int]:
        rows = self._conn.execute("SELECT port FROM ports").fetchall()
        return {r["port"] for r in rows}

    def release_ports(self, worktree_id: int) -> None:
        self._conn.execute("DELETE FROM ports WHERE worktree_id=?", (worktree_id,))

    # -- slices ----------------------------------------------------------- #
    def create_slice(
        self,
        worktree_id: int,
        service: str,
        adapter: str,
        status: SliceStatus = SliceStatus.provisioning,
    ) -> Slice:
        ts = _now()
        cur = self._conn.execute(
            "INSERT INTO slices (worktree_id, service, adapter, status, address, created_at,"
            " updated_at) VALUES (?, ?, ?, ?, NULL, ?, ?)",
            (worktree_id, service, adapter, status.value, ts, ts),
        )
        return Slice(cur.lastrowid, worktree_id, service, adapter, status.value, None)

    def set_slice_ready(self, slice_id: int, address: str) -> None:
        self._conn.execute(
            "UPDATE slices SET status=?, address=?, updated_at=? WHERE id=?",
            (SliceStatus.ready.value, address, _now(), slice_id),
        )

    def slices_for(self, worktree_id: int) -> list[Slice]:
        rows = self._conn.execute(
            "SELECT * FROM slices WHERE worktree_id=? ORDER BY service", (worktree_id,)
        ).fetchall()
        return [_slice(r) for r in rows]

    def list_slices(self, status: SliceStatus | None = None) -> list[Slice]:
        if status is None:
            rows = self._conn.execute("SELECT * FROM slices").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM slices WHERE status=?", (status.value,)
            ).fetchall()
        return [_slice(r) for r in rows]

    def delete_slice(self, slice_id: int) -> None:
        self._conn.execute("DELETE FROM slices WHERE id=?", (slice_id,))

    # -- processes (PIDs, for reconcile after a daemon restart) ------------ #
    def add_process(self, worktree_id: int, pid: int) -> None:
        self._conn.execute(
            "INSERT INTO processes (worktree_id, pid) VALUES (?, ?)", (worktree_id, pid)
        )

    def processes_for(self, worktree_id: int) -> list[int]:
        rows = self._conn.execute(
            "SELECT pid FROM processes WHERE worktree_id=?", (worktree_id,)
        ).fetchall()
        return [r["pid"] for r in rows]

    def clear_processes(self, worktree_id: int) -> None:
        self._conn.execute("DELETE FROM processes WHERE worktree_id=?", (worktree_id,))


def _worktree(row: sqlite3.Row) -> Worktree:
    return Worktree(
        row["id"], row["project"], row["name"], row["path"], row["branch"],
        row["status"], row["created_at"], row["updated_at"],
    )


def _port(row: sqlite3.Row) -> Port:
    return Port(row["id"], row["worktree_id"], row["service"], row["label"], row["port"])


def _slice(row: sqlite3.Row) -> Slice:
    return Slice(
        row["id"], row["worktree_id"], row["service"], row["adapter"],
        row["status"], row["address"],
    )
