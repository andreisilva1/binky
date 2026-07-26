"""Machine-global settings — the durable, singular knobs the daemon reads at boot.

These are properties of *the machine*, not of any one project: how much RAM and disk Binky
holds in reserve, and which ports the proxy fronts. A singleton daemon can't take them from
the environment — the value would bind to whoever happened to launch it first — so they live
in a small JSON file next to the registry, the same spirit as ``daemon.json``. Zero-config
still holds: every key has a built-in default and the file only appears once something is
changed. Ports are read at boot (a live re-bind can come later); reserves are read live on
every admission check.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import asdict, dataclass, fields

from binky import paths

RESERVE_MIN = 5  # never let the RAM reserve drop below this % — some headroom is always kept
RESERVE_MAX = 50  # nor above this — past half the machine, Binky would starve itself
DISK_RESERVE_MAX_GB = 1024  # a sane ceiling for the disk headroom, to catch fat-finger typos


class SettingsError(ValueError):
    """A settings value was rejected (out of range, or an unknown key)."""


@dataclass
class Settings:
    # RAM is a *percentage* of total kept free (leave the OS a fraction — Binky "sees" that
    # much less). Disk is a *fixed* headroom in GB, not a percentage: reserving a fraction of
    # a large disk makes the floor balloon past what's free even when there's plenty of usable
    # space, so what matters for disk is simply "never get within N GB of full".
    ram_reserve_pct: int = 20  # keep this % of total RAM free
    disk_reserve_gb: int = 5  # always keep at least this many GB free (0 disables the gate)
    proxy_port: int = 80  # HTTP hostname proxy (0 disables it)
    https_port: int = 443  # TLS front via Caddy


def _path():
    return paths.binky_home() / "settings.json"


def load() -> Settings:
    """Read the store, falling back to the built-in default for anything missing or bad.

    Deliberately forgiving: a corrupt or partial file must never stop the daemon from
    booting — it just means the defaults apply until the value is set again.
    """
    s = Settings()
    known = {f.name for f in fields(s)}
    with contextlib.suppress(OSError, json.JSONDecodeError, TypeError, AttributeError):
        raw = json.loads(_path().read_text(encoding="utf-8"))
        for key, value in raw.items():
            if key in known and isinstance(value, int) and not isinstance(value, bool):
                setattr(s, key, value)
    return s


def _validate(s: Settings) -> None:
    if not (RESERVE_MIN <= s.ram_reserve_pct <= RESERVE_MAX):
        raise SettingsError(
            f"ram_reserve_pct must be between {RESERVE_MIN} and {RESERVE_MAX} "
            f"(got {s.ram_reserve_pct})"
        )
    if not (0 <= s.disk_reserve_gb <= DISK_RESERVE_MAX_GB):
        raise SettingsError(
            f"disk_reserve_gb must be between 0 and {DISK_RESERVE_MAX_GB} "
            f"(got {s.disk_reserve_gb})"
        )
    for key in ("proxy_port", "https_port"):
        value = getattr(s, key)
        if not (0 <= value <= 65535):
            raise SettingsError(f"{key} must be between 0 and 65535 (got {value})")


def save(s: Settings) -> None:
    """Validate and persist the whole settings object (atomic-enough: one write)."""
    _validate(s)
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(s), indent=2) + "\n", encoding="utf-8")


def update(**changes: int) -> Settings:
    """Apply a partial change over the current store and return the new state."""
    s = load()
    known = {f.name for f in fields(s)}
    for key, value in changes.items():
        if key not in known:
            raise SettingsError(f"unknown setting: {key}")
        setattr(s, key, int(value))
    save(s)
    return s
