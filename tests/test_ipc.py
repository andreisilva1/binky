"""ipc.call's failure contract.

The docstring promises DaemonNotRunning when unreachable and RpcError on a bad
response. A failure *after* connecting (timeout, reset mid-call) used to escape
as a raw OSError, past every caller that only guards DaemonNotRunning / RpcError.
These pin the contract with a fake socket, no real daemon.
"""

import json

import pytest

from binky import ipc


class _FakeSock:
    def __init__(self, *, sendall_exc: Exception | None = None, recv: bytes = b"") -> None:
        self.sendall_exc = sendall_exc
        self._recv = recv
        self.closed = False

    def settimeout(self, _t: float) -> None: ...
    def connect(self, _addr: object) -> None: ...

    def sendall(self, _data: bytes) -> None:
        if self.sendall_exc is not None:
            raise self.sendall_exc

    def recv(self, _n: int) -> bytes:
        chunk, self._recv = self._recv, b""
        return chunk

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _fake_daemon_info(monkeypatch):
    info = ipc.DaemonInfo(transport="tcp", token="t", pid=1, host="127.0.0.1", port=1)
    monkeypatch.setattr(ipc, "read_daemon_info", lambda: info)


def _connect_returns(monkeypatch, sock: _FakeSock) -> _FakeSock:
    monkeypatch.setattr(ipc, "_connect", lambda info, timeout: sock)
    return sock


def test_send_failure_after_connect_is_daemon_not_running(monkeypatch) -> None:
    sock = _connect_returns(monkeypatch, _FakeSock(sendall_exc=BrokenPipeError("reset")))
    with pytest.raises(ipc.DaemonNotRunning):
        ipc.call("ping")
    assert sock.closed  # socket still cleaned up


def test_recv_timeout_mid_call_is_daemon_not_running(monkeypatch) -> None:
    class _TimingOut(_FakeSock):
        def recv(self, _n: int) -> bytes:
            raise TimeoutError("timed out")

    sock = _connect_returns(monkeypatch, _TimingOut())
    with pytest.raises(ipc.DaemonNotRunning):
        ipc.call("ping")
    assert sock.closed


def test_garbled_response_is_rpc_error(monkeypatch) -> None:
    sock = _connect_returns(monkeypatch, _FakeSock(recv=b"not-json\n"))
    with pytest.raises(ipc.RpcError) as excinfo:
        ipc.call("ping")
    assert excinfo.value.code == "malformed"
    assert sock.closed


def test_a_well_formed_response_still_returns_its_result(monkeypatch) -> None:
    line = json.dumps({"ok": True, "result": {"x": 1}}).encode() + b"\n"
    sock = _connect_returns(monkeypatch, _FakeSock(recv=line))
    assert ipc.call("ping") == {"x": 1}
    assert sock.closed
