"""Domain operations behind the daemon's RPC methods.

``up`` / ``down`` / ``remove`` / ``status`` — git worktrees, health + transactional
rollback, the startup reconciler and resource adapters are all wired in here.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

from binky import adapters, allocator, git, health, observability, paths, resolve
from binky.config import ConfigError, load_config
from binky.registry import Registry, WorktreeStatus
from binky.rpc import RpcFault
from binky.supervisor import ProcessManager, kill_pid

# A worktree name becomes a git branch, a directory, and the ``${worktree}`` token
# that flows into shell commands — so restrict it to a shell-safe, path-safe charset.
_SAFE_NAME = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._/-]*")


def _validate_name(name: str) -> None:
    if ".." in name or name.startswith("/") or not _SAFE_NAME.fullmatch(name):
        raise RpcFault(
            "bad_name",
            f"invalid worktree name {name!r}: use letters, digits, '. _ - /' (no '..')",
        )


def _load(root: str | None):
    if not root:
        raise RpcFault("bad_request", "missing 'root'")
    try:
        return load_config(Path(root) / "binky.toml")
    except ConfigError as exc:
        raise RpcFault("config", str(exc)) from exc


def _topo_order(services: dict) -> list[str]:
    """Services in dependency order (a service after everything it depends on)."""
    order: list[str] = []
    done: set[str] = set()

    def visit(name: str, stack: set[str]) -> None:
        if name in done:
            return
        if name in stack:
            raise RpcFault("cycle", f"dependency cycle involving '{name}'")
        stack.add(name)
        for dep in services[name].depends_on:
            if dep in services:
                visit(dep, stack)
        stack.discard(name)
        done.add(name)
        order.append(name)

    for name in services:
        visit(name, set())
    return order


# One lock per golden so a shared template is built exactly once under concurrency;
# late worktrees block briefly, then see the ready marker and clone immediately.
_GOLDEN_LOCKS: dict[str, threading.Lock] = {}
_GOLDEN_LOCKS_GUARD = threading.Lock()


def _golden_lock(key: str) -> threading.Lock:
    with _GOLDEN_LOCKS_GUARD:
        return _GOLDEN_LOCKS.setdefault(key, threading.Lock())


def _golden_slice_name(project: str, clone_from: str) -> str:
    return adapters.slice_name(project, f"golden_{clone_from}")


def _adapter_config(svc, sname: str, ports: dict, worktree: str) -> dict:
    """The flat config dict handed to an adapter (and to external adapters as JSON).

    ``options`` is flattened in rather than nested so an adapter reads one plain
    dict; the reserved keys below always win, so an option can never shadow them.
    Every value goes through ``${env.X}`` resolution, which is how credentials stay
    out of a committed ``binky.toml``.
    """
    options = {k: resolve.resolve(v, sname, ports, worktree) for k, v in svc.options.items()}
    return {
        **options,
        "admin_url": resolve.resolve(svc.admin_url, sname, ports, worktree)
        if svc.admin_url
        else None,
        "buckets": list(svc.buckets),
    }


def _ensure_golden(adapter, svc, sname: str, wt, project: str) -> str:
    """Build (once) the golden template from the service's migrate+seed, and return it.

    The golden is a resource slice nobody owns; it's rebuilt only when migrate/seed
    change (tracked by a hashed marker), and never torn down by a worktree remove.
    """
    golden = _golden_slice_name(project, svc.clone_from)
    digest = hashlib.sha256(f"{svc.migrate}\x00{svc.seed}".encode()).hexdigest()
    marker = paths.binky_home() / "golden" / svc.adapter.split("@", 1)[0] / golden
    with _golden_lock(golden):
        if marker.exists() and marker.read_text(encoding="utf-8").strip() == digest:
            return golden  # already built at this migrate+seed
        with contextlib.suppress(Exception):
            adapter.teardown(golden)  # rebuild from scratch on a migrate/seed change
        adapter.provision(golden)
        golden_address = adapter.resolve(golden)
        _run_hook(svc.migrate, "migrate", sname, wt, svc, golden_address)
        _run_hook(svc.seed, "seed", sname, wt, svc, golden_address)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(digest, encoding="utf-8")
    return golden


def _try_clone(adapter, svc, sname: str, wt, project: str, sl_name: str) -> bool:
    """Clone from the golden template; return False if the caller must fall back.

    ``capabilities()`` is a promise about the *adapter*, not about the machine it runs
    on — a clone can still fail on something environmental (a client binary too old to
    authenticate, a missing grant, a server setting). Correctness never depends on
    cloning: provisioning the slice and running migrate+seed per worktree produces the
    same world, only slower. So a failed clone degrades instead of failing ``up``, and
    says so in the event log rather than silently.
    """
    try:
        golden = _ensure_golden(adapter, svc, sname, wt, project)
        adapter.clone(golden, sl_name)
        return True
    except adapters.AdapterError as exc:
        observability.log_event(
            "clone_fallback",
            project=project,
            worktree=wt.name,
            service=sname,
            adapter=svc.adapter,
            error=str(exc),
        )
        # A half-copied slice must never be migrated into — start the slow path clean.
        with contextlib.suppress(Exception):
            adapter.teardown(sl_name)
        return False


def _provision_adapters(
    reg, cfg, wt, project: str, name: str, ports: dict, services: dict
) -> dict[str, str]:
    """Provision each adapter service's slice and return addresses.

    With ``clone_from`` (and a clone-capable adapter) the slice is a ~instant copy of
    a golden template that already holds migrate+seed; otherwise it's provisioned and
    migrated/seeded per worktree. A ``clone_from`` falls back to that per-worktree path
    both when the adapter can't clone and when a clone attempt fails (see
    :func:`_try_clone`).
    """
    addresses = {sl.service: sl.address for sl in reg.slices_for(wt.id) if sl.address}
    sl_name = adapters.slice_name(project, name)
    for sname, svc in services.items():
        if not svc.adapter or sname in addresses:
            continue
        try:
            adapter = adapters.get(svc.adapter, _adapter_config(svc, sname, ports, name))
            cloned = bool(svc.clone_from) and bool(adapter.capabilities().get("clone"))
            if cloned:
                cloned = _try_clone(adapter, svc, sname, wt, project, sl_name)
            if not cloned:
                adapter.provision(sl_name)
            address = adapter.resolve(sl_name)
        except adapters.AdapterError as exc:
            raise RpcFault("adapter", str(exc)) from exc
        slice_row = reg.create_slice(wt.id, sname, svc.adapter)
        reg.set_slice_ready(slice_row.id, address)
        addresses[sname] = address
        if not cloned:  # a cloned slice already has migrate+seed baked into the golden
            _run_hook(svc.migrate, "migrate", sname, wt, svc, address)
            _run_hook(svc.seed, "seed", sname, wt, svc, address)
    return addresses


def _run_hook(command: str | None, kind: str, sname: str, wt, svc, address: str) -> None:
    if not command:
        return
    result = subprocess.run(
        command,
        cwd=str(Path(wt.path) / (svc.workdir or ".")),
        shell=True,
        capture_output=True,
        text=True,
        env={**os.environ, "DATABASE_URL": address},
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RpcFault(kind, f"'{sname}' {kind} failed: {detail}")


def _url_target(sname: str, svc) -> tuple[str | None, str]:
    """The (subdomain, port-label) a service is exposed under, or (None, "")."""
    url = svc.url
    if not url:
        return None, ""
    if url is True:
        return sname, ""
    if isinstance(url, str):
        return url, ""
    for label, sub in url.items():  # dict form {label: subdomain}
        return sub, label
    return None, ""


def _routes(cfg, worktree: str, ports: dict, services: dict) -> dict[str, int]:
    """Hostname -> port for every ``url`` service (``<sub>.<worktree>.<domain>``)."""
    domain = cfg.project.domain or f"{cfg.project.name}.localhost"
    out: dict[str, int] = {}
    for sname, svc in services.items():
        sub, label = _url_target(sname, svc)
        if sub is None:
            continue
        port = ports.get((sname, label)) if label else resolve.primary_port(ports, sname)
        if port:
            out[f"{sub}.{worktree}.{domain}"] = port
    return out


def _exposed(cfg, wt, ports: dict, addresses: dict) -> dict[str, str]:
    """The curated live coordinates handed to agents (``[agents] expose``).

    Empty unless a service is listed; a resource yields its address (``${db.url}``),
    a url service its ``<scheme>://<sub>.<worktree>.<domain>`` route.
    """
    expose = cfg.agents.expose if cfg.agents else []
    if not expose:
        return {}
    scheme = "https" if cfg.project.https else "http"
    domain = cfg.project.domain or f"{cfg.project.name}.localhost"
    out: dict[str, str] = {}
    for name in expose:
        svc = cfg.services.get(name)
        if svc is None:
            continue
        if svc.adapter:
            if name in addresses:
                out[name] = addresses[name]
            continue
        sub, label = _url_target(name, svc)
        if sub is not None:
            out[name] = f"{scheme}://{sub}.{wt.name}.{domain}"
        else:
            port = ports.get((name, label)) if label else resolve.primary_port(ports, name)
            if port:
                out[name] = f"http://localhost:{port}"
    return out


def _worktree_coordinates(reg, cfg, wt) -> dict[str, str]:
    ports = {(p.service, p.label): p.port for p in reg.ports_for(wt.id)}
    addresses = {sl.service: sl.address for sl in reg.slices_for(wt.id) if sl.address}
    return _exposed(cfg, wt, ports, addresses)


def env(reg: Registry, params: dict) -> dict:
    """The worktree's exposed coordinates, for an agent/orchestrator to consume."""
    name = params.get("worktree")
    if not name:
        raise RpcFault("bad_request", "missing 'worktree'")
    cfg = _load(params.get("root"))
    wt = reg.get_worktree(cfg.project.name, name)
    if wt is None:
        raise RpcFault("not_found", f"no worktree '{name}'")
    coords = _worktree_coordinates(reg, cfg, wt)
    return {"project": cfg.project.name, "worktree": name, "env": coords}


