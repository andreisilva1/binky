"""End-to-end: `up` spawns processes with allocated ports, `down` stops them."""

import contextlib
import sys
from pathlib import Path

import pytest

from warren import daemon, ipc


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("WARREN_HOME", str(tmp_path / "home"))
    yield
    with contextlib.suppress(Exception):
        daemon.stop(timeout=5)


def _make_project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "sleeper.py").write_text("import time\ntime.sleep(15)\n", encoding="utf-8")
    py = sys.executable
    # single-quoted TOML literal strings so Windows backslashes stay literal
    (proj / "warren.toml").write_text(
        '[project]\n'
        'name = "demo"\n'
        '[services.sleeper]\n'
        f"cmd = '\"{py}\" sleeper.py'\n"
        '[services.web]\n'
        f"cmd = '\"{py}\" sleeper.py'\n"
        'url = "web"\n',
        encoding="utf-8",
    )
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
