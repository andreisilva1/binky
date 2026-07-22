# Troubleshooting

Start here:

```bash
warren doctor
```

It checks the daemon, the registry, the port range, the proxy and disk use, and exits non-zero when
something is actually wrong. Most of what follows is a specific case of one of its lines.

---

## `warren up` fails

### `adapter: ...` — the adapter couldn't reach its server

Warren doesn't run your Postgres, it *carves a slice* of one you already run. `admin_url` has to
point at a server that's up, with an account that can create and drop databases.

```bash
psql "$POSTGRES_ADMIN_URL" -c "SELECT 1"     # does the URL work at all?
```

If it works there and not in Warren, the usual cause is `${env.NAME}` resolving to nothing: Warren
reads it from the environment of the **daemon**, not your shell. Export it before the daemon starts,
or restart it:

```bash
warren daemon stop && warren daemon start
```

### A driver isn't installed

`ModuleNotFoundError: psycopg` means the extra isn't installed. Adapters ship as extras on purpose:

```bash
uv tool install ".[postgres]"      # or [redis], [mysql], [mongodb], [s3], [kafka]
```

`rabbitmq`, `qdrant`, `clickhouse` and `elasticsearch` need nothing — they drive plain HTTP.

### The worktree has uncommitted changes

`warren remove` refuses rather than deleting work. Commit, stash, or pass `--force` if you're sure.

---

## Ports

### Something outside Warren holds the port

Warren allocates from its own range and never guesses, but it can't stop another program from
sitting on a port first. Move Warren's range:

```toml
[project]
port_range = [42000, 43000]
```

### A service ignores the port Warren gave it

Warren injects `$PORT` and resolves `${self.port}` — it can't make a framework listen there. If your
dev server has its own default, pass it explicitly:

```toml
cmd = "pnpm dev --port ${self.port}"     # not just "pnpm dev"
```

This is the single most common cause of "two worktrees collide even with Warren".

---

## URLs and the proxy

### `web.login-fix.acme.localhost` doesn't resolve

`*.localhost` resolves to loopback on macOS and most Linux setups, and on Windows 10+. If yours
doesn't, add the specific name to your hosts file, or skip the proxy and hit the port directly —
`warren status` lists every service's port, and `warren env <worktree>` prints the addresses.

### HTTPS isn't working

`https = true` fronts the proxy with **Caddy**, which must be on `PATH`. If it isn't, Warren skips
TLS and http keeps working — deliberately, so a missing binary degrades instead of breaking `up`.

```bash
caddy version        # if this fails, that's why
```

The first request to a `tls internal` name asks to trust a local CA; until you accept it, browsers
show a warning.

---

## `clone_from`

### It cloned, but my schema is stale

The golden template is keyed by a hash of your `migrate` and `seed` **commands**, not the files they
run. Editing `migrations/007_x.sql` does not change `"make migrate"`, so nothing rebuilds. Force it:

```bash
# ~/.warren/golden/<adapter>/<project>_golden_<clone_from>
rm -f ~/.warren/golden/postgres/acme_golden_golden
```

(The marker is a file holding the hash; deleting it makes the next `warren up` rebuild the
template. `ls ~/.warren/golden/*/` if you're not sure of the name.)

This is the sharpest edge in the feature. If it bites you often, put something command-visible in
the hook (a version, a checksum) so the hash moves when your schema does.

### It silently stopped cloning

It didn't do it silently — look for `clone_fallback` in the event log:

```bash
grep clone_fallback ~/.warren/events.log
```

A clone falls back to provision+migrate+seed when it fails for an environmental reason: a client
binary too old to authenticate against the server, a missing grant, a server setting. The world you
get is identical, just built the slow way. The most common case by far is a `mysqldump` older than
the MySQL server it's dumping (`caching_sha2_password could not be loaded`) — upgrade the client, or
accept the slow path.

### My adapter doesn't support it

`clickhouse` and the search adapters report `clone: false` on purpose: there's no server-side copy,
so "cloning" would mean `INSERT ... SELECT` per table — O(rows), which is exactly the cost
`clone_from` exists to avoid. Warren provisions and seeds per worktree instead.

---

## The daemon

### It won't start, or `warren status` hangs

```bash
warren daemon status
warren doctor
```

The daemon is machine-global and starts on demand. If it's wedged, stop and start it — worktrees
keep running; the daemon reconciles them on the way back up.

```bash
warren daemon stop && warren daemon start
```

### The registry looks corrupt

State is a SQLite database at `~/.warren/warren.db`, migrated in place across upgrades. It's yours —
back it up, inspect it, or in the worst case move it aside and let Warren rebuild:

```bash
mv ~/.warren/warren.db ~/.warren/warren.db.bak
```

You lose Warren's bookkeeping, not your code: git worktrees stay on disk, and adapter slices stay on
their servers. You'll want to drop the orphaned slices by hand.

---

## `warren init`

### Most of my services came out commented

Expected, and it's the honest answer. Compose says how to run something **in a container**; Warren
runs it **on the host**, and those are rarely the same command. Rather than guessing a `cmd` that
looks right and doesn't work, `init` writes the block commented with a TODO. Fill in the dev-server
command you'd actually type.

### It didn't pick up my database URL

`init` matches known variable names (`DATABASE_URL`, `REDIS_URL`, `MONGO_URL`, …) from `.env` — by
**name only**, it never reads your values. If yours is named something else, point `admin_url` at it
yourself: `admin_url = "${env.MY_PG_URL}"`.

---

## Still stuck

`warren doctor` output plus the relevant part of `~/.warren/events.log` is what makes an issue
actionable. Warren logs event names, worktrees, services and adapter names — not your config values.

One thing to skim before pasting: failure events carry the underlying error text, and an error
raised by a database driver can quote the connection it was attempting. Give it a look.
