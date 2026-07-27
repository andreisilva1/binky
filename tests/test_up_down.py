"""End-to-end: `up` spawns processes with allocated ports, `down` stops them."""

import contextlib
import http.client
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from _demo_app import WEB_SERVER_PY
from binky import daemon, ipc, paths, settings


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("BINKY_HOME", str(tmp_path / "home"))
    settings.save(settings.Settings(proxy_port=0, https_port=0))  # no proxy unless a test opts in
    yield
    with contextlib.suppress(Exception):
        daemon.stop(timeout=5)


def _make_project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "sleeper.py").write_text(
        "import time\nprint('hello from binky', flush=True)\ntime.sleep(15)\n", encoding="utf-8"
    )
    (proj / "server.py").write_text(WEB_SERVER_PY, encoding="utf-8")
    py = sys.executable
    # single-quoted TOML literal strings so Windows backslashes stay literal
    (proj / "binky.toml").write_text(
        '[project]\n'
        'name = "demo"\n'
        # The admission ceiling is machine-global now (default_max_parallel from boot RAM;
        # no per-project override). It is max(2, min(RAM_GB // 2, cores)) — 3+ on every
        # current CI runner (Linux/Windows 4c, macOS 3c) — and a slot is held until `down`,
        # so the three concurrent ups below are admitted together. On a 2-core box the third
        # would queue; that box is out of scope for this ports-and-git test.
        '[services.sleeper]\n'
        f"cmd = '\"{py}\" sleeper.py'\n"
        '[services.web]\n'
        f"cmd = '\"{py}\" server.py ${{self.port}}'\n"
        'url = "web"\n'
        'depends_on = ["sleeper"]\n',
        encoding="utf-8",
    )
    _git(proj, "init", "-q")
    _git(proj, "add", "-A")
    _git(proj, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "init")
    return proj


def test_up_status_down(tmp_path: Path) -> None:
    daemon.start(timeout=20)
    proj = _make_project(tmp_path)

    res = ipc.call("up", {"root": str(proj), "worktree": "feat"}, timeout=60)
    assert res["worktree"] == "feat"
    services = {s["service"]: s for s in res["services"]}
    assert set(services) == {"sleeper", "web"}
    assert isinstance(services["web"]["port"], int)  # url → allocated a port
    assert services["sleeper"]["port"] is None  # no url/ports → no port

    # a real git worktree was provisioned for the branch
    wt_dir = paths.worktrees_dir() / "demo" / "feat"
    assert wt_dir.exists()
    assert (wt_dir / "binky.toml").exists()

    feat = next(w for w in ipc.call("status")["worktrees"] if w["worktree"] == "feat")
    assert feat["running"] is True
    assert feat["status"] == "ready"

    with pytest.raises(ipc.RpcError, match="already up"):
        ipc.call("up", {"root": str(proj), "worktree": "feat"})

    result = ipc.call("down", {"root": str(proj), "worktree": "feat"})
    assert result["stopped"] >= 1

    feat = next(w for w in ipc.call("status")["worktrees"] if w["worktree"] == "feat")
    assert feat["running"] is False
    assert feat["status"] == "stopped"


def test_up_reuses_ports_after_down(tmp_path: Path) -> None:
    daemon.start(timeout=20)
    proj = _make_project(tmp_path)

    first = ipc.call("up", {"root": str(proj), "worktree": "feat"}, timeout=60)
    web_port = next(s["port"] for s in first["services"] if s["service"] == "web")
    ipc.call("down", {"root": str(proj), "worktree": "feat"})

    second = ipc.call("up", {"root": str(proj), "worktree": "feat"}, timeout=60)
    web_port2 = next(s["port"] for s in second["services"] if s["service"] == "web")
    assert web_port2 == web_port  # ports are kept across down/up


