"""Domain operations behind the daemon's RPC methods.

``up`` / ``down`` / ``remove`` / ``status`` — git worktrees, health + transactional
rollback, the startup reconciler and resource adapters are all wired in here.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import subprocess
from pathlib import Path

from warren import adapters, allocator, git, health, paths, resolve
from warren.config import ConfigError, load_config
from warren.registry import Registry, WorktreeStatus
from warren.rpc import RpcFault
from warren.supervisor import ProcessManager, kill_pid


def _load(root: str | None):
    if not root:
        raise RpcFault("bad_request", "missing 'root'")
    try:
        return load_config(Path(root) / "warren.toml")
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


def _provision_adapters(
    reg, cfg, wt, project: str, name: str, ports: dict, services: dict
) -> dict[str, str]:
    """Provision each adapter service's slice, run its migrate/seed, return addresses."""
    addresses = {sl.service: sl.address for sl in reg.slices_for(wt.id) if sl.address}
    sl_name = adapters.slice_name(project, name)
    for sname, svc in services.items():
        if not svc.adapter or sname in addresses:
            continue
        admin = resolve.resolve(svc.admin_url, sname, ports, name) if svc.admin_url else None
        try:
            adapter = adapters.get(svc.adapter, {"admin_url": admin})
            adapter.provision(sl_name)
            address = adapter.resolve(sl_name)
        except adapters.AdapterError as exc:
            raise RpcFault("adapter", str(exc)) from exc
        slice_row = reg.create_slice(wt.id, sname, svc.adapter)
        reg.set_slice_ready(slice_row.id, address)
        addresses[sname] = address
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


def _teardown_slices(reg, cfg, wt) -> None:
    """Drop every adapter slice this worktree owns (best-effort)."""
    sl_name = adapters.slice_name(cfg.project.name, wt.name)
    for sl in reg.slices_for(wt.id):
        svc = cfg.services.get(sl.service)
        if not svc or not svc.adapter:
            continue
        admin = resolve.resolve(svc.admin_url, sl.service, {}, wt.name) if svc.admin_url else None
        with contextlib.suppress(Exception):
            adapters.get(svc.adapter, {"admin_url": admin}).teardown(sl_name)


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
    marker = paths.warren_home() / "setup" / project / worktree / sname
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
    return {"project": cfg.project.name, "worktree": name, "removed": True}


def status(reg: Registry, procman: ProcessManager, _params: dict) -> dict:
    worktrees = [
        {
            "project": wt.project,
            "worktree": wt.name,
            "status": wt.status,
            "running": procman.is_running(wt.id),
            "health": procman.worktree_health(wt.id),
            "ports": [
                {"service": p.service, "label": p.label, "port": p.port}
                for p in reg.ports_for(wt.id)
            ],
        }
        for wt in reg.list_worktrees()
    ]
    return {"worktrees": worktrees}
