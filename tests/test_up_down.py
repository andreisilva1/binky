"""End-to-end: `up` spawns processes with allocated ports, `down` stops them."""

import contextlib
import subprocess
import sys
import time
from pathlib import Path

import pytest

from warren import daemon, ipc, paths


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("WARREN_HOME", str(tmp_path / "home"))
    yield
    with contextlib.suppress(Exception):
        daemon.stop(timeout=5)


def _make_project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "sleeper.py").write_text(
        "import time\nprint('hello from warren', flush=True)\ntime.sleep(15)\n", encoding="utf-8"
    )
    py = sys.executable
    # single-quoted TOML literal strings so Windows backslashes stay literal
    (proj / "warren.toml").write_text(
        '[project]\n'
        'name = "demo"\n'
        '[services.sleeper]\n'
        f"cmd = '\"{py}\" sleeper.py'\n"
        '[services.web]\n'
        f"cmd = '\"{py}\" -m http.server ${{self.port}}'\n"
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
    assert (wt_dir / "warren.toml").exists()

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
    (proj / "warren.toml").write_text(
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


def test_dependency_cycle(tmp_path: Path) -> None:
    daemon.start(timeout=20)
    proj = tmp_path / "cyc"
    proj.mkdir()
    (proj / "warren.toml").write_text(
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
    (proj / "warren.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    with pytest.raises(ipc.RpcError, match="not a git repository"):
        ipc.call("up", {"root": str(proj), "worktree": "feat"})


def test_logs_captured(tmp_path: Path) -> None:
    daemon.start(timeout=20)
    proj = _make_project(tmp_path)
    ipc.call("up", {"root": str(proj), "worktree": "feat"}, timeout=60)
    log = paths.logs_dir() / "demo" / "feat" / "sleeper.log"

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if log.exists() and "hello from warren" in log.read_text(encoding="utf-8", errors="ignore"):
            break
        time.sleep(0.1)
    assert log.exists()
    assert "hello from warren" in log.read_text(encoding="utf-8", errors="ignore")


def test_remove_clean(tmp_path: Path) -> None:
    daemon.start(timeout=20)
    proj = _make_project(tmp_path)
    ipc.call("up", {"root": str(proj), "worktree": "feat"}, timeout=60)
    wt_dir = paths.worktrees_dir() / "demo" / "feat"
    assert wt_dir.exists()

    ipc.call("remove", {"root": str(proj), "worktree": "feat"})
    assert not wt_dir.exists()
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
