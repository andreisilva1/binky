"""Filesystem locations for Binky's machine-global state (``~/.binky``)."""

from __future__ import annotations

import os
from pathlib import Path


def binky_home() -> Path:
    """``$BINKY_HOME`` if set, else ``~/.binky``."""
    override = os.environ.get("BINKY_HOME")
    return Path(override) if override else Path.home() / ".binky"


def db_path() -> Path:
    return binky_home() / "binky.db"


def daemon_info_path() -> Path:
    """Endpoint + token + pid of the running daemon (also serves as the pidfile)."""
    return binky_home() / "daemon.json"


def socket_path() -> Path:
    """Unix-domain socket path (Unix only; Windows uses a loopback TCP port)."""
    return binky_home() / "binky.sock"


def worktrees_dir() -> Path:
    """Where per-project git worktrees live: ``$BINKY_HOME/worktrees``."""
    return binky_home() / "worktrees"


def logs_dir() -> Path:
    """Where per-service logs are captured: ``$BINKY_HOME/logs``."""
    return binky_home() / "logs"
