"""The agent substrate: `expose` publishes a worktree's live coordinates, and
`verify` runs the project's checks against it and records a pass/fail verdict.

Binky stays the substrate — `verify` is a gate a driver calls at *its* done moment,
not something Binky triggers by watching an agent.
"""

import contextlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from binky import daemon, ipc, ops
from binky.config import load_config_data


# --- unit: the exposed-coordinates map (no daemon) ------------------------- #
def test_exposed_map_urls_and_addresses() -> None:
    cfg = load_config_data(
        {
            "project": {"name": "demo", "https": True},
            "services": {"web": {"cmd": "run", "url": "web"}, "db": {"adapter": "fake"}},
            "agents": {"expose": ["web", "db"]},
        }
    )
    wt = SimpleNamespace(name="feat")
    out = ops._exposed(cfg, wt, {("web", ""): 4001}, {"db": "postgres://x/acme_feat"})
    assert out["web"] == "https://web.feat.demo.localhost"  # https since project.https
    assert out["db"] == "postgres://x/acme_feat"  # adapter → its address (${db.url})


def test_exposed_default_is_empty() -> None:
    cfg = load_config_data(
        {"project": {"name": "demo"}, "services": {"web": {"cmd": "run", "url": "web"}}}
    )
    wt = SimpleNamespace(name="feat")
    assert ops._exposed(cfg, wt, {("web", ""): 4001}, {}) == {}  # nothing exposed until listed


# --- e2e: verify + env through the daemon ---------------------------------- #
@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("BINKY_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("BINKY_PROXY_PORT", "0")
    yield
    with contextlib.suppress(Exception):
        daemon.stop(timeout=5)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _project(tmp_path: Path, *, verify_ok: bool = True, expose: list[str] | None = None) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    py = sys.executable
    (proj / "check.py").write_text(
        f"import sys\nsys.exit({0 if verify_ok else 1})\n", encoding="utf-8"
    )
    expose_line = f"expose = {expose!r}\n" if expose is not None else ""
    (proj / "binky.toml").write_text(
        '[project]\nname = "demo"\n'
        '[services.web]\n'
        f"cmd = '\"{py}\" -m http.server ${{self.port}}'\n"
        'url = "web"\n'
        '[agents]\n'
        + expose_line
        + f"verify = ['\"{py}\" check.py']\n",
        encoding="utf-8",
    )
    _git(proj, "init", "-q")
    _git(proj, "add", "-A")
    _git(proj, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "-m", "i")
    return proj


def test_verify_passing_records_status(tmp_path: Path) -> None:
    daemon.start(timeout=20)
    proj = _project(tmp_path, verify_ok=True)
    ipc.call("up", {"root": str(proj), "worktree": "feat"}, timeout=60)

    res = ipc.call("verify", {"root": str(proj), "worktree": "feat"}, timeout=60)
    assert res["passed"] is True
    assert res["verify"] == "passing"

    feat = next(w for w in ipc.call("status")["worktrees"] if w["worktree"] == "feat")
    assert feat["verify"] == "passing"  # verdict became worktree state


def test_verify_failing(tmp_path: Path) -> None:
    daemon.start(timeout=20)
    proj = _project(tmp_path, verify_ok=False)
    ipc.call("up", {"root": str(proj), "worktree": "feat"}, timeout=60)

    res = ipc.call("verify", {"root": str(proj), "worktree": "feat"}, timeout=60)
    assert res["passed"] is False
    assert res["verify"] == "failing"


def test_env_exposes_listed_service(tmp_path: Path) -> None:
    daemon.start(timeout=20)
    proj = _project(tmp_path, expose=["web"])
    ipc.call("up", {"root": str(proj), "worktree": "feat"}, timeout=60)

    res = ipc.call("env", {"root": str(proj), "worktree": "feat"})
    assert res["env"] == {"web": "http://web.feat.demo.localhost"}


def test_env_empty_without_expose(tmp_path: Path) -> None:
    daemon.start(timeout=20)
    proj = _project(tmp_path)  # no expose declared
    ipc.call("up", {"root": str(proj), "worktree": "feat"}, timeout=60)

    res = ipc.call("env", {"root": str(proj), "worktree": "feat"})
    assert res["env"] == {}


def test_metrics_via_daemon(tmp_path: Path) -> None:
    daemon.start(timeout=20)
    proj = _project(tmp_path)
    ipc.call("up", {"root": str(proj), "worktree": "feat"}, timeout=60)

    m = ipc.call("metrics")
    assert m["worktrees"]["total"] >= 1
    assert m["worktrees"]["running"] >= 1
    assert set(m["admission"]) == {"capacity", "in_use", "queued"}