def verify(reg: Registry, procman: ProcessManager, params: dict) -> dict:
    """Run ``[agents] verify`` against the live worktree and record the verdict.

    Binky is the substrate, so it doesn't decide *when* work is done — a driver
    calls this at its done moment. It runs the checks against the exposed env and
    persists ``passing``/``failing`` as worktree state; the exit verdict is the gate.
    """
    name = params.get("worktree")
    if not name:
        raise RpcFault("bad_request", "missing 'worktree'")
    cfg = _load(params.get("root"))
    wt = reg.get_worktree(cfg.project.name, name)
    if wt is None:
        raise RpcFault("not_found", f"no worktree '{name}'")
    checks = cfg.agents.verify if cfg.agents else []
    if not checks:
        raise RpcFault("no_verify", "no [agents] verify commands defined")

    exposed = _worktree_coordinates(reg, cfg, wt)
    check_env = {**os.environ, **{f"{n.upper()}_URL": v for n, v in exposed.items()}}
    results = []
    passed_all = True
    for command in checks:
        proc = subprocess.run(
            command, cwd=wt.path, shell=True, capture_output=True, text=True,
            env=check_env, check=False,
        )
        ok = proc.returncode == 0
        passed_all = passed_all and ok
        output = (proc.stdout + proc.stderr).strip()[-2000:]
        results.append({"command": command, "passed": ok, "output": output})
    verdict = "passing" if passed_all else "failing"
    reg.set_verify_status(wt.id, verdict)
    return {
        "project": cfg.project.name, "worktree": name,
        "verify": verdict, "passed": passed_all, "checks": results,
    }


