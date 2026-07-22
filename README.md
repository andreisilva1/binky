# 🐇 Warren

**Give every git worktree its own isolated local world — ports, env, database, URLs — so a swarm of coding agents runs in parallel on one machine without anything colliding.**

`docker-compose` was built for one process at a time. Warren is built for fifteen at once.

> Status: **alpha**. The core works end to end — worktree isolation, ports, adapters, supervision,
> reconciler, admission, proxy, HTTPS. The config format is stable; the CLI may still move.

## The problem

You point three agents at the same repo. The second one runs `pnpm dev` and gets `EADDRINUSE`.
The third runs your migrations and drops the table the first was reading. You start hand-editing
`.env` files, and now you're the scheduler.

Warren is the scheduler. One `warren up <branch>` per agent, and each gets its own ports, its own
database, its own URLs — from the same committed config.

## Install

Python 3.11+ and git. macOS, Linux and Windows (tested on all three in CI).

Not on PyPI yet — install from a clone:

```bash
git clone https://github.com/andreisilva1/warren && cd warren
uv tool install .               # or: pipx install .
warren --version
```

Adapters that need a driver ship as extras — installing Warren for Redis shouldn't pull `boto3`:

```bash
uv tool install ".[postgres]"        # or [redis], [mysql], [mongodb], [s3], [kafka]
uv tool install ".[all-adapters]"    # the whole matrix
```

## Quickstart

Already have a `docker-compose.yml`? Start from it:

```bash
warren init                 # writes a warren.toml you can read and edit
warren check                # validate it
```

