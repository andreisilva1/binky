"""Integration tests for the daemon lifecycle (spawns a real detached process)."""

import contextlib

import pytest

from binky import daemon, ipc, paths


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


def test_a_daemon_that_dies_on_startup_says_why(monkeypatch) -> None:
    """A failed start must report the cause, not just that time ran out.

    With stderr going to DEVNULL these two were the same event to the caller,
    which is what made a dead daemon look like a slow one. An unparseable proxy
    port kills the daemon before it writes its endpoint file, so this exercises
    the path where there is nothing to ping and never will be.
    """
    monkeypatch.setenv("BINKY_PROXY_PORT", "not-a-port")

    with pytest.raises(RuntimeError) as excinfo:
        daemon.start(timeout=30)

    message = str(excinfo.value)
    assert "exited with code" in message, message  # fast-failed, didn't wait out the timeout
    assert "not-a-port" in message  # and carried the daemon's own traceback back
    assert paths.daemon_log_path().exists()


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
