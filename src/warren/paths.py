"""Filesystem locations for Warren's machine-global state (``~/.warren``)."""

from __future__ import annotations

import os
from pathlib import Path


def warren_home() -> Path:
    """``$WARREN_HOME`` if set, else ``~/.warren``."""
    override = os.environ.get("WARREN_HOME")
    return Path(override) if override else Path.home() / ".warren"


def db_path() -> Path:
    return warren_home() / "warren.db"


def daemon_info_path() -> Path:
    """Endpoint + token + pid of the running daemon (also serves as the pidfile)."""
    return warren_home() / "daemon.json"


def socket_path() -> Path:
    """Unix-domain socket path (Unix only; Windows uses a loopback TCP port)."""
    return warren_home() / "warren.sock"


def worktrees_dir() -> Path:
    """Where per-project git worktrees live: ``$WARREN_HOME/worktrees``."""
    return warren_home() / "worktrees"


def logs_dir() -> Path:
    """Where per-service logs are captured: ``$WARREN_HOME/logs``."""
    return warren_home() / "logs"
