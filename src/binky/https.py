"""HTTPS via a bundled-by-convention Caddy in front of the Python proxy.

The Python proxy handles http and all the dynamic Host routing; when a project asks
for https, Binky puts **Caddy** in front of it to terminate TLS with a local CA
(`tls internal`) — the split we chose (Python for http, Caddy for https). Caddy is
used *if present on PATH*; if it isn't, https is skipped and http still works.

The Caddyfile is a host-agnostic catch-all, so one Caddy covers every worktree's
``*.*.<domain>`` name **on demand** without per-route config. Worktree hostnames are
dynamic (a new one every ``up``), so Caddy can't pre-issue their certs — it mints an
internal cert the first time each name is dialled. On-demand issuance is gated by an
``ask`` back to the proxy so Caddy only ever issues for a name the proxy actually
routes (an unknown Host is refused, not silently minted — which also silences Caddy's
"vulnerable to abuse" warning):

    {
        admin off
        auto_https disable_redirects
        on_demand_tls {
            ask http://127.0.0.1:<proxy_port>/__binky/ondemand
        }
    }
    https:// {
        tls internal {
            on_demand
        }
        reverse_proxy 127.0.0.1:<proxy_port>
    }
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
from pathlib import Path

from binky import paths
from binky.procs import NO_WINDOW


def caddy_binary() -> str | None:
    """The Caddy executable on PATH, or ``None`` (``$BINKY_CADDY`` overrides)."""
    return os.environ.get("BINKY_CADDY") or shutil.which("caddy")


#: Path Caddy hits (and the proxy answers) to authorize an on-demand cert. Keeping it
#: here means proxy.py and the Caddyfile can't drift on the sentinel.
ONDEMAND_ASK_PATH = "/__binky/ondemand"


def render_caddyfile(proxy_port: int, https_port: int = 443) -> str:
    site = "https://" if https_port == 443 else f"https://:{https_port}"
    # Worktree hostnames are minted at `up` time, so their certs can't be pre-provisioned:
    # `tls internal { on_demand }` issues on first contact. `on_demand_tls.ask` gates that
    # on the proxy — Caddy only issues for a name the proxy routes, so an unknown SNI is
    # refused instead of silently minted (this is also what silences Caddy's abuse warning).
    return (
        "{\n"
        "\tadmin off\n"
        "\tauto_https disable_redirects\n"
        "\ton_demand_tls {\n"
        f"\t\task http://127.0.0.1:{proxy_port}{ONDEMAND_ASK_PATH}\n"
        "\t}\n"
        "}\n\n"
        f"{site} {{\n"
        "\ttls internal {\n"
        "\t\ton_demand\n"
        "\t}\n"
        f"\treverse_proxy 127.0.0.1:{proxy_port} {{\n"
        # The proxy routes by Host on a connection's FIRST request, then raw-pumps bytes
        # (so WS/SSE/streaming pass through). Caddy pools upstream connections by address,
        # not by Host, so reusing one across worktrees would pin every later request to the
        # first worktree's backend — cross-routing https traffic. Disable upstream keepalive
        # so each request is a fresh connection the proxy routes on its own Host. (Local dev
        # tool; the extra connect is cheap. WS upgrades keep their own dedicated connection.)
        "\t\ttransport http {\n"
        "\t\t\tkeepalive off\n"
        "\t\t}\n"
        "\t}\n"
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
        return paths.binky_home() / "caddy"

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
        # keep Caddy's CA + config under BINKY_HOME instead of the real user profile
        env = {**os.environ, "XDG_DATA_HOME": str(data), "XDG_CONFIG_HOME": str(data)}
        try:
            self._proc = subprocess.Popen(
                self._argv(caddyfile), env=env, stdout=log, stderr=subprocess.STDOUT,
                creationflags=NO_WINDOW,
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