def _teardown_slices(reg, cfg, wt) -> None:
    """Drop every adapter slice this worktree owns (best-effort)."""
    sl_name = adapters.slice_name(cfg.project.name, wt.name)
    for sl in reg.slices_for(wt.id):
        svc = cfg.services.get(sl.service)
        if not svc or not svc.adapter:
            continue
        with contextlib.suppress(Exception):
            config = _adapter_config(svc, sl.service, {}, wt.name)
            adapters.get(svc.adapter, config).teardown(sl_name)


def _select_services(cfg, group: str | None) -> dict:
    """All services, or a group's members plus their transitive dependencies."""
    if not group:
        return dict(cfg.services)
    members = cfg.groups.get(group)
    if members is None:
        raise RpcFault("unknown_group", f"no group '{group}'")
    if members == "*":
        return dict(cfg.services)
    wanted: set[str] = set()

    def add(n: str) -> None:
        if n in wanted or n not in cfg.services:
            return
        wanted.add(n)
        for dep in cfg.services[n].depends_on:
            add(dep)

    for member in members:
        add(member)
    return {n: s for n, s in cfg.services.items() if n in wanted}


def _load_env_files(files: list[str], cwd: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for rel in files:
        path = Path(cwd) / rel
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _run_setup(svc, sname: str, project: str, worktree: str, cwd: str, env: dict) -> None:
    """Run a service's `setup` once per worktree (re-run only if the command changed)."""
    if not svc.setup:
        return
    marker = paths.binky_home() / "setup" / project / worktree / sname
    digest = hashlib.sha256(svc.setup.encode()).hexdigest()
    if marker.exists() and marker.read_text(encoding="utf-8").strip() == digest:
        return
    result = subprocess.run(
        svc.setup, cwd=cwd, shell=True, env={**os.environ, **env},
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RpcFault("setup", f"'{sname}' setup failed: {detail}")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(digest, encoding="utf-8")


def _health_check(sname: str, svc, ports: dict, worktree: str, cwd: str):
    """A bound one-shot health probe for continuous supervision (no late-binding)."""
    return lambda: health.check_once(sname, svc, ports, worktree, cwd)


def _run_lifecycle_hook(command: str | None, wt_path: str, worktree: str) -> None:
    """Best-effort `post_up` / `pre_down` hook (runs in the worktree dir)."""
    if not command:
        return
    resolved = resolve.resolve(command, "", {}, worktree)
    with contextlib.suppress(Exception):
        subprocess.run(resolved, cwd=wt_path, shell=True, capture_output=True, check=False)


def up(reg: Registry, procman: ProcessManager, params: dict) -> dict:
    name = params.get("worktree")
    if not name:
        raise RpcFault("bad_request", "missing 'worktree'")
    _validate_name(name)
    root = params.get("root")
    cfg = _load(root)
    project = cfg.project.name
    if not root or not git.is_repo(root):
        raise RpcFault("not_git", f"{root} is not a git repository (run `git init` first)")
    services = _select_services(cfg, params.get("group"))

    wt = reg.get_worktree(project, name)
    created_worktree = False
    if wt is None:
        wt_path = paths.worktrees_dir() / project / name
        if not wt_path.exists():
            try:
                git.add_worktree(root, wt_path, name)
            except git.GitError as exc:
                raise RpcFault("git", str(exc)) from exc
        wt = reg.create_worktree(project, name, str(wt_path), name)
        created_worktree = True
    if procman.is_running(wt.id):
        raise RpcFault("already_up", f"'{name}' is already up")

    # everything below is transactional: on any failure, undo what THIS up created
    try:
        # ports: reuse what's recorded, allocate anything missing
        ports: dict[tuple[str, str], int] = {
            (p.service, p.label): p.port for p in reg.ports_for(wt.id)
        }
        missing = [
            req
            for sname, svc in services.items()
            for req in resolve.port_requests(sname, svc)
            if req not in ports
        ]
        if missing:
            rng = tuple(cfg.project.port_range) if cfg.project.port_range else None
            ports.update(allocator.allocate(reg, wt.id, project, name, missing, rng))

        addresses = _provision_adapters(reg, cfg, wt, project, name, ports, services)

        started = []
        for sname in _topo_order(services):
            svc = services[sname]
            if not svc.cmd:
                continue  # adapters (no cmd) are provisioned separately, above
            cwd = str(Path(wt.path) / (svc.workdir or "."))
            env = {
                **_load_env_files(svc.env_file, cwd),
                **resolve.build_env(sname, svc, ports, name, addresses),
            }
            _run_setup(svc, sname, project, name, cwd, env)
            pid = procman.start(
                wt.id,
                sname,
                resolve.resolve_command(sname, svc, ports, name, addresses),
                cwd,
                env,
                paths.logs_dir() / project / name / f"{sname}.log",
                svc.limits or cfg.defaults.limits,
                (svc.restart or cfg.defaults.restart).value,
                _health_check(sname, svc, ports, name, cwd),
            )
            reg.add_process(wt.id, pid)
            timeout = health.timeout_for(svc)
            if not health.wait_healthy(sname, svc, ports, name, cwd, timeout):
                raise RpcFault("unhealthy", f"'{sname}' did not become healthy within {timeout:g}s")
            started.append(
                {"service": sname, "pid": pid, "port": resolve.primary_port(ports, sname)}
            )

        reg.set_worktree_status(wt.id, WorktreeStatus.ready)
        if cfg.hooks:
            _run_lifecycle_hook(cfg.hooks.post_up, wt.path, name)
        return {
            "project": project,
            "worktree": name,
            "services": started,
            "routes": _routes(cfg, name, ports, services),
        }
    except Exception:
        _rollback(reg, procman, cfg, wt, root, created_worktree)
        raise


def _rollback(reg, procman, cfg, wt, root, created_worktree: bool) -> None:
    """Best-effort undo of a failed ``up`` (never touches a pre-existing worktree)."""
    with contextlib.suppress(Exception):
        procman.stop(wt.id)
    if created_worktree:
        _teardown_slices(reg, cfg, wt)
        with contextlib.suppress(Exception):
            reg.release_ports(wt.id)
        with contextlib.suppress(Exception):
            if Path(wt.path).exists():
                git.remove_worktree(root, wt.path, force=True)
        with contextlib.suppress(Exception):
            reg.delete_worktree(wt.id)
    else:
        with contextlib.suppress(Exception):
            reg.clear_processes(wt.id)
        with contextlib.suppress(Exception):
            reg.set_worktree_status(wt.id, WorktreeStatus.stopped)


def reconcile(reg: Registry) -> None:
    """On daemon startup, converge the registry against reality.

    The daemon manages no processes yet, so any recorded PIDs are orphans from
    before the restart — kill them. Then: half-created ``provisioning`` worktrees
    are swept (C); ``ready`` worktrees (whose processes just died with the daemon)
    drop to ``stopped``; ``stopped`` worktrees are left untouched (never GC'd).
    """
    for wt in reg.list_worktrees():
        for pid in reg.processes_for(wt.id):
            kill_pid(pid)
        reg.clear_processes(wt.id)
        if wt.status == WorktreeStatus.provisioning:
            with contextlib.suppress(Exception):
                reg.release_ports(wt.id)
            with contextlib.suppress(Exception):
                reg.delete_worktree(wt.id)
        elif wt.status == WorktreeStatus.ready:
            reg.set_worktree_status(wt.id, WorktreeStatus.stopped)


def down(reg: Registry, procman: ProcessManager, params: dict) -> dict:
    name = params.get("worktree")
    if not name:
        raise RpcFault("bad_request", "missing 'worktree'")
    cfg = _load(params.get("root"))
    wt = reg.get_worktree(cfg.project.name, name)
    if wt is None:
        raise RpcFault("not_found", f"no worktree '{name}'")
    if cfg.hooks:
        _run_lifecycle_hook(cfg.hooks.pre_down, wt.path, name)
    stopped = procman.stop(wt.id)
    reg.clear_processes(wt.id)
    reg.set_worktree_status(wt.id, WorktreeStatus.stopped)  # down = pause; keeps ports
    return {"project": cfg.project.name, "worktree": name, "stopped": stopped}


def remove(reg: Registry, procman: ProcessManager, params: dict) -> dict:
    name = params.get("worktree")
    if not name:
        raise RpcFault("bad_request", "missing 'worktree'")
    root = params.get("root")
    cfg = _load(root)
    wt = reg.get_worktree(cfg.project.name, name)
    if wt is None:
        raise RpcFault("not_found", f"no worktree '{name}'")

    wt_path = Path(wt.path)
    force = bool(params.get("force"))
    if wt_path.exists() and git.is_dirty(wt_path) and not force:
        raise RpcFault("dirty", f"'{name}' has uncommitted changes (use --force to discard)")

    if cfg.hooks:
        _run_lifecycle_hook(cfg.hooks.pre_down, wt.path, name)
    procman.stop(wt.id)
    _teardown_slices(reg, cfg, wt)
    if wt_path.exists():
        try:
            git.remove_worktree(root, wt_path, force=True)
        except git.GitError as exc:
            raise RpcFault("git", str(exc)) from exc
    reg.release_ports(wt.id)
    reg.delete_worktree(wt.id)  # cascades to slices
    # retention: a removed worktree keeps no logs around
    shutil.rmtree(paths.logs_dir() / cfg.project.name / name, ignore_errors=True)
    return {"project": cfg.project.name, "worktree": name, "removed": True}


def status(reg: Registry, procman: ProcessManager, _params: dict) -> dict:
    worktrees = [
        {
            "project": wt.project,
            "worktree": wt.name,
            "status": wt.status,
            "running": procman.is_running(wt.id),
            "health": procman.worktree_health(wt.id),
            "verify": wt.verify_status,
            "ports": [
                {"service": p.service, "label": p.label, "port": p.port}
                for p in reg.ports_for(wt.id)
            ],
        }
        for wt in reg.list_worktrees()
    ]
    return {"worktrees": worktrees}
