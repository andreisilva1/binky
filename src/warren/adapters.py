"""Resource adapters (THESIS §8.2) — the in-process draft of the contract.

An adapter carves an isolated *slice* of a shared resource for a worktree and
cleans it up, via three verbs over the slice (never the connection, never the
shared server):

- ``provision(slice)`` — create the slice (e.g. ``CREATE DATABASE``)
- ``resolve(slice)``   — return its address (the connection string) -> ``${db.url}``
- ``teardown(slice)``  — destroy the slice (e.g. ``DROP DATABASE ... WITH (FORCE)``)

This is the in-process prototype; it becomes the external JSON protocol + SDK
once the shape is proven (THESIS §8.2). Postgres is the first adapter; the user
supplies their own server via ``admin_url`` — Warren never runs one.
"""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from urllib.parse import urlparse, urlunparse


class AdapterError(Exception):
    """An adapter operation failed (bad config, driver missing, resource error)."""


class Adapter(ABC):
    def __init__(self, config: dict) -> None:
        self.config = config

    @abstractmethod
    def provision(self, slice_name: str) -> None: ...

    @abstractmethod
    def resolve(self, slice_name: str) -> str: ...

    @abstractmethod
    def teardown(self, slice_name: str) -> None: ...


_REGISTRY: dict[str, type[Adapter]] = {}


def register(name: str, cls: type[Adapter]) -> None:
    _REGISTRY[name] = cls


def get(spec: str, config: dict) -> Adapter:
    """Instantiate the adapter for a spec like ``postgres@16`` (version ignored for now)."""
    name = spec.split("@", 1)[0]
    cls = _REGISTRY.get(name)
    if cls is None:
        raise AdapterError(f"unknown adapter: {name}")
    return cls(config)


def slice_name(project: str, worktree: str) -> str:
    """A safe, stable slice identifier, e.g. ``acme_login_fix``."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", f"{project}_{worktree}").lower()


def _with_dbname(url: str, dbname: str) -> str:
    return urlunparse(urlparse(url)._replace(path=f"/{dbname}"))


class PostgresAdapter(Adapter):
    """One database per worktree, carved out of the user's own Postgres server."""

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.admin_url = config.get("admin_url") or os.environ.get("WARREN_DB_ADMIN_URL")
        if not self.admin_url:
            raise AdapterError("postgres adapter needs an admin_url (or $WARREN_DB_ADMIN_URL)")

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover — exercised only without the extra installed
            raise AdapterError("psycopg not installed — `pip install 'warren[postgres]'`") from exc
        return psycopg.connect(self.admin_url, autocommit=True)

    def provision(self, slice_name: str) -> None:
        with self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (slice_name,)
            ).fetchone()
            if not exists:
                conn.execute(f'CREATE DATABASE "{slice_name}"')

    def resolve(self, slice_name: str) -> str:
        return _with_dbname(self.admin_url, slice_name)

    def teardown(self, slice_name: str) -> None:
        with self._connect() as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{slice_name}" WITH (FORCE)')


register("postgres", PostgresAdapter)