def test_up_refuses_a_reused_port_that_was_stolen(tmp_path: Path) -> None:
    """C8: a port Binky reused was taken by an outside process while the worktree was down.
    `up` must fail loud and name the port, not hand the service a port it can't bind and let
    it crash-loop in silence. We don't auto-reallocate — the decision is to report, not paper
    over."""
    daemon.start(timeout=20)
    proj = _make_project(tmp_path)

    first = ipc.call("up", {"root": str(proj), "worktree": "feat"}, timeout=60)
    web_port = next(s["port"] for s in first["services"] if s["service"] == "web")
    ipc.call("down", {"root": str(proj), "worktree": "feat"})

    thief = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    thief.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    thief.bind(("127.0.0.1", web_port))  # a stranger grabs the port while feat is down
    thief.listen(1)
    try:
        with pytest.raises(ipc.RpcError, match="took it") as excinfo:
            ipc.call("up", {"root": str(proj), "worktree": "feat"}, timeout=30)
        assert excinfo.value.code == "port_taken"
        assert str(web_port) in str(excinfo.value)  # the offending port is named
    finally:
        thief.close()

    # transactional: the refused up left the worktree exactly as `down` did, not half-up
    feat = next(w for w in ipc.call("status")["worktrees"] if w["worktree"] == "feat")
    assert feat["running"] is False
    assert feat["status"] == "stopped"


def test_parallel_ups_distinct_worktrees(tmp_path: Path) -> None:
    """Concurrent ups (up to what admission will admit at once) all succeed, with
    distinct ports and no git/index races — the core "many at once" promise.

    How many run at once is admission's ``capacity``, derived from the box's RAM/cores, so
    we saturate *that* rather than hardcode a count a small CI runner can't admit. Beyond
    capacity an up correctly *queues* until a slot frees — which never comes here (nothing
    goes down), so a hardcoded 3 on a 2-core runner (capacity 2) leaves the third waiting out
    its whole RPC timeout. Ask the daemon its capacity and launch min(capacity, 3): always
    real concurrency (admission's floor is 2), never more than it can hold."""
    daemon.start(timeout=20)
    capacity = ipc.call("metrics")["admission"]["capacity"]
    proj = _make_project(tmp_path)
    names = ["a", "b", "c"][: min(capacity, 3)]
    assert len(names) >= 2  # admission's floor is 2 — we always exercise genuine concurrency

    results: dict[str, dict] = {}
    errors: list[tuple[str, Exception]] = []

    def worker(name: str) -> None:
        try:
            results[name] = ipc.call("up", {"root": str(proj), "worktree": name}, timeout=90)
        except Exception as exc:
            errors.append((name, exc))

    threads = [threading.Thread(target=worker, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    web_ports = [
        next(s["port"] for s in results[n]["services"] if s["service"] == "web") for n in names
    ]
    assert len(set(web_ports)) == len(names)  # collision-free across parallel ups

    statuses = {w["worktree"]: w for w in ipc.call("status")["worktrees"]}
    for n in names:
        assert statuses[n]["status"] == "ready"
        assert statuses[n]["running"] is True


def test_live_gc_after_service_exits(tmp_path: Path) -> None:
    """A service that exits on its own is noticed live: the daemon's supervisor
    reclaims the worktree (status → stopped) without a daemon restart."""
    daemon.start(timeout=20)
    proj = tmp_path / "shortlived"
    proj.mkdir()
    (proj / "run.py").write_text("import time\ntime.sleep(2)\n", encoding="utf-8")
    py = sys.executable
    (proj / "binky.toml").write_text(
        '[project]\nname = "sl"\n'
        '[services.job]\n'
        f"cmd = '\"{py}\" run.py'\n"
        'restart = "never"\n',  # exits 0, never restarts → gets reclaimed
        encoding="utf-8",
    )
    _git(proj, "init", "-q")
    _git(proj, "add", "-A")
    _git(proj, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "i")

    ipc.call("up", {"root": str(proj), "worktree": "feat"}, timeout=30)

    deadline = time.monotonic() + 15
    feat = None
    while time.monotonic() < deadline:
        feat = next(w for w in ipc.call("status")["worktrees"] if w["worktree"] == "feat")
        if feat["status"] == "stopped" and not feat["running"]:
            break
        time.sleep(0.3)
    assert feat is not None
    assert feat["status"] == "stopped"  # supervised to a stop, live
    assert feat["running"] is False


def test_up_missing_config(tmp_path: Path) -> None:
    daemon.start(timeout=20)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ipc.RpcError, match="config"):
        ipc.call("up", {"root": str(empty), "worktree": "x"})


def test_unhealthy_aborts(tmp_path: Path) -> None:
    daemon.start(timeout=20)
    proj = tmp_path / "bad"
    proj.mkdir()
    (proj / "sleeper.py").write_text("import time\ntime.sleep(15)\n", encoding="utf-8")
    py = sys.executable
    (proj / "binky.toml").write_text(
        '[project]\nname = "bad"\n'
        '[services.web]\n'
        f"cmd = '\"{py}\" sleeper.py'\n"  # never binds its port
        'url = "web"\n'
        'health = { tcp = "${self.port}", timeout = "1s" }\n',
        encoding="utf-8",
    )
    _git(proj, "init", "-q")
    _git(proj, "add", "-A")
    _git(proj, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "i")
    with pytest.raises(ipc.RpcError, match="healthy"):
        ipc.call("up", {"root": str(proj), "worktree": "feat"}, timeout=30)

    # transactional: the failed up left nothing behind
    assert all(w["worktree"] != "feat" for w in ipc.call("status")["worktrees"])
    assert not (paths.worktrees_dir() / "bad" / "feat").exists()


def test_dependency_cycle(tmp_path: Path) -> None:
    daemon.start(timeout=20)
    proj = tmp_path / "cyc"
    proj.mkdir()
    (proj / "binky.toml").write_text(
        '[project]\nname = "cyc"\n'
        '[services.a]\ncmd = "echo x"\ndepends_on = ["b"]\n'
        '[services.b]\ncmd = "echo x"\ndepends_on = ["a"]\n',
        encoding="utf-8",
    )
    _git(proj, "init", "-q")
    _git(proj, "add", "-A")
    _git(proj, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "i")
    with pytest.raises(ipc.RpcError, match="cycle"):
        ipc.call("up", {"root": str(proj), "worktree": "feat"})


def test_up_requires_git(tmp_path: Path) -> None:
    daemon.start(timeout=20)
    proj = tmp_path / "nogit"
    proj.mkdir()
    (proj / "binky.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    with pytest.raises(ipc.RpcError, match="not a git repository"):
        ipc.call("up", {"root": str(proj), "worktree": "feat"})


def test_logs_captured(tmp_path: Path) -> None:
    daemon.start(timeout=20)
    proj = _make_project(tmp_path)
    ipc.call("up", {"root": str(proj), "worktree": "feat"}, timeout=60)
    log = paths.logs_dir() / "demo" / "feat" / "sleeper.log"

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if log.exists() and "hello from binky" in log.read_text(encoding="utf-8", errors="ignore"):
            break
        time.sleep(0.1)
    assert log.exists()
    assert "hello from binky" in log.read_text(encoding="utf-8", errors="ignore")


def test_proxy_routes_by_host(tmp_path: Path) -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    proxy_port = probe.getsockname()[1]
    probe.close()
    # the daemon reads ports from the settings store at boot, so set it before start
    settings.save(settings.Settings(proxy_port=proxy_port, https_port=0))

    daemon.start(timeout=20)
    proj = _make_project(tmp_path)
    ipc.call("up", {"root": str(proj), "worktree": "feat"}, timeout=60)

    # web.feat.demo.localhost → the worktree's http.server
    conn = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=5)
    conn.request("GET", "/", headers={"Host": "web.feat.demo.localhost"})
    assert conn.getresponse().status == 200
    conn.close()

    # unknown host → 502
    conn = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=5)
    conn.request("GET", "/", headers={"Host": "ghost.feat.demo.localhost"})
    assert conn.getresponse().status == 502
    conn.close()


