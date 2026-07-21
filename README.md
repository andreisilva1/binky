# 🐇 Warren

**Give every git worktree its own isolated local world — ports, env, database, URLs — so a swarm of coding agents runs in parallel on one machine without anything colliding.**

`docker-compose` was built for one process at a time. Warren is built for fifteen at once.

> Status: **alpha**. The core works end-to-end (worktree isolation, ports, adapters, supervision,
> reconciler, admission, proxy). HTTPS, native Windows, and packaging polish are still coming.

## Install

Warren needs **Python 3.11+** and **git**. From source:

```bash
uv tool install .            # or: pipx install .
warren --version
```

For the Postgres adapter, install the extra: `uv tool install ".[postgres]"`.
On **Windows**, run Warren inside **WSL2** (the design is Unix-first; native Windows is a later port).

## Quickstart

Describe your services once, in a committed `warren.toml`:

```toml
[project]
name = "acme"

[services.web]
cmd = "pnpm dev --port ${self.port}"
url = "web"                              # → web.<worktree>.acme.localhost
depends_on = ["api"]

[services.api]
cmd = "go run ./cmd/server"
depends_on = ["db"]
env = { DATABASE_URL = "${db.url}" }

[services.db]
adapter = "postgres"                     # a private database per worktree
admin_url = "${env.POSTGRES_ADMIN_URL}"
migrate = "make migrate"
```

Then bring worktrees up in parallel — each gets its own ports, database, URLs and logs:

```bash
warren up login-fix
warren up checkout-v2
warren status               # every worktree on the machine, grouped by project
warren down login-fix       # pause (keeps files + ports); `up` again is instant
warren remove login-fix     # destroy it (refuses if there are uncommitted changes)
```

## Commands

| Command | What it does |
|---|---|
| `warren up <name> [--group g]` | Create/use the branch's worktree, allocate ports, provision resources, start services |
| `warren down <name>` | Stop the worktree's processes (keeps its files and ports) |
| `warren remove <name> [--force]` | Destroy the worktree — stop, drop its slices, remove its files |
| `warren status [.]` | List worktrees (grouped by project); `.` = current project only |
| `warren logs <name>` | Print a worktree's captured service logs |
| `warren dashboard` | Live TUI of every worktree |
| `warren check [path]` | Validate a `warren.toml` |
| `warren daemon start\|stop\|status` | Manage the background daemon |

## How it works

A single machine-global daemon sits between your worktrees and the machine:

1. **Worktree** — creates/uses a git worktree per name.
2. **Ports** — a global allocator assigns every process a port *before anything starts* and injects the
   whole map, so nothing collides (`${self.port}`, `${api.url}`, …).
3. **Resources** — adapters carve an isolated slice (a Postgres database per worktree) and tear it down.
4. **Supervision** — starts services in `depends_on` order, waits for health, captures logs.
5. **Proxy** — routes `<service>.<worktree>.<domain>` to the right port (WebSockets/HMR included).
6. **Admission** — a FIFO queue bounded by `max_parallel` and free RAM, so the swarm doesn't melt the box.

State lives in `~/.warren/` (a SQLite registry); nothing runs in the cloud.

## Development

```bash
python -m venv .venv && . .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -e ".[dev]"
pytest
ruff check src tests
```

The Postgres adapter's integration test runs only when `WARREN_TEST_PG` points at a Postgres admin URL.

## License

MIT — see [LICENSE](LICENSE).
