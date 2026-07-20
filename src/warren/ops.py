"""Domain operations behind the daemon's RPC methods (thin — Phase 3.2).

``up`` / ``down`` / ``status``. Thin: no git worktree, adapters, or proxy yet —
this proves the spine (config -> allocate -> resolve -> spawn -> registry). Git
worktrees arrive in Phase 4, transactional orchestration + rollback in 5.3,
adapters in 7.
"""

from __future__ import annotations

from pathlib import Path

from warren import allocator, git, paths, resolve
from warren.config import ConfigError, load_config
from warren.registry import Registry, WorktreeStatus
from warren.rpc import RpcFault
from warren.supervisor import ProcessManager


def _load(root: str | None):
    if not root:
        raise RpcFault("bad_request", "missing 'root'")
    try:
        return load_config(Path(root) / "warren.toml")
    except ConfigError as exc:
        raise RpcFault("config", str(exc)) from exc


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
    if wt is None:
        wt_path = paths.worktrees_dir() / project / name
        if not wt_path.exists():
            try:
                git.add_worktree(root, wt_path, name)
            except git.GitError as exc:
                raise RpcFault("git", str(exc)) from exc
        wt = reg.create_worktree(project, name, str(wt_path), name)
    if procman.is_running(wt.id):
        raise RpcFault("already_up", f"'{name}' is already up")

    # ports: reuse what's recorded, allocate anything missing
    ports: dict[tuple[str, str], int] = {(p.service, p.label): p.port for p in reg.ports_for(wt.id)}
    missing = [
        req
        for sname, svc in cfg.services.items()
        for req in resolve.port_requests(sname, svc)
        if req not in ports
    ]
    if missing:
        rng = tuple(cfg.project.port_range) if cfg.project.port_range else None
        ports.update(allocator.allocate(reg, wt.id, project, name, missing, rng))

    started = []
    for sname, svc in cfg.services.items():
        if not svc.cmd:
            continue  # adapters (no cmd) are wired in Phase 7
        pid = procman.start(
            wt.id,
            resolve.resolve_command(sname, svc, ports, name),
            str(Path(wt.path) / (svc.workdir or ".")),
            resolve.build_env(sname, svc, ports, name),
        )
        started.append({"service": sname, "pid": pid, "port": resolve.primary_port(ports, sname)})

    reg.set_worktree_status(wt.id, WorktreeStatus.ready)
    return {"project": project, "worktree": name, "services": started}


def down(reg: Registry, procman: ProcessManager, params: dict) -> dict:
    name = params.get("worktree")
    if not name:
        raise RpcFault("bad_request", "missing 'worktree'")
    cfg = _load(params.get("root"))
    wt = reg.get_worktree(cfg.project.name, name)
    if wt is None:
        raise RpcFault("not_found", f"no worktree '{name}'")
    stopped = procman.stop(wt.id)
    reg.set_worktree_status(wt.id, WorktreeStatus.stopped)  # down = pause; keeps ports
    return {"worktree": name, "stopped": stopped}


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
