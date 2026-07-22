"""SDK for writing a Warren adapter as a standalone executable (the external tier).

Author the *same* class shape as an in-process adapter, then let ``run`` speak the
subprocess+JSON protocol for you::

    #!/usr/bin/env python
    from warren.adapter_sdk import Adapter, run

    class MyAdapter(Adapter):
        CAPABILITIES = {"clone": False}

        def provision(self, slice_name): ...
        def resolve(self, slice_name): return "scheme://..."
        def teardown(self, slice_name): ...

    if __name__ == "__main__":
        run(MyAdapter)

Install the file as ``warren-adapter-<name>`` on PATH and reference it as
``adapter = "<name>"`` in ``warren.toml``. Non-Python authors implement the same
protocol directly (one JSON request on stdin, one JSON response on stdout).
"""

from __future__ import annotations

import json
import sys

from warren.adapters import Adapter, AdapterError

__all__ = ["Adapter", "AdapterError", "run"]


def run(adapter_cls: type[Adapter], argv: list[str] | None = None) -> int:
    """Protocol entry point: dispatch one operation and print its JSON response.

    Returns a process exit code (0 on a well-formed response, including handled
    adapter errors reported as ``{"ok": false, ...}``; 2 on a bad invocation).
    """
    args = sys.argv[1:] if argv is None else argv
    if not args:
        _emit({"ok": False, "error": "usage: <adapter> <provision|resolve|teardown|capabilities>"})
        return 2
    op = args[0]

    if op == "capabilities":  # no config/slice needed
        _emit({"ok": True, "capabilities": dict(getattr(adapter_cls, "CAPABILITIES", {}))})
        return 0

    raw = sys.stdin.read()
    try:
        request = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        _emit({"ok": False, "error": f"invalid JSON request: {exc}"})
        return 0
    config = request.get("config") or {}
    slice_name = request.get("slice")

    try:
        adapter = adapter_cls(config)
        if op == "provision":
            adapter.provision(slice_name)
            _emit({"ok": True})
        elif op == "resolve":
            _emit({"ok": True, "address": adapter.resolve(slice_name)})
        elif op == "teardown":
            adapter.teardown(slice_name)
            _emit({"ok": True})
        elif op == "clone":
            adapter.clone(request.get("source"), request.get("dest"))
            _emit({"ok": True})
        else:
            _emit({"ok": False, "error": f"unknown op: {op}"})
    except Exception as exc:  # any adapter failure becomes a clean protocol error
        _emit({"ok": False, "error": str(exc)})
    return 0


def _emit(response: dict) -> None:
    sys.stdout.write(json.dumps(response) + "\n")
    sys.stdout.flush()
