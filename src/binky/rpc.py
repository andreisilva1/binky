"""The daemon's RPC method surface.

A tiny router: methods are ``async (params: dict) -> dict`` handlers, dispatched
to a structured envelope — ``{"ok": true, "result": ...}`` or
``{"ok": false, "error": {"code", "message"}}``. Later phases register domain
methods (``up``, ``down``, ``status``, …) on the same router.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

Handler = Callable[[dict], Awaitable[dict]]


class RpcFault(Exception):
    """Raise inside a handler to return a structured error to the caller."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class Router:
    def __init__(self) -> None:
        self._methods: dict[str, Handler] = {}

    def add(self, name: str, handler: Handler) -> None:
        self._methods[name] = handler

    def method(self, name: str) -> Callable[[Handler], Handler]:
        def decorator(handler: Handler) -> Handler:
            self._methods[name] = handler
            return handler

        return decorator

    def names(self) -> list[str]:
        return sorted(self._methods)

    async def dispatch(self, method: str | None, params: dict) -> dict:
        """Run ``method`` and return a response envelope (never raises)."""
        handler = self._methods.get(method or "")
        if handler is None:
            return _error("unknown_method", f"unknown method: {method}")
        try:
            result = await handler(params)
        except RpcFault as fault:
            return _error(fault.code, fault.message)
        except Exception as exc:  # unexpected handler bug → surfaced, not swallowed
            return _error("internal", str(exc))
        return {"ok": True, "result": result}


def _error(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}
