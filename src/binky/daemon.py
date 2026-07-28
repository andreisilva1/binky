"""The always-alive Binky daemon.

An asyncio server that owns the machine-global state and answers CLI requests.
It is spawned lazily and detached, so it outlives the CLI process; it writes its
endpoint + token to ``daemon.json`` when ready and clears it on shutdown.

It sets up the process, transport, and lifecycle and exposes the full RPC
surface (``up``/``down``/``remove``/``status`` plus ``ping``/``shutdown``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import signal
import subprocess
import sys
import time

from binky import admission, ipc, observability, ops, paths, procs, proxy, rpc, settings
from binky.https import Caddy
from binky.registry import Registry, WorktreeStatus
from binky.supervisor import ProcessManager, TickReport

SUPERVISE_INTERVAL = 2.0  # seconds between live supervision ticks
PRESSURE_TICKS = 3  # consecutive over-floor ticks before we evict (hysteresis, ~6s)
EVICT_GRACE = 30.0  # seconds a just-resumed worktree is protected from re-eviction (anti-flap)


class Daemon:
    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self._token = secrets.token_hex(16)
        self.registry = Registry.connect()
        self.procman = ProcessManager()
        self.settings = settings.load()  # machine-global reserves + ports, read at boot
        self.admission = admission.make_default(self.settings)
        self._slots: dict[tuple[str, str], int] = {}
        # C2 pressure eviction: what a suspended worktree needs to bring itself back,
        # the parked task waiting in the admission queue to do so, and when each was last
        # resumed (so a just-resumed worktree isn't the one we immediately evict again).
        self._up_params: dict[tuple[str, str], dict] = {}
        self._resume_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._resumed_at: dict[tuple[str, str], float] = {}
        self._pressure_ticks = 0
        self._wt_locks: dict[tuple[str | None, str | None], asyncio.Lock] = {}
        self.routes: dict[str, int] = {}
        self._routes_by_wt: dict[tuple[str, str], dict[str, int]] = {}
        self.proxy = proxy.Proxy(lambda host: self.routes.get(host))
        self._proxy_port = 0
        self._proxy_error: str | None = None  # why the HTTP proxy isn't listening, if it isn't
        self._https_error: str | None = None  # why TLS fronting is unavailable, if requested
        self._caddy: Caddy | None = None
        # Adopt any still-live worktrees a previous daemon left running, and clean up the
        # rest (C3). Needs procman (built above) so adoption has somewhere to register.
        ops.reconcile(self.registry, self.procman)
        # reconcile re-supervises the adopted *processes* and rewrites the registry rows, but
        # the routing/admission/resume tables live only in this process and are otherwise
        # populated solely by `up`. Rebuild them for the adopted worktrees so they come back
        # reachable and correctly accounted for, not stranded "running" with dead hostnames.
        self._adopt_state()
        self.router = rpc.Router()
        self.router.add("ping", self._ping)
        self.router.add("shutdown", self._shutdown)
        self.router.add("methods", self._list_methods)
        self.router.add("version", self._version)
        self.router.add("up", self._up)
        self.router.add("down", self._down)
        self.router.add("reload", self._reload)
        self.router.add("remove", self._remove)
        self.router.add("status", self._status)
        self.router.add("verify", self._verify)
        self.router.add("env", self._env)
        self.router.add("route", self._route)
        self.router.add("metrics", self._metrics)
        self.router.add("doctor", self._doctor)
        self.router.add("gc", self._gc)
        self.router.add("settings_get", self._settings_get)
        self.router.add("settings_set", self._settings_set)

    async def _ping(self, _params: dict) -> dict:
        return {"pong": True, "pid": os.getpid()}

    async def _shutdown(self, _params: dict) -> dict:
        self._stop.set()
        return {"stopping": True}

    async def _list_methods(self, _params: dict) -> dict:
        return {"methods": self.router.names()}

    async def _version(self, _params: dict) -> dict:
        from binky import __version__

        return {"version": __version__}

    def _wt_lock(self, root: str | None, worktree: str | None) -> asyncio.Lock:
        """One lock per worktree so two ops on the *same* worktree serialize while
        different worktrees run in parallel. Keyed by (root, worktree), which maps
        one-to-one to (project, worktree) since a root has a single project."""
        return self._wt_locks.setdefault((root, worktree), asyncio.Lock())

    async def _up(self, params: dict) -> dict:
        async with self._wt_lock(params.get("root"), params.get("worktree")):
            return await self._up_locked(params)

    async def _up_locked(self, params: dict) -> dict:
        """The body of `up`, assuming the worktree lock is already held — so `reload` can run
        down+up under a single hold of the lock without re-entering it (asyncio.Lock isn't
        reentrant, so `_reload` must not call `_up`/`_down`, which re-acquire it)."""
        wants_https = False
        with contextlib.suppress(Exception):  # apply the project's https preference
            cfg = ops._load(params.get("root"))
            wants_https = bool(cfg.project.https)
            if wants_https:
                self._ensure_https()
        # Refuse fast when the wait could never end (a resource floor with nothing
        # running to free it) rather than hang until the caller's RPC times out.
        refusal = self.admission.rejection()
        if refusal is not None:
            raise rpc.RpcFault("resource_exhausted", refusal)
        token = await self.admission.acquire()  # FIFO — waits for capacity
        try:
            # off the event loop: wait_healthy / migrate must not block other requests
            result = await asyncio.to_thread(ops.up, self.registry, self.procman, params)
        except BaseException as exc:
            self.admission.release(token)
            # A worktree that's already up but lost its routes (a C3 adoption whose boot
            # rebuild couldn't run) would otherwise stay unreachable and `up` would just
            # refuse. Heal the routes before surfacing the error — the signal is unchanged.
            if isinstance(exc, rpc.RpcFault) and exc.code == "already_up":
                self._repair_routes(params)
            raise
        key = (result["project"], result["worktree"])
        self._slots[key] = token
        self._up_params[key] = params  # remembered so a pressure-suspend can re-up it (C2)
        self._routes_by_wt[key] = result.get("routes", {})
        self._rebuild_routes()
        warnings = self._reachability_warnings(result.get("routes"), wants_https)
        if warnings:
            result["warnings"] = warnings
        observability.log_event(
            "up", project=result["project"], worktree=result["worktree"],
            services=len(result.get("services", [])),
        )
        return result

    async def _down(self, params: dict) -> dict:
        async with self._wt_lock(params.get("root"), params.get("worktree")):
            return await self._down_locked(params)

    async def _down_locked(self, params: dict) -> dict:
        """The body of `down`, assuming the worktree lock is already held."""
        result = await asyncio.to_thread(ops.down, self.registry, self.procman, params)
        self._release_slot(result["project"], result["worktree"])
        observability.log_event("down", project=result["project"], worktree=result["worktree"])
        return result

    async def _reload(self, params: dict) -> dict:
        # down+up under a single hold of the worktree lock, so nothing interleaves between the
        # stop and the start. Admission is unchanged net-net vs. a plain down then up: the slot
        # is released on the way down and re-acquired on the way up — so a reload can queue if
        # the machine is at capacity with others already waiting, same rule as `up`.
        async with self._wt_lock(params.get("root"), params.get("worktree")):
            down_res = await self._down_locked(params)
            up_res = await self._up_locked(params)
        up_res["stopped"] = down_res.get("stopped", 0)
        observability.log_event(
            "reload", project=up_res["project"], worktree=up_res["worktree"],
            services=len(up_res.get("services", [])),
        )
        return up_res

    async def _remove(self, params: dict) -> dict:
        async with self._wt_lock(params.get("root"), params.get("worktree")):
            result = await asyncio.to_thread(ops.remove, self.registry, self.procman, params)
        self._release_slot(result["project"], result["worktree"])
        observability.log_event("remove", project=result["project"], worktree=result["worktree"])
        return result

    def _release_slot(self, project: str, worktree: str) -> None:
        key = (project, worktree)
        token = self._slots.pop(key, None)
        if token is not None:
            self.admission.release(token)
        self._routes_by_wt.pop(key, None)
        self._rebuild_routes()
        # A worktree going down for real (down/remove/died) outranks a pending resume:
        # cancel the parked waiter so RAM freeing up doesn't resurrect it (C2).
        self._cancel_resume(key)
        self._up_params.pop(key, None)
        self._resumed_at.pop(key, None)

    def _cancel_resume(self, key: tuple[str, str]) -> None:
        task = self._resume_tasks.pop(key, None)
        if task is not None and not task.done():
            task.cancel()

    def _ensure_https(self) -> None:
        """Lazily front the running proxy with Caddy for TLS (once, if Caddy is present).

        Records why it couldn't, when it can't, so a project that asked for `https` isn't
        silently served plain HTTP — `up` and `doctor` surface the reason.
        """
        if self._caddy is not None:
            return
        if self._proxy_port <= 0:
            self._https_error = "the HTTP proxy isn't listening, so there's nothing to front"
            return
        caddy = Caddy(self._proxy_port, https_port=self.settings.https_port)
        if not caddy.available:
            self._https_error = "Caddy is not on PATH"
            return
        if caddy.start():
            self._caddy = caddy
            self._https_error = None
        else:
            self._https_error = "Caddy is installed but failed to start (see daemon log)"

    def _reachability_warnings(self, routes: dict | None, wants_https: bool) -> list[str]:
        """Honest, up-time warnings for anything that stops a worktree's *hostnames* from
        resolving. The per-service ports still work; the point is to never let `up` print a
        clean success while the routing the user expects is quietly dead."""
        warnings: list[str] = []
        if routes and self._proxy_error:
            warnings.append(
                f"http proxy isn't listening on {self._proxy_error} — hostnames won't "
                "resolve; reach services by their :port instead"
            )
        if wants_https and self._https_error:
            warnings.append(f"https was requested but is unavailable: {self._https_error}")
        return warnings

    async def _doctor(self, _params: dict) -> dict:
        """Daemon-internal health the CLI's `doctor` can't see from outside the process."""
        return {
            "proxy_port": self._proxy_port,
            "proxy_error": self._proxy_error,
            "https_active": self._caddy is not None,
            "https_error": self._https_error,
        }

    def _rebuild_routes(self) -> None:
        self.routes = {
            host: port for routes in self._routes_by_wt.values() for host, port in routes.items()
        }

    def _rebuild_wt_routes(self, wt, group: str | None = None) -> None:
        """Recompute one worktree's proxy routes from the registry + its checked-out config,
        into ``_routes_by_wt`` (the caller flattens with ``_rebuild_routes``). Best-effort: a
        config that won't load leaves the worktree without routes rather than raising. The
        config is read from ``wt.path`` — ``binky.toml`` is versioned, so the worktree checkout
        always has it, and it reflects that worktree's own config."""
        with contextlib.suppress(Exception):
            cfg = ops._load(wt.path)
            services = ops._select_services(cfg, group)
            ports = {(p.service, p.label): p.port for p in self.registry.ports_for(wt.id)}
            self._routes_by_wt[(wt.project, wt.name)] = ops._routes(
                cfg, wt.name, ports, services
            )

    def _adopt_state(self) -> None:
        """Rebuild the daemon-side state for worktrees ``reconcile`` just adopted (C3).

        An adopted worktree is one that survived the restart still ``ready`` with live,
        now-supervised processes. For each, restore what a normal ``up`` would have put in
        this process's memory: the admission slot (so capacity accounting matches reality and
        the next ``up`` can't over-admit), the resume context (so a pressure-suspend can still
        bring it back — C2), and the proxy routes (so its hostnames resolve again). Without
        this the process survives the restart but comes back unreachable and uncounted."""
        for wt in self.registry.list_worktrees():
            if wt.status != WorktreeStatus.ready or not self.procman.is_running(wt.id):
                continue
            key = (wt.project, wt.name)
            self._slots[key] = self.admission.readmit()
            if wt.up_params:
                self._up_params[key] = wt.up_params
            self._rebuild_wt_routes(wt, wt.up_params.get("group") if wt.up_params else None)
        self._rebuild_routes()

    def _repair_routes(self, params: dict) -> None:
        """Idempotent safety net: re-install a running worktree's routes if they're missing.

        The primary rebuild happens at boot (``_adopt_state``); this catches the rare case
        where that couldn't run (e.g. the config was briefly unloadable then) and the user
        retries ``up`` — which otherwise just refuses "already up" and leaves the hostnames
        dead. We only touch a worktree with *no* routes, so a normally-routed one is untouched."""
        with contextlib.suppress(Exception):
            cfg = ops._load(params.get("root"))
            wt = self.registry.get_worktree(cfg.project.name, params.get("worktree"))
            if wt is None or (wt.project, wt.name) in self._routes_by_wt:
                return
            self._rebuild_wt_routes(wt, params.get("group"))
            self._rebuild_routes()

    async def _pump_loop(self) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            while not self._stop.is_set():
                await asyncio.sleep(1.0)
                self.admission.pump()

    async def _supervise_loop(self) -> None:
        """Live supervision: reap/restart children and reclaim worktrees that died."""
        with contextlib.suppress(asyncio.CancelledError):
            while not self._stop.is_set():
                await asyncio.sleep(SUPERVISE_INTERVAL)
                report = await asyncio.to_thread(self.procman.tick, time.monotonic())
                await self._apply_report(report)
                await self._relieve_pressure()

    async def _relieve_pressure(self) -> None:
        """Evict the newest worktree when free RAM has sat below the reserve for a while.

        The admission gate only keeps RAM free at *start*; a worktree admitted when the
        machine was healthy can still grow it into the reserve. Rather than leave the OS to
        OOM-kill something at random, Binky pauses the **newest** worktree (LIFO — the one
        that has invested the least) and re-queues it in the admission FIFO, so it comes
        back on its own once the pressure clears. A short streak (``PRESSURE_TICKS``) avoids
        reacting to a momentary spike; resetting the streak after an eviction gives the freed
        RAM time to actually come back before we consider evicting a second one."""
        if self.admission.under_pressure():
            self._pressure_ticks += 1
        else:
            self._pressure_ticks = 0
        if self._pressure_ticks >= PRESSURE_TICKS and await self._evict_one_under_pressure():
            self._pressure_ticks = 0

    async def _evict_one_under_pressure(self) -> bool:
        """Suspend the newest un-protected running worktree; True if one was suspended.

        Newest = highest admission token. A worktree resumed within ``EVICT_GRACE`` is
        skipped so we don't immediately re-evict what we just brought back (anti-flap); if
        every candidate is within grace, we suspend nothing this round and wait it out."""
        now = time.monotonic()
        for key, _token in sorted(self._slots.items(), key=lambda kv: kv[1], reverse=True):
            if now - self._resumed_at.get(key, 0.0) < EVICT_GRACE:
                continue
            await self._suspend(key)
            return True
        return False

    async def _suspend(self, key: tuple[str, str]) -> None:
        """Pause a worktree under pressure: stop its processes, mark it suspended, free its
        RAM slot, and park a task that re-ups it once the admission queue lets it back in.
        Ports and slices are kept, so the resume is a re-spawn, not a re-provision."""
        project, worktree = key
        params = self._up_params.get(key)
        root = params.get("root") if params else None
        async with self._wt_lock(root, worktree):
            wt = await asyncio.to_thread(self.registry.get_worktree, project, worktree)
            if wt is None or not self.procman.is_running(wt.id):
                return  # went down between selection and here — nothing to suspend
            await asyncio.to_thread(self.procman.stop, wt.id)
            await asyncio.to_thread(self.registry.clear_processes, wt.id)
            await asyncio.to_thread(
                self.registry.set_worktree_status, wt.id, WorktreeStatus.suspended
            )
        # off-loop work done; the slot/route bookkeeping touches asyncio state, stay on-loop.
        # NB: not _release_slot — that would cancel the very resume task we're about to park.
        token = self._slots.pop(key, None)
        if token is not None:
            self.admission.release(token)
        saved_routes = self._routes_by_wt.pop(key, None)
        self._rebuild_routes()
        observability.log_event("suspend", project=project, worktree=worktree)
        self._resume_tasks[key] = asyncio.create_task(
            self._resume_when_admitted(key, saved_routes)
        )

    async def _resume_when_admitted(self, key: tuple[str, str], saved_routes: dict | None) -> None:
        """Wait in the admission FIFO on a suspended worktree's behalf; re-up it when let in.

        Parked in the same queue a fresh ``up`` waits in, so pressure clearing (the pump)
        wakes it in fairness order. If it was taken down while parked, it yields the slot and
        stays down instead of resurrecting."""
        project, worktree = key
        params = self._up_params.get(key)
        if params is None:
            return
        try:
            token = await self.admission.acquire()
        except asyncio.CancelledError:
            return  # taken down while parked; _release_slot already cleaned up
        try:
            async with self._wt_lock(params.get("root"), worktree):
                wt = await asyncio.to_thread(self.registry.get_worktree, project, worktree)
                if wt is None or wt.status != WorktreeStatus.suspended:
                    self.admission.release(token)  # no longer ours to bring back
                    return
                result = await asyncio.to_thread(ops.up, self.registry, self.procman, params)
            self._slots[key] = token
            self._routes_by_wt[key] = result.get("routes", saved_routes or {})
            self._rebuild_routes()
            self._resumed_at[key] = time.monotonic()
            observability.log_event("resume", project=project, worktree=worktree)
        except BaseException:
            self.admission.release(token)
            observability.log_event("resume_failed", project=project, worktree=worktree)
            raise
        finally:
            self._resume_tasks.pop(key, None)

    async def _apply_report(self, report: TickReport) -> None:
        """Sync a supervision tick to the registry + admission/routes.

        The registry writes are blocking SQLite and can stall on the busy-timeout
        (up to 5s while a worker thread holds the write lock), so — like every other
        registry op the daemon does — they run off the event loop; a supervision tick
        must never freeze the proxy and RPC. Only the admission/route bookkeeping,
        which touches asyncio primitives, stays on the loop.
        """
        downed, restart_names = await asyncio.to_thread(self._sync_registry, report)
        for wt_id, service in report.restarted:
            name = restart_names.get(wt_id)
            if name is not None:
                observability.log_event(
                    "restart", project=name[0], worktree=name[1], service=service
                )
            else:  # the row vanished between tick and read — still record it, keyed by id
                observability.log_event("restart", worktree_id=wt_id, service=service)
        for wt in downed:  # free the RAM slot + drop proxy routes
            self._release_slot(wt.project, wt.name)
            if wt.id in report.failed:  # crash-looped past give-up — a consumable alert
                observability.log_event("worktree_failed", project=wt.project, worktree=wt.name)
            else:
                observability.log_event("gc", project=wt.project, worktree=wt.name)

    def _sync_registry(self, report: TickReport) -> tuple[list, dict[int, tuple[str, str]]]:
        """Off-loop: apply pid changes, mark down worktrees stopped, and resolve the names of
        restarted worktrees. Returns the downed worktree rows (so the caller can free their
        slots without a second read) and a ``{wt_id: (project, name)}`` map — so restart events
        can log the human name every other event uses, without a registry read on the event loop."""
        for wt_id, pids in report.changed_pids.items():
            with contextlib.suppress(Exception):
                self.registry.clear_processes(wt_id)
                for pid in pids:
                    self.registry.add_process(wt_id, pid)
        downed = []
        for wt_id in report.worktrees_down:
            wt = None
            with contextlib.suppress(Exception):
                wt = self.registry.get_worktree_by_id(wt_id)
                self.registry.clear_processes(wt_id)
                # crash-loop give-up is a distinct terminal state from an intentional pause,
                # so `status` can show "failed" instead of a look-alike "stopped".
                status = WorktreeStatus.failed if wt_id in report.failed else WorktreeStatus.stopped
                self.registry.set_worktree_status(wt_id, status)
            if wt is not None:
                downed.append(wt)
        restart_names: dict[int, tuple[str, str]] = {}
        for wt_id in {wt_id for wt_id, _service in report.restarted}:
            with contextlib.suppress(Exception):
                wt = self.registry.get_worktree_by_id(wt_id)
                if wt is not None:
                    restart_names[wt_id] = (wt.project, wt.name)
        return downed, restart_names

    async def _status(self, params: dict) -> dict:
        # read-only and thread-safe (connection-per-thread registry); no lock needed
        return await asyncio.to_thread(ops.status, self.registry, self.procman, params)

    async def _verify(self, params: dict) -> dict:
        async with self._wt_lock(params.get("root"), params.get("worktree")):
            result = await asyncio.to_thread(ops.verify, self.registry, self.procman, params)
        observability.log_event(
            "verify", project=result["project"], worktree=result["worktree"],
            verdict=result["verify"],
        )
        return result

    async def _env(self, params: dict) -> dict:
        return await asyncio.to_thread(ops.env, self.registry, params)

    async def _route(self, params: dict) -> dict:
        # ops resolves the Host from config + registry; only the daemon knows where the proxy
        # is actually listening, so it stamps the port (and any bind error) on the way out.
        res = await asyncio.to_thread(ops.resolve_route, self.registry, params)
        res["proxy_port"] = self._proxy_port
        res["proxy_error"] = self._proxy_error
        return res

    async def _metrics(self, _params: dict) -> dict:
        return await asyncio.to_thread(
            observability.collect_metrics, self.registry, self.procman, self.admission
        )

    async def _gc(self, params: dict) -> dict:
        # teardown calls can block on a real server round-trip, so keep it off the loop
        return await asyncio.to_thread(ops.gc, self.registry, params)

    def _settings_payload(self) -> dict:
        from dataclasses import asdict

        return {
            **asdict(self.settings),
            # ports are read at boot; a change to them only lands on the next daemon start
            "proxy_port_live": self._proxy_port,
        }

    async def _settings_get(self, _params: dict) -> dict:
        return self._settings_payload()

    async def _settings_set(self, params: dict) -> dict:
        """Persist a partial settings change and apply what can take effect live.

        Reserves apply immediately (admission reads them on the next check); port changes
        are recorded but only bind on the next daemon start, which the reply flags."""
        changes = {k: v for k, v in params.items() if v is not None}
        try:
            self.settings = await asyncio.to_thread(lambda: settings.update(**changes))
        except settings.SettingsError as exc:
            raise rpc.RpcFault("bad_request", str(exc)) from exc
        self.admission.set_reserves(self.settings.ram_reserve_pct, self.settings.disk_reserve_gb)
        payload = self._settings_payload()
        # only claim a restart is needed when a port value actually differs from what's bound
        payload["restart_required"] = self.settings.proxy_port != self._proxy_port and bool(
            {"proxy_port", "https_port"} & set(changes)
        )
        return payload

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            if not line:
                return
            req = json.loads(line.decode())
            if req.get("token") != self._token:
                resp = {"ok": False, "error": {"code": "unauthorized", "message": "bad token"}}
            else:
                resp = await self.router.dispatch(req.get("method"), req.get("params") or {})
            writer.write((json.dumps(resp) + "\n").encode())
            await writer.drain()
        except (json.JSONDecodeError, ConnectionError):
            pass
        finally:
            writer.close()

    async def serve(self) -> None:
        # Resolved once and reused on the way out: socket_path() can fall back to a
        # directory it creates, which is not something to redo inside a `finally`.
        sock_path = paths.socket_path() if ipc.USE_UNIX else None
        if sock_path is not None:
            sock_path.parent.mkdir(parents=True, exist_ok=True)
            _unlink(sock_path)
            server = await asyncio.start_unix_server(self._handle, path=str(sock_path))
            info = ipc.DaemonInfo(
                transport="unix", token=self._token, pid=os.getpid(), path=str(sock_path)
            )
        else:
            server = await asyncio.start_server(self._handle, host="127.0.0.1", port=0)
            port = server.sockets[0].getsockname()[1]
            info = ipc.DaemonInfo(
                transport="tcp", token=self._token, pid=os.getpid(), host="127.0.0.1", port=port
            )

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            # signals aren't settable on some platforms (e.g. Windows)
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.add_signal_handler(sig, self._stop.set)

        proxy_port = self.settings.proxy_port
        if proxy_port > 0:
            try:
                await self.proxy.start(proxy_port)
                self._proxy_port = proxy_port
            except OSError as exc:
                # The daemon still serves RPC and per-service ports; only hostname routing
                # is lost. Record *why* (privileged :80 without root, port already taken)
                # so `up`/`doctor` can say so instead of the user finding dead hostnames.
                self._proxy_error = f":{proxy_port} — {exc}"
                observability.log_event("proxy_bind_failed", port=proxy_port, error=str(exc))
        pump_task = asyncio.create_task(self._pump_loop())
        supervise_task = asyncio.create_task(self._supervise_loop())
        ipc.write_daemon_info(info)
        try:
            async with server:
                await self._stop.wait()
                # Retract the endpoint here, *inside* the `async with`. Leaving the
                # block awaits server.wait_closed(), which on Windows' Proactor loop
                # can stall until the in-flight shutdown connection drains — while
                # the listening socket is already closed, so is_running() reads
                # False. Clearing only in `finally` (after wait_closed) therefore
                # left a window where the daemon was unreachable but daemon.json was
                # still on disk, which is exactly what a caller polling stop() saw.
                ipc.clear_daemon_info()
        finally:
            # Idempotent, and covers the path where _stop was never set (a crash on
            # startup) so the endpoint file is never left behind.
            ipc.clear_daemon_info()
            if sock_path is not None:
                _unlink(sock_path)
            with contextlib.suppress(Exception):
                await self.proxy.stop()
            if self._caddy is not None:
                self._caddy.stop()
            for task in (pump_task, supervise_task):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            # Deliberately do NOT stop the worktree processes here. The daemon exiting is
            # not a signal to tear down running work: `binky daemon stop`/`restart` must
            # leave worktrees alive so the next daemon *adopts* them on the way back up
            # (C3) — the documented "worktrees keep running; the daemon reconciles them"
            # contract. Children are spawned in their own session/process group precisely
            # so they survive our exit; `down`/`remove` are the verbs that stop a worktree.
            self.registry.close()


