"""Resource adapters — carve an isolated *slice* of a shared resource per worktree.

An adapter speaks three verbs over the slice (never the connection, never the
shared server), plus a capability query:

- ``provision(slice)`` — create the slice (e.g. ``CREATE DATABASE``)
- ``resolve(slice)``   — return its address (the connection string) -> ``${db.url}``
- ``teardown(slice)``  — destroy the slice (e.g. ``DROP DATABASE ... WITH (FORCE)``)
- ``capabilities()``   — what the adapter supports (e.g. ``{"clone": true}``)

Two tiers implement the *same* contract:

- **in-process** (this file) — first-party adapters shipped in the repo as Python
  classes, discovered by name. Lowest friction to contribute; tested in CI.
- **external** (:class:`ExternalAdapter`) — any-language executables the community
  writes without patching Binky, driven over a one-shot subprocess + JSON protocol.
  A Python author gets the protocol for free via ``binky.adapter_sdk``.

Discovery: a service's ``adapter`` name is looked up in the in-process registry
first; if absent, Binky looks for ``binky-adapter-<name>`` on ``PATH``.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from typing import ClassVar
from urllib.parse import urlparse, urlunparse

from binky.procs import NO_WINDOW


# Bound every adapter's admin connect. libpq (and most drivers) default to an unbounded
# wait, so a server that is down — or a socket that accepts the TCP handshake but never
# speaks the protocol — would hang `up` indefinitely, stranding the worktree in
# `provisioning`. This ceiling turns that into a fast, clean AdapterError instead.
CONNECT_TIMEOUT = 10  # seconds


class AdapterError(Exception):
    """An adapter operation failed (bad config, driver missing, resource error)."""


class Adapter(ABC):
    # what this adapter supports; consumed by features like snapshots (``clone``)
    CAPABILITIES: ClassVar[dict] = {}

    def __init__(self, config: dict) -> None:
        self.config = config

    @abstractmethod
    def provision(self, slice_name: str) -> None: ...

    @abstractmethod
    def resolve(self, slice_name: str) -> str: ...

    @abstractmethod
    def teardown(self, slice_name: str) -> None: ...

    def clone(self, source_slice: str, dest_slice: str) -> None:
        """Copy *source_slice* into a new *dest_slice* (opt-in; gated by ``clone``)."""
        raise AdapterError(f"{type(self).__name__} does not support clone")

    def capabilities(self) -> dict:
        return dict(self.CAPABILITIES)


# --------------------------------------------------------------------------- #
#  external tier — the JSON protocol
# --------------------------------------------------------------------------- #
# Binky invokes ``<binary> <op>`` (op ∈ provision|resolve|teardown|capabilities),
# writes a JSON request on stdin, and reads a JSON response on stdout:
#   request:  {"slice": "<slice>", "config": {...}}
#   response: {"ok": true, ...}  |  {"ok": false, "error": "<msg>"}
#             resolve adds "address"; capabilities adds "capabilities".
class ExternalAdapter(Adapter):
    """Drive an out-of-process adapter executable over the subprocess+JSON protocol."""

    def __init__(self, config: dict, command: list[str]) -> None:
        super().__init__(config)
        self._command = command

    def _call(self, op: str, **fields: str) -> dict:
        request: dict = {"config": self.config, **fields}
        try:
            proc = subprocess.run(
                [*self._command, op],
                input=json.dumps(request),
                capture_output=True,
                text=True,
                check=False,
                creationflags=NO_WINDOW,
            )
        except OSError as exc:
            raise AdapterError(f"could not run adapter {self._command}: {exc}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            raise AdapterError(f"adapter '{op}' failed: {detail}")
        try:
            resp = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError as exc:
            snippet = proc.stdout[:200]
            raise AdapterError(f"adapter '{op}' returned invalid JSON: {snippet!r}") from exc
        if not resp.get("ok"):
            raise AdapterError(resp.get("error") or f"adapter '{op}' failed")
        return resp

    def provision(self, slice_name: str) -> None:
        self._call("provision", slice=slice_name)

    def resolve(self, slice_name: str) -> str:
        return self._call("resolve", slice=slice_name)["address"]

    def teardown(self, slice_name: str) -> None:
        self._call("teardown", slice=slice_name)

    def clone(self, source_slice: str, dest_slice: str) -> None:
        self._call("clone", source=source_slice, dest=dest_slice)

    def capabilities(self) -> dict:
        return self._call("capabilities").get("capabilities", {})


# --------------------------------------------------------------------------- #
#  registry + discovery
# --------------------------------------------------------------------------- #
_REGISTRY: dict[str, type[Adapter]] = {}

# First-party adapters living in `builtin_adapters`. They are imported on first use,
# not at import time, so `import binky` stays free of boto3/pymongo/... and a missing
# extra fails where the user can act on it — with the exact `pip install` to run.
# Several names map to one class: those backends are protocol-compatible forks or
# share an API, so the slice and the code are genuinely identical.
_LAZY: dict[str, str] = {
    "redis": "RedisAdapter",
    "valkey": "RedisAdapter",
    "mysql": "MySQLAdapter",
    "mariadb": "MySQLAdapter",
    "percona": "MySQLAdapter",
    "mongodb": "MongoAdapter",
    "s3": "S3Adapter",
    "minio": "S3Adapter",
    "rabbitmq": "RabbitMQAdapter",
    "kafka": "KafkaAdapter",
    "redpanda": "KafkaAdapter",
    "qdrant": "QdrantAdapter",
    "clickhouse": "ClickHouseAdapter",
    "elasticsearch": "ElasticsearchAdapter",
    "opensearch": "ElasticsearchAdapter",
}


def register(name: str, cls: type[Adapter]) -> None:
    _REGISTRY[name] = cls


def available() -> list[str]:
    """Every adapter name Binky can resolve without leaving the process."""
    return sorted(set(_REGISTRY) | set(_LAZY))


def _load(name: str) -> type[Adapter] | None:
    cls = _REGISTRY.get(name)
    if cls is not None:
        return cls
    attr = _LAZY.get(name)
    if attr is None:
        return None
    module = importlib.import_module("binky.builtin_adapters")
    cls = getattr(module, attr)
    _REGISTRY[name] = cls
    return cls


def get(spec: str, config: dict) -> Adapter:
    """Resolve an adapter spec like ``postgres@16`` to an instance (version ignored).

    In-process first-party adapters win; otherwise an external
    ``binky-adapter-<name>`` executable on PATH is driven over the JSON protocol.
    """
    name = spec.split("@", 1)[0]
    cls = _load(name)
    if cls is not None:
        return cls(config)
    binary = shutil.which(f"binky-adapter-{name}")
    if binary:
        return ExternalAdapter(config, [binary])
    raise AdapterError(f"unknown adapter: {name}")


def slice_name(project: str, worktree: str) -> str:
    """A safe, stable slice identifier, e.g. ``acme_login_fix``."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", f"{project}_{worktree}").lower()


