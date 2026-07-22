"""Integration tests for the daemon lifecycle (spawns a real detached process)."""

import contextlib

import pytest

from binky import daemon, ipc


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("BINKY_HOME", str(tmp_path))
    monkeypatch.setenv("BINKY_PROXY_PORT", "0")
    yield
    # best-effort: never leave a spawned daemon behind
    with contextlib.suppress(Exception):
        daemon.stop(timeout=5)


def test_not_running_initially() -> None:
    assert daemon.is_running() is False
    assert ipc.read_daemon_info() is None
    with pytest.raises(ipc.DaemonNotRunning):
        ipc.call("ping")


def test_start_ping_stop() -> None:
    daemon.start(timeout=20)
    assert daemon.is_running()

    result = ipc.call("ping")
    assert result["pong"] is True
    assert isinstance(result["pid"], int)

    with pytest.raises(ipc.RpcError, match="unknown method") as excinfo:
        ipc.call("does-not-exist")
    assert excinfo.value.code == "unknown_method"

    # introspection methods over the wire
    assert set(ipc.call("methods")["methods"]) >= {"ping", "shutdown", "methods", "version"}
    assert ipc.call("version")["version"]

    assert daemon.stop(timeout=10) is True
    assert daemon.is_running() is False
    assert ipc.read_daemon_info() is None


def test_start_is_idempotent() -> None:
    daemon.start(timeout=20)
    first = ipc.read_daemon_info()
    daemon.start(timeout=20)  # already running → no-op
    second = ipc.read_daemon_info()
    assert first.pid == second.pid


def test_bad_token_rejected() -> None:
    daemon.start(timeout=20)
    info = ipc.read_daemon_info()
    tampered = ipc.DaemonInfo(
        transport=info.transport,
        token="wrong-token",
        pid=info.pid,
        path=info.path,
        host=info.host,
        port=info.port,
    )
    ipc.write_daemon_info(tampered)
    with pytest.raises(ipc.RpcError, match="bad token"):
        ipc.call("ping")
    # restore the real token so the fixture teardown can shut the daemon down
    ipc.write_daemon_info(info)