Or write it by hand — this is the whole config:

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
seed = "make seed"
clone_from = "golden"                    # migrate+seed once, then copy per worktree
```

Then bring worktrees up in parallel:

```bash
warren up login-fix
warren up checkout-v2
warren status               # every worktree on the machine, grouped by project
warren down login-fix       # pause (keeps files + ports); `up` again is instant
warren remove login-fix     # destroy it (refuses if there are uncommitted changes)
```

Nothing is hardcoded and nothing is discovered at runtime: every process boots already knowing the
whole port map, its own and its dependencies'.

## What it costs to run N worlds

The claim is that N isolated worlds shouldn't cost N times one world. Measured, not asserted —
`benchmarks/worlds.py` is in this repo and reproduces the table below (`postgres:16`, 200,000 rows
per world, Docker Desktop on Windows 11):

| worlds | RAM: a container each | RAM: Warren | time: a container each | time: Warren |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 150 MiB | 174 MiB | 6s | 4s |
| 2 | 298 MiB | 182 MiB | 11s | 6s |
| 4 | 596 MiB | 228 MiB | 20s | 10s |
| 8 | 1197 MiB | 295 MiB | 40s | 16s |

**The 8th world costs +150 MiB and +4.8s as its own container, or +17 MiB and +1.7s under Warren.**

Read the first row too. At **one** world Warren is behind — it pays for a server plus a golden
template nobody is using yet, and only earns that back on the second. And **disk is not a win**:
`CREATE DATABASE ... TEMPLATE` is a file copy, not copy-on-write, so every world is still a full
copy of the rows. What Warren removes is the *server* per world, not the *bytes* per world. It also
doesn't make your own app processes cheaper — one dev server per worktree costs the same either way.

```bash
python benchmarks/worlds.py --sweep 1,2,4,8 --rows 200000
```

## Adapters

An adapter carves an isolated **slice** of one shared server — a database, a key prefix, a bucket —
instead of running one server per worktree. **16 names, 10 implementations, every one exercised
against a real server in CI.**

| slice | adapters |
| --- | --- |
| a database | `postgres` · `mysql` · `mariadb` · `percona` · `mongodb` · `clickhouse` |
| a key prefix | `redis` · `valkey` |
| a bucket / prefix | `s3` · `minio` |
| a vhost / topic prefix | `rabbitmq` · `kafka` · `redpanda` |
| a collection | `qdrant` |
| an index prefix | `elasticsearch` · `opensearch` |

Protocol-compatible names share one implementation rather than a copy of it — `valkey` is the Redis
adapter, `percona` is the MySQL one, `opensearch` is the Elasticsearch one.

### `clone_from`: pay for migrate+seed once

With `clone_from`, the first `warren up` builds a **golden template** — provision, migrate, seed —
and every worktree after that is a copy of it. The template is keyed by a hash of your `migrate` and
`seed` commands, so changing either rebuilds it automatically.

Supported where the server can copy a slice server-side (`postgres`, `mysql`, `mariadb`, `percona`,
`mongodb`). Where it isn't, or where a clone fails on something environmental — a client binary too
old to authenticate, a missing grant — Warren **falls back** to provision+migrate+seed for that
worktree and logs `clone_fallback`. Correctness never depends on cloning; only speed does.

One sharp edge worth knowing: the hash covers the migrate and seed *commands*, not the files they
call. Editing a script those commands run does not rebuild the template — delete its marker under
`~/.warren/golden/` to force one.

### Writing your own

Two tiers, one contract. Both implement the same four verbs:

| verb | does |
| --- | --- |
| `provision(slice)` | create the slice (`CREATE DATABASE ...`) |
| `resolve(slice)` | return its address → `${db.url}` |
| `teardown(slice)` | destroy it |
| `capabilities()` | what it supports, e.g. `{"clone": true}` |

**In-process** adapters are Python classes in this repo, discovered by name. **External** adapters
are executables in any language, discovered as `warren-adapter-<name>` on `PATH` and driven over a
one-shot subprocess + JSON protocol:

```
$ warren-adapter-filestore provision   <<< '{"slice": "acme_login-fix", "config": {}}'
{"ok": true}
$ warren-adapter-filestore resolve     <<< '{"slice": "acme_login-fix", "config": {}}'
{"ok": true, "address": "file:///tmp/warren-filestore/acme_login-fix"}
```

Python authors get the protocol for free — `warren.adapter_sdk.run()` bridges a normal adapter class
to it. See [examples/adapters/filestore.py](examples/adapters/filestore.py) for a complete,
dependency-free one in 30 lines.

## Commands

| Command | What it does |
|---|---|
| `warren init [dir] [--force]` | Write a starting `warren.toml` from `docker-compose.yml` + `.env` |
| `warren check [path]` | Validate a `warren.toml` and flag footguns |
| `warren up <name> [--group g]` | Create/use the branch's worktree, allocate ports, provision resources, start services |
| `warren down <name>` | Stop the worktree's processes (keeps its files and ports) |
| `warren remove <name> [--force]` | Destroy the worktree — stop, drop its slices, remove its files |
| `warren status [.]` | List worktrees (grouped by project); `.` = current project only |
| `warren env <name> [--dotenv]` | Print a worktree's exposed coordinates, for you or an agent |
| `warren verify <name>` | Run the project's `agents.verify` checks; exits non-zero on failure |
| `warren logs <name>` | Print a worktree's captured service logs |
| `warren dashboard` | Live TUI of every worktree |
| `warren doctor` | Diagnose the install and daemon; exits non-zero if something is wrong |
| `warren metrics` | Point-in-time daemon metrics as JSON |
| `warren mcp` | Run the MCP server so agents can call Warren's verbs as tools |
| `warren daemon start\|stop\|status` | Manage the background daemon |

## How it works

A single machine-global daemon sits between your worktrees and the machine:

1. **Worktree** — creates/uses a git worktree per name.
2. **Ports** — a global allocator assigns every process a port *before anything starts* and injects
   the whole map, so nothing collides (`${self.port}`, `${api.url}`, …).
3. **Resources** — adapters carve an isolated slice per worktree and tear it down on remove.
4. **Supervision** — starts services in `depends_on` order, waits for health, captures logs.
5. **Proxy** — routes `<service>.<worktree>.<domain>` to the right port (WebSockets/HMR included).
   Set `https = true` and Warren fronts it with Caddy for TLS via a local CA, when Caddy is on PATH.
6. **Admission** — a FIFO queue bounded by `max_parallel` and free RAM, so the swarm doesn't melt
   the box.

State lives in `~/.warren/` (a SQLite registry, migrated in place across upgrades). Nothing runs in
the cloud, and Warren never phones home.

## Examples

- [examples/hello](examples/hello) — the smallest possible project.
- [examples/rag](examples/rag) — vector search where each worktree gets its own embedded corpus.
  The case where sharing a database actually hurts: two agents re-indexing, one `TRUNCATE`.
- [examples/adapters/filestore.py](examples/adapters/filestore.py) — an external adapter, complete.

## Security

Warren runs **your** commands on **your** machine as **you**. It is a developer tool for a
single-user workstation, not a multi-tenant sandbox — a `warren.toml` is as trusted as a `Makefile`,
and `warren up` on a repo you don't trust is `make` on a repo you don't trust. The threat model and
what's explicitly out of scope are written down in [SECURITY.md](SECURITY.md).

The one rule worth repeating here: **never put a credential inline in a committed `warren.toml`**.
Use `${env.NAME}` or an `env_file`. `warren check` warns when you do, and `warren init` never writes
one.

## Something not working?

[TROUBLESHOOTING.md](TROUBLESHOOTING.md) covers the cases that actually come up — a port held from
outside, a dev server ignoring `$PORT`, a stale golden template, `*.localhost` not resolving. Start
with `warren doctor`.

## Development

```bash
python -m venv .venv && . .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -e ".[dev,all-adapters]"
pytest
ruff check src tests examples benchmarks
```

Adapter integration tests skip unless you point them at a real server —
`WARREN_TEST_PG`, `WARREN_TEST_REDIS`, `WARREN_TEST_MYSQL`, `WARREN_TEST_MONGO`, `WARREN_TEST_S3`,
`WARREN_TEST_RABBITMQ`, `WARREN_TEST_KAFKA`, `WARREN_TEST_QDRANT`, `WARREN_TEST_CLICKHOUSE`,
`WARREN_TEST_ELASTICSEARCH`. CI provides all of them.

## License

MIT — see [LICENSE](LICENSE).
