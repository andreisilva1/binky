"""HTTPS via a bundled-by-convention Caddy in front of the Python proxy.

The Python proxy handles http and all the dynamic Host routing; when a project asks
for https, Warren puts **Caddy** in front of it to terminate TLS with a local CA
(`tls internal`) — the split we chose (Python for http, Caddy for https). Caddy is
used *if present on PATH*; if it isn't, https is skipped and http still works.

The Caddyfile is a host-agnostic catch-all, so one Caddy covers every worktree's
``*.*.<domain>`` name on demand without per-route config:

    {
        admin off
        auto_https disable_redirects
    }
    https:// {
        tls internal
        reverse_proxy 127.0.0.1:<proxy_port>
    }
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
from pathlib import Path

from warren import paths


def caddy_binary() -> str | None:
    """The Caddy executable on PATH, or ``None`` (``$WARREN_CADDY`` overrides)."""
    return os.environ.get("WARREN_CADDY") or shutil.which("caddy")


def render_caddyfile(proxy_port: int, https_port: int = 443) -> str:
    site = "https://" if https_port == 443 else f"https://:{https_port}"
    return (
        "{\n"
        "\tadmin off\n"
        "\tauto_https disable_redirects\n"
        "}\n\n"
        f"{site} {{\n"
        "\ttls internal\n"
        f"\treverse_proxy 127.0.0.1:{proxy_port}\n"
        "}\n"
    )


class Caddy:
    """Lifecycle for the Caddy TLS front. A no-op (graceful) when Caddy is absent."""

    def __init__(self, proxy_port: int, https_port: int = 443, binary: str | None = None) -> None:
        self.proxy_port = proxy_port
        self.https_port = https_port
        self.binary = binary if binary is not None else caddy_binary()
        self._proc: subprocess.Popen | None = None

    @property
    def available(self) -> bool:
        return bool(self.binary)

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _data_dir(self) -> Path:
        return paths.warren_home() / "caddy"

    def _argv(self, caddyfile: Path) -> list[str]:
        return [str(self.binary), "run", "--config", str(caddyfile), "--adapter", "caddyfile"]

    def start(self) -> bool:
        """Launch Caddy fronting the proxy. Returns True if it was started."""
        if not self.available or self.running:
            return False
        data = self._data_dir()
        data.mkdir(parents=True, exist_ok=True)
        caddyfile = data / "Caddyfile"
        caddyfile.write_text(render_caddyfile(self.proxy_port, self.https_port), encoding="utf-8")
        log = open(data / "caddy.log", "ab")  # noqa: SIM115 — closed in stop()
        # keep Caddy's CA + config under WARREN_HOME instead of the real user profile
        env = {**os.environ, "XDG_DATA_HOME": str(data), "XDG_CONFIG_HOME": str(data)}
        try:
            self._proc = subprocess.Popen(
                self._argv(caddyfile), env=env, stdout=log, stderr=subprocess.STDOUT
            )
        except OSError:
            log.close()
            return False
        self._log = log
        return True

    def stop(self) -> None:
        if self._proc is not None:
            with contextlib.suppress(Exception):
                self._proc.terminate()
                self._proc.wait(timeout=5)
            with contextlib.suppress(Exception):
                self._proc.kill()
            self._proc = None
        with contextlib.suppress(Exception):
            self._log.close()