# --------------------------------------------------------------------------- #
#  in-process first-party adapters
# --------------------------------------------------------------------------- #
def _with_dbname(url: str, dbname: str) -> str:
    return urlunparse(urlparse(url)._replace(path=f"/{dbname}"))


class PostgresAdapter(Adapter):
    """One database per worktree, carved out of the user's own Postgres server."""

    CAPABILITIES: ClassVar[dict] = {"clone": True}

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.admin_url = config.get("admin_url") or os.environ.get("BINKY_DB_ADMIN_URL")
        if not self.admin_url:
            raise AdapterError("postgres adapter needs an admin_url (or $BINKY_DB_ADMIN_URL)")

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover — exercised only without the extra installed
            raise AdapterError("psycopg not installed — `pip install 'binky[postgres]'`") from exc
        # Bound the connect (see CONNECT_TIMEOUT) unless the admin URL already sets one, and
        # translate a connection failure into a clean AdapterError — otherwise a raw
        # psycopg.OperationalError slips past `_provision_adapters`' `except AdapterError` and
        # surfaces as an opaque error instead of "could not connect to postgres: …".
        kwargs: dict = {"autocommit": True}
        if "connect_timeout" not in self.admin_url:
            kwargs["connect_timeout"] = CONNECT_TIMEOUT
        try:
            return psycopg.connect(self.admin_url, **kwargs)
        except psycopg.OperationalError as exc:
            raise AdapterError(f"could not connect to postgres: {exc}".strip()) from exc

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

    def clone(self, source_slice: str, dest_slice: str) -> None:
        # file-level copy of the whole database — ~constant time regardless of size.
        # Requires no active connections to the template (agents use their own slices).
        with self._connect() as conn:
            conn.execute(f'CREATE DATABASE "{dest_slice}" TEMPLATE "{source_slice}"')


register("postgres", PostgresAdapter)
