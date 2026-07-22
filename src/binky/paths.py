"""Filesystem locations for Binky's machine-global state (``~/.binky``)."""

from __future__ import annotations

import contextlib
import hashlib
import os
import stat
from pathlib import Path

# `sun_path` holds 108 bytes on Linux and 104 on macOS/BSD, one of which is the
# terminating NUL. Stay well under the smaller of the two rather than probing.
_MAX_SOCKET_PATH = 100


def binky_home() -> Path:
    """``$BINKY_HOME`` if set, else ``~/.binky``."""
    override = os.environ.get("BINKY_HOME")
    return Path(override) if override else Path.home() / ".binky"


def db_path() -> Path:
    return binky_home() / "binky.db"


def daemon_info_path() -> Path:
    """Endpoint + token + pid of the running daemon (also serves as the pidfile)."""
    return binky_home() / "daemon.json"


def daemon_log_path() -> Path:
    """Whatever the daemon wrote to stderr while starting up.

    Truncated on every spawn: it answers "why didn't it come up *this* time",
    which is the only question worth asking of it.
    """
    return binky_home() / "daemon.err"


def socket_path() -> Path:
    """Unix-domain socket path (Unix only; Windows uses a loopback TCP port).

    ``$BINKY_HOME/binky.sock`` whenever it fits in ``sun_path``, which is the
    ordinary case — ``~/.binky/binky.sock`` is nowhere near the limit. It stops
    fitting when BINKY_HOME is deep, and a macOS ``$TMPDIR`` is already ~50 bytes
    before anything is appended to it. ``bind`` then fails with a bare
    "AF_UNIX path too long" and the daemon dies before it can say anything
    useful, so fall back to a short directory of our own instead.
    """
    preferred = binky_home() / "binky.sock"
    if os.name == "nt" or len(str(preferred).encode()) <= _MAX_SOCKET_PATH:
        return preferred
    return _short_socket_dir() / "binky.sock"


def _short_socket_dir() -> Path:
    """A private directory under ``/tmp``, keyed to BINKY_HOME so two homes never
    share a socket.

    ``/tmp`` is shared, so the directory is verified rather than trusted: if it
    is not a real directory that we own with mode 0700, someone else put it
    there, and binding the control socket inside it would hand the daemon to
    them. The check is not atomic with the creation — on the single-user
    workstation Binky is built for (see SECURITY.md) the ownership test is the
    part that matters.
    """
    digest = hashlib.sha256(str(binky_home().resolve()).encode()).hexdigest()[:8]
    path = Path("/tmp") / f"binky-{os.getuid()}-{digest}"
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):  # chmod ignores umask; mkdir's mode does not
        os.chmod(path, 0o700)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise RuntimeError(
            f"{path} is not a private directory owned by this user; refusing to put "
            "the daemon socket there"
        )
    return path


def worktrees_dir() -> Path:
    """Where per-project git worktrees live: ``$BINKY_HOME/worktrees``."""
    return binky_home() / "worktrees"


def logs_dir() -> Path:
    """Where per-service logs are captured: ``$BINKY_HOME/logs``."""
    return binky_home() / "logs"