def _unlink(path) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)


def run() -> None:
    """Foreground serve — the entry point for the spawned daemon process."""
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(Daemon().serve())


# --------------------------------------------------------------------------- #
#  lifecycle (called from the CLI process)
# --------------------------------------------------------------------------- #
def is_running() -> bool:
    if ipc.read_daemon_info() is None:
        return False
    try:
        ipc.call("ping", timeout=1.0)
        return True
    except (ipc.DaemonNotRunning, ipc.RpcError, OSError):
        return False


def _startup_failure(reason: str) -> str:
    """Fold whatever the daemon managed to say into the error the caller sees."""
    log = paths.daemon_log_path()
    text = ""
    with contextlib.suppress(OSError):
        text = log.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return f"{reason}; it wrote nothing to {log}"
    tail = "\n".join(text.splitlines()[-15:])
    return f"{reason}. Last output from {log}:\n{tail}"


def _spawn_kwargs() -> dict:
    """Popen kwargs that launch the daemon windowless and independent of the CLI.

    On Windows the daemon is a console-subsystem process; ``DETACHED_PROCESS`` cut
    it loose from the parent's console but left Windows free to allocate it a fresh
    one, which popped up as a blank ``cmd`` window that stuck around for the whole
    life of the daemon. ``CREATE_NO_WINDOW`` — the same flag every other child the
    daemon spawns already uses (see :mod:`binky.procs`) — gives it its own *hidden*
    console instead: no window, and still detached from the launching terminal, so
    closing that terminal doesn't take the daemon down. Off Windows we start a new
    session to detach it.
    """
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = procs.NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    return kwargs


