"""Domain operations behind the daemon's RPC methods.

``up`` / ``down`` / ``remove`` / ``status``. Git worktrees (Phase 4), health +
transactional rollback (Phase 5), the startup reconciler (Phase 6) and resource
adapters (Phase 7) are wired in; the reverse proxy is Phase 9.
"""

from __future__ import annotations

import contextlib
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


def _provision_adapters(reg, cfg, wt, project: str, name: str, ports: dict) -> dict[str, str]:
    """Provision each adapter service's slice, run its migrate/seed, return addresses."""
    addresses = {sl.service: sl.address for sl in reg.slices_for(wt.id) if sl.address}
    sl_name = adapters.slice_name(project, name)
    for sname, svc in cfg.services.items():
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


def up(reg: Registry, procman: ProcessManager, params: dict) -> dict:
    name = params.get("worktree")
    if not name:
        raise RpcFault("bad_request", "missing 'worktree'")
    root = params.get("root")
    cfg = _load(root)
    project = cfg.project.name
    if not root or not git.is_repo(root):
        raise RpcFault("not_git", f"{root} is not a git repository (run `git init` first)")

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
            for sname, svc in cfg.services.items()
            for req in resolve.port_requests(sname, svc)
            if req not in ports
        ]
        if missing:
            rng = tuple(cfg.project.port_range) if cfg.project.port_range else None
            ports.update(allocator.allocate(reg, wt.id, project, name, missing, rng))

        addresses = _provision_adapters(reg, cfg, wt, project, name, ports)

        started = []
        for sname in _topo_order(cfg.services):
            svc = cfg.services[sname]
            if not svc.cmd:
                continue  # adapters (no cmd) are wired in Phase 7
            cwd = str(Path(wt.path) / (svc.workdir or "."))
            pid = procman.start(
                wt.id,
                resolve.resolve_command(sname, svc, ports, name, addresses),
                cwd,
                resolve.build_env(sname, svc, ports, name, addresses),
                paths.logs_dir() / project / name / f"{sname}.log",
            )
            reg.add_process(wt.id, pid)
            timeout = health.timeout_for(svc)
            if not health.wait_healthy(sname, svc, ports, name, cwd, timeout):
                raise RpcFault("unhealthy", f"'{sname}' did not become healthy within {timeout:g}s")
            started.append(
                {"service": sname, "pid": pid, "port": resolve.primary_port(ports, sname)}
            )

        reg.set_worktree_status(wt.id, WorktreeStatus.ready)
        return {"project": project, "worktree": name, "services": started}
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
    """On daemon startup, converge the registry against reality (THESIS §8.4).

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
    stopped = procman.stop(wt.id)
    reg.clear_processes(wt.id)
    reg.set_worktree_status(wt.id, WorktreeStatus.stopped)  # down = pause; keeps ports
    return {"worktree": name, "stopped": stopped}


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

    procman.stop(wt.id)
    _teardown_slices(reg, cfg, wt)
    if wt_path.exists():
        try:
            git.remove_worktree(root, wt_path, force=True)
        except git.GitError as exc:
            raise RpcFault("git", str(exc)) from exc
    reg.release_ports(wt.id)
    reg.delete_worktree(wt.id)  # cascades to slices
    return {"worktree": name, "removed": True}


def status(reg: Registry, procman: ProcessManager, _params: dict) -> dict:
    worktrees = [
        {
            "project": wt.project,
            "worktree": wt.name,
            "status": wt.status,
            "running": procman.is_running(wt.id),
            "ports": [
                {"service": p.service, "label": p.label, "port": p.port}
                for p in reg.ports_for(wt.id)
            ],
        }
        for wt in reg.list_worktrees()
    ]
    return {"worktrees": worktrees}