def test_remove_clean(tmp_path: Path) -> None:
    daemon.start(timeout=20)
    proj = _make_project(tmp_path)
    ipc.call("up", {"root": str(proj), "worktree": "feat"}, timeout=60)
    wt_dir = paths.worktrees_dir() / "demo" / "feat"
    assert wt_dir.exists()

    ipc.call("remove", {"root": str(proj), "worktree": "feat"})
    assert not wt_dir.exists()
    assert not (paths.logs_dir() / "demo" / "feat").exists()  # logs retained until remove
    assert all(w["worktree"] != "feat" for w in ipc.call("status")["worktrees"])


def test_remove_refuses_dirty_then_forces(tmp_path: Path) -> None:
    daemon.start(timeout=20)
    proj = _make_project(tmp_path)
    ipc.call("up", {"root": str(proj), "worktree": "feat"}, timeout=60)
    wt_dir = paths.worktrees_dir() / "demo" / "feat"
    (wt_dir / "scratch.txt").write_text("uncommitted", encoding="utf-8")

    with pytest.raises(ipc.RpcError, match="uncommitted"):
        ipc.call("remove", {"root": str(proj), "worktree": "feat"})
    assert wt_dir.exists()

    ipc.call("remove", {"root": str(proj), "worktree": "feat", "force": True})
    assert not wt_dir.exists()