def start(timeout: float = 10.0) -> None:
    """Spawn the daemon detached and wait until it is reachable. Idempotent."""
    if is_running():
        return
    kwargs = _spawn_kwargs()

    # The daemon outlives this process, so its stderr can't be a pipe — nobody
    # would be left to drain it. Sending it to DEVNULL was worse: it made a
    # daemon that died on startup indistinguishable from one that was merely
    # slow, and the caller got a bare timeout with the traceback thrown away.
    log = paths.daemon_log_path()
    err = None
    with contextlib.suppress(OSError):
        log.parent.mkdir(parents=True, exist_ok=True)
        err = log.open("wb")
    kwargs["stderr"] = err or subprocess.DEVNULL
    try:
        proc = subprocess.Popen([sys.executable, "-m", "binky", "daemon", "serve"], **kwargs)
    finally:
        if err is not None:
            err.close()  # the child inherited its own handle

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_running():
            return
        # Don't sit out the whole timeout for a process that is already gone.
        if proc.poll() is not None:
            raise RuntimeError(_startup_failure(f"daemon exited with code {proc.returncode}"))
        time.sleep(0.05)
    raise RuntimeError(_startup_failure("daemon did not become ready in time"))


def stop(timeout: float = 5.0) -> bool:
    """Ask the daemon to shut down. Returns True if it was running."""
    if not is_running():
        return False
    with contextlib.suppress(ipc.DaemonNotRunning, ipc.RpcError, OSError):
        ipc.call("shutdown", timeout=2.0)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # Both conditions. The daemon stops answering the moment it closes its
        # listening socket, which is before it has retracted its endpoint file;
        # returning on the ping alone hands the caller a daemon.json still on disk.
        if not is_running() and ipc.read_daemon_info() is None:
            return True
        time.sleep(0.05)
    return True


def ensure() -> None:
    """Start the daemon if it isn't already running (lazy auto-start)."""
    start()
