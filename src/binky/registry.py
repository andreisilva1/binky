"""The machine-global registry — the daemon's source of truth.

SQLite at ``~/.binky/binky.db`` (WAL, foreign keys, busy-timeout). Holds the
*desired state* the reconciler converges against: worktrees, their allocated
ports, and their resource slices. Everything the daemon owns lives here — which
is also what makes teardown ownership-safe: teardown only touches rows
in this table.

Thin, typed data-access layer over stdlib ``sqlite3`` — no ORM.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from binky import paths, procs

SCHEMA_VERSION = 3


def _migrate_2_verify_status(conn: sqlite3.Connection) -> None:
    """v1 → v2: add worktrees.verify_status (idempotent — fresh DBs already have it)."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(worktrees)")}
    if "verify_status" not in columns:
        conn.execute("ALTER TABLE worktrees ADD COLUMN verify_status TEXT")


def _migrate_3_process_identity(conn: sqlite3.Connection) -> None:
    """v2 → v3: add processes.create_time so reconcile can tell a recorded pid from
    a stranger that later inherited its recycled number (idempotent)."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(processes)")}
    if "create_time" not in columns:
        conn.execute("ALTER TABLE processes ADD COLUMN create_time REAL")


# version -> upgrade step. Applied in order for any DB below SCHEMA_VERSION, so a
# Binky upgrade never strands an existing registry. Steps must be idempotent.
_MIGRATIONS = {2: _migrate_2_verify_status, 3: _migrate_3_process_identity}


class WorktreeStatus(StrEnum):
    provisioning = "provisioning"
    ready = "ready"
    stopped = "stopped"  # paused via `binky down` — the reconciler must not GC these
    failed = "failed"  # crash-looped past give-up — distinct from an intentional `down`
    suspended = "suspended"  # paused by the daemon under RAM pressure; resumes itself (C2)


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
    verify_status: str | None = None  # None → never verified; else "passing"/"failing"


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
    verify_status TEXT,
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
    pid         INTEGER NOT NULL,
    create_time REAL
);
"""


def default_db_path() -> Path:
    """``$BINKY_HOME/binky.db`` (default ``~/.binky/binky.db``)."""
    return paths.db_path()


def _now() -> int:
    return int(time.time())


class Registry:
    """Typed access to the SQLite registry. Use :meth:`connect` to open.

    Thread-safe by giving each thread its own connection to the shared WAL
    database (concurrent reads; writes serialized by the engine). This is what
    lets the daemon run ``up``/``down`` on a worker pool instead of funnelling
    every request through one main-thread connection.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._local = threading.local()
        self._conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()

    # -- lifecycle -------------------------------------------------------- #
    @classmethod
    def connect(cls, path: str | Path | None = None) -> Registry:
        db_path = Path(path) if path is not None else default_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        reg = cls(db_path)
        reg._ensure_schema()
        return reg

    def _open(self) -> sqlite3.Connection:
        # autocommit per statement; transaction() groups a set of ops atomically.
        # check_same_thread=False is safe here: connections are thread-local, so
        # each is only *used* by its owner — only close() touches them cross-thread.
        conn = sqlite3.connect(str(self._path), isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        with self._conns_lock:
            self._conns.append(conn)
        return conn

    @property
    def _conn(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._open()
            self._local.conn = conn
        return conn

    def _ensure_schema(self) -> None:
        self._conn.executescript(_SCHEMA)  # base tables (IF NOT EXISTS)
        self._migrate()

    def _migrate(self) -> None:
        current = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if current >= SCHEMA_VERSION:
            return
        if current == 0:  # brand-new DB, created at the latest schema
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            return
        for version in range(current + 1, SCHEMA_VERSION + 1):  # upgrade an older registry
            migrate = _MIGRATIONS.get(version)
            if migrate is not None:
                migrate(self._conn)
            self._conn.execute(f"PRAGMA user_version={version}")

    def close(self) -> None:
        with self._conns_lock:
            conns, self._conns = self._conns, []
        for conn in conns:
            with suppress(Exception):
                conn.close()
        self._local = threading.local()

    def __enter__(self) -> Registry:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Atomic unit of work: commit on success, roll back on error.

        ``BEGIN IMMEDIATE`` takes the write lock up front, so concurrent writers
        serialize (respecting ``busy_timeout``) rather than racing on the same
        snapshot and deadlocking when both try to upgrade a shared lock.
        """
        self._conn.execute("BEGIN IMMEDIATE")
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

    def get_worktree_by_id(self, worktree_id: int) -> Worktree | None:
        row = self._conn.execute(
            "SELECT * FROM worktrees WHERE id=?", (worktree_id,)
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

    def set_verify_status(self, worktree_id: int, verify_status: str) -> None:
        self._conn.execute(
            "UPDATE worktrees SET verify_status=?, updated_at=? WHERE id=?",
            (verify_status, _now(), worktree_id),
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

    def now(self) -> int:
        """The registry's own clock (epoch seconds) — the one `created_at`/`updated_at`
        are stamped with, so age comparisons stay on a single timebase."""
        return _now()

    def stale_provisioning_slices(self, before_ts: int) -> list[Slice]:
        """`provisioning` slices last touched at or before *before_ts* — a write-ahead
        trail (see ops._provision_adapters) left by a crash between recording the intent
        and the resource becoming ready. `gc` sweeps these; the TTL keeps it from racing
        an `up` that is legitimately mid-provision right now."""
        rows = self._conn.execute(
            "SELECT * FROM slices WHERE status=? AND updated_at<=? ORDER BY updated_at",
            (SliceStatus.provisioning.value, before_ts),
        ).fetchall()
        return [_slice(r) for r in rows]

    # -- processes (PIDs, for reconcile after a daemon restart) ------------ #
    def add_process(self, worktree_id: int, pid: int, create_time: float | None = None) -> None:
        # capture the pid's birth time now, while we still know it is *this* process;
        # after a restart it is what lets reconcile avoid killing a recycled pid.
        if create_time is None:
            create_time = procs.process_create_time(pid)
        self._conn.execute(
            "INSERT INTO processes (worktree_id, pid, create_time) VALUES (?, ?, ?)",
            (worktree_id, pid, create_time),
        )

    def processes_for(self, worktree_id: int) -> list[int]:
        rows = self._conn.execute(
            "SELECT pid FROM processes WHERE worktree_id=?", (worktree_id,)
        ).fetchall()
        return [r["pid"] for r in rows]

    def process_identities(self, worktree_id: int) -> list[tuple[int, float | None]]:
        """``(pid, create_time)`` pairs — the identity reconcile checks before a kill."""
        rows = self._conn.execute(
            "SELECT pid, create_time FROM processes WHERE worktree_id=?", (worktree_id,)
        ).fetchall()
        return [(r["pid"], r["create_time"]) for r in rows]

    def clear_processes(self, worktree_id: int) -> None:
        self._conn.execute("DELETE FROM processes WHERE worktree_id=?", (worktree_id,))


def _worktree(row: sqlite3.Row) -> Worktree:
    return Worktree(
        row["id"], row["project"], row["name"], row["path"], row["branch"],
        row["status"], row["created_at"], row["updated_at"], row["verify_status"],
    )


def _port(row: sqlite3.Row) -> Port:
    return Port(row["id"], row["worktree_id"], row["service"], row["label"], row["port"])


def _slice(row: sqlite3.Row) -> Slice:
    return Slice(
        row["id"], row["worktree_id"], row["service"], row["adapter"],
        row["status"], row["address"],
    )
