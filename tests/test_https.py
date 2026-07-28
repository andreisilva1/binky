"""HTTPS front: Caddyfile rendering, PATH detection, graceful absence, and the
process lifecycle (driven with a fake binary so it needs no real Caddy)."""

import shutil
import sys
from pathlib import Path

from binky import https
from binky.https import Caddy, caddy_binary, render_caddyfile


def test_render_caddyfile_default_and_custom_port() -> None:
    default = render_caddyfile(8080)
    assert "https:// {" in default
    assert "tls internal" in default
    assert "reverse_proxy 127.0.0.1:8080" in default

    custom = render_caddyfile(8080, https_port=8443)
    assert "https://:8443 {" in custom


def test_render_caddyfile_enables_on_demand_tls() -> None:
    # Worktree hostnames are minted at `up`, so their certs must be issued on demand;
    # without `on_demand` Caddy has no cert for a dynamic SNI and aborts the handshake
    # (the bug the fire test surfaced). The ask must point back at the proxy so only
    # routable names get a cert — regression-lock both.
    rendered = render_caddyfile(8080)
    assert "on_demand" in rendered  # tls internal { on_demand }
    assert f"ask http://127.0.0.1:8080{https.ONDEMAND_ASK_PATH}" in rendered


def test_render_caddyfile_disables_upstream_keepalive() -> None:
    # The proxy routes by Host on a connection's first request then raw-pumps. Caddy pools
    # upstream connections by address, not Host, so reusing one across worktrees pins every
    # later request to the first worktree's backend — cross-routing https traffic. Keepalive
    # must be off so each request is a fresh connection the proxy routes on its own Host.
    rendered = render_caddyfile(8080)
    assert "keepalive off" in rendered


def test_caddy_binary_env_override(monkeypatch) -> None:
    monkeypatch.setenv("BINKY_CADDY", "/opt/caddy")
    assert caddy_binary() == "/opt/caddy"
    monkeypatch.delenv("BINKY_CADDY", raising=False)
    monkeypatch.setattr(
        https.shutil, "which", lambda name: "/usr/bin/caddy" if name == "caddy" else None
    )
    assert caddy_binary() == "/usr/bin/caddy"


def test_absent_caddy_is_graceful(monkeypatch) -> None:
    monkeypatch.delenv("BINKY_CADDY", raising=False)
    monkeypatch.setattr(https.shutil, "which", lambda name: None)
    caddy = Caddy(8080)
    assert not caddy.available
    assert caddy.start() is False  # https simply skipped; http still works
    assert not caddy.running


class _FakeCaddy(Caddy):
    """A Caddy whose binary is just a long-lived process (no real TLS)."""

    def __init__(self, script: str) -> None:
        super().__init__(8080, https_port=8443, binary=sys.executable)
        self._script = script

    def _argv(self, caddyfile: Path) -> list[str]:
        return [sys.executable, self._script]


def test_caddy_lifecycle(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BINKY_HOME", str(tmp_path / "home"))
    script = tmp_path / "sleep.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")

    caddy = _FakeCaddy(str(script))
    assert caddy.available
    assert caddy.start() is True
    assert caddy.running
    assert caddy.start() is False  # idempotent while running

    # the Caddyfile was materialized under BINKY_HOME
    assert (tmp_path / "home" / "caddy" / "Caddyfile").exists()

    caddy.stop()
    assert not caddy.running


# Real end-to-end only where Caddy is actually installed.
import pytest  # noqa: E402

_HAS_CADDY = shutil.which("caddy") is not None


@pytest.mark.skipif(not _HAS_CADDY, reason="install Caddy on PATH to run the real TLS front")
def test_real_caddy_starts(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BINKY_HOME", str(tmp_path / "home"))
    caddy = Caddy(8080, https_port=8443)
    try:
        assert caddy.start() is True
        assert caddy.running
    finally:
        caddy.stop()
