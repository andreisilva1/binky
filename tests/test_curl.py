"""`binky curl` — resolve a service's proxy route and dial it with the right Host header.

Two layers: ``ops.resolve_route`` (pure resolution against config + registry) and the CLI
command end-to-end against a *fake* proxy — a throwaway HTTP server standing in for Binky's
proxy, so the whole request/Host/body/flags path is proven without a real worktree or database.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from typer.testing import CliRunner

from binky import cli, ops
from binky.cli import app
from binky.registry import Registry
from binky.rpc import RpcFault

runner = CliRunner()


def _make_project(tmp_path):
    """A project with a url'd ``api`` service, a url'd ``web``, and a no-url ``db`` adapter."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "binky.toml").write_text(
        '[project]\nname = "acme"\n'
        '[services.db]\nadapter = "postgres"\nadmin_url = "postgres://x"\n'
        '[services.api]\ncmd = "run"\nurl = "api"\n'
        '[services.web]\ncmd = "run"\nurl = "web"\n',
        encoding="utf-8",
    )
    return proj


# --- ops.resolve_route ---------------------------------------------------------------- #


def test_resolve_route_returns_proxy_host_and_direct_port(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BINKY_HOME", str(tmp_path / "home"))
    proj = _make_project(tmp_path)
    reg = Registry.connect(tmp_path / "home" / "binky.db")
    try:
        wt = reg.create_worktree("acme", "feat", str(proj), "feat")
        reg.allocate_port(wt.id, "api", "", 4783)

        got = ops.resolve_route(reg, {"root": str(proj), "worktree": "feat", "service": "api"})
        assert got["host"] == "api.feat.acme.localhost"  # <sub>.<worktree>.<domain>
        assert got["scheme"] == "http"
        assert got["direct_port"] == 4783
    finally:
        reg.close()


def test_resolve_route_rejects_a_service_with_no_url(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BINKY_HOME", str(tmp_path / "home"))
    proj = _make_project(tmp_path)
    reg = Registry.connect(tmp_path / "home" / "binky.db")
    try:
        reg.create_worktree("acme", "feat", str(proj), "feat")
        with pytest.raises(RpcFault) as excinfo:
            ops.resolve_route(reg, {"root": str(proj), "worktree": "feat", "service": "db"})
        assert excinfo.value.code == "no_route"
    finally:
        reg.close()


def test_resolve_route_unknown_service_and_worktree_are_not_found(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BINKY_HOME", str(tmp_path / "home"))
    proj = _make_project(tmp_path)
    reg = Registry.connect(tmp_path / "home" / "binky.db")
    try:
        reg.create_worktree("acme", "feat", str(proj), "feat")
        with pytest.raises(RpcFault) as no_svc:
            ops.resolve_route(reg, {"root": str(proj), "worktree": "feat", "service": "ghost"})
        assert no_svc.value.code == "not_found"
        with pytest.raises(RpcFault) as no_wt:
            ops.resolve_route(reg, {"root": str(proj), "worktree": "ghost", "service": "api"})
        assert no_wt.value.code == "not_found"
    finally:
        reg.close()


# --- CLI `curl` against a fake proxy -------------------------------------------------- #


class _Seen:
    host = method = path = ctype = None
    body = b""


@pytest.fixture
def fake_proxy():
    """An HTTP server that records what it received and answers 200 (404 on ``/nope``)."""
    seen = _Seen()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a):  # keep the test output quiet
            pass

        def _serve(self):
            seen.host = self.headers.get("Host")
            seen.method = self.command
            seen.path = self.path
            seen.ctype = self.headers.get("Content-Type")
            length = int(self.headers.get("Content-Length") or 0)
            seen.body = self.rfile.read(length) if length else b""
            if self.path == "/nope":
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"missing")
                return
            payload = json.dumps({"ok": True, "seen_host": seen.host}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_PATCH = do_DELETE = _serve

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv.server_address[1], seen
    finally:
        srv.shutdown()
        thread.join()


def _route_via(monkeypatch, port, *, host="api.feat.acme.localhost", proxy_error=None):
    """Point the CLI at ``port`` and pretend the daemon is up."""
    monkeypatch.setattr(cli._daemon, "is_running", lambda: True)

    def fake_call(method, params=None, timeout=None):
        assert method == "route"
        return {
            "host": host, "scheme": "http",
            "proxy_port": port, "direct_port": 4783, "proxy_error": proxy_error,
        }

    monkeypatch.setattr(cli.ipc, "call", fake_call)


def test_curl_get_renders_body_and_sends_the_right_host(fake_proxy, monkeypatch) -> None:
    port, seen = fake_proxy
    _route_via(monkeypatch, port)
    result = runner.invoke(app, ["curl", "feat", "api", "/tasks"])
    assert result.exit_code == 0
    assert '"ok": true' in result.output  # the 200 body reached stdout
    assert seen.method == "GET"
    assert seen.path == "/tasks"
    assert seen.host == "api.feat.acme.localhost"  # the Host header the proxy routes on


def test_curl_post_sends_json_body_by_default(fake_proxy, monkeypatch) -> None:
    port, seen = fake_proxy
    _route_via(monkeypatch, port)
    result = runner.invoke(
        app, ["curl", "feat", "api", "-X", "POST", "-d", '{"title":"x"}', "/tasks"]
    )
    assert result.exit_code == 0
    assert seen.method == "POST"
    assert seen.body == b'{"title":"x"}'
    assert seen.ctype == "application/json"  # defaulted, since -d was given without a header


def test_curl_bare_path_is_prefixed_with_slash(fake_proxy, monkeypatch) -> None:
    port, seen = fake_proxy
    _route_via(monkeypatch, port)
    result = runner.invoke(app, ["curl", "feat", "api", "tasks"])
    assert result.exit_code == 0
    assert seen.path == "/tasks"


def test_curl_fail_flag_exits_nonzero_on_4xx(fake_proxy, monkeypatch) -> None:
    port, _seen = fake_proxy
    _route_via(monkeypatch, port)
    ok = runner.invoke(app, ["curl", "feat", "api", "/nope"])
    assert ok.exit_code == 0  # without --fail, a 404 is still a "successful" call
    failed = runner.invoke(app, ["curl", "feat", "api", "-f", "/nope"])
    assert failed.exit_code == 1  # --fail turns >= 400 into a non-zero exit


def test_curl_errors_when_proxy_is_not_listening(fake_proxy, monkeypatch) -> None:
    port, _seen = fake_proxy
    _route_via(monkeypatch, port)  # then override the port to 0
    monkeypatch.setattr(
        cli.ipc, "call",
        lambda *a, **k: {"host": "api.feat.acme.localhost", "scheme": "http",
                         "proxy_port": 0, "direct_port": 4783, "proxy_error": ":80 in use"},
    )
    result = runner.invoke(app, ["curl", "feat", "api", "/tasks"])
    assert result.exit_code == 1
    assert "proxy isn't listening" in result.output


def test_curl_surfaces_a_route_rpc_error(monkeypatch) -> None:
    monkeypatch.setattr(cli._daemon, "is_running", lambda: True)

    def boom(*_a, **_k):
        raise cli.ipc.RpcError("no_route", "service 'db' has no `url`")

    monkeypatch.setattr(cli.ipc, "call", boom)
    result = runner.invoke(app, ["curl", "feat", "db", "/x"])
    assert result.exit_code == 1
    assert "no `url`" in result.output
