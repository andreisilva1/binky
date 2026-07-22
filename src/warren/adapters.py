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
  writes without patching Warren, driven over a one-shot subprocess + JSON protocol.
  A Python author gets the protocol for free via ``warren.adapter_sdk``.

Discovery: a service's ``adapter`` name is looked up in the in-process registry
first; if absent, Warren looks for ``warren-adapter-<name>`` on ``PATH``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from typing import ClassVar
from urllib.parse import urlparse, urlunparse


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
# Warren invokes ``<binary> <op>`` (op ∈ provision|resolve|teardown|capabilities),
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


def register(name: str, cls: type[Adapter]) -> None:
    _REGISTRY[name] = cls


def get(spec: str, config: dict) -> Adapter:
    """Resolve an adapter spec like ``postgres@16`` to an instance (version ignored).

    In-process first-party adapters win; otherwise an external
    ``warren-adapter-<name>`` executable on PATH is driven over the JSON protocol.
    """
    name = spec.split("@", 1)[0]
    cls = _REGISTRY.get(name)
    if cls is not None:
        return cls(config)
    binary = shutil.which(f"warren-adapter-{name}")
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

    def clone(self, source_slice: str, dest_slice: str) -> None:
        # file-level copy of the whole database — ~constant time regardless of size.
        # Requires no active connections to the template (agents use their own slices).
        with self._connect() as conn:
            conn.execute(f'CREATE DATABASE "{dest_slice}" TEMPLATE "{source_slice}"')


register("postgres", PostgresAdapter)
