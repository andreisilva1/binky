# Troubleshooting

Start here:

```bash
binky doctor
```

It checks the daemon, the registry, the port range, the proxy and disk use, and exits non-zero when
something is actually wrong. Most of what follows is a specific case of one of its lines.

---

## `binky up` fails

### `adapter: ...` — the adapter couldn't reach its server

Binky doesn't run your Postgres, it *carves a slice* of one you already run. `admin_url` has to
point at a server that's up, with an account that can create and drop databases.

```bash
psql "$POSTGRES_ADMIN_URL" -c "SELECT 1"     # does the URL work at all?
```

If it works there and not in Binky, the usual cause is `${env.NAME}` resolving to nothing: Binky
reads it from the environment of the **daemon**, not your shell. Export it before the daemon starts,
or restart it:

```bash
binky daemon stop && binky daemon start
```

### A driver isn't installed

`ModuleNotFoundError: psycopg` means the extra isn't installed. Adapters ship as extras on purpose:

```bash
uv tool install ".[postgres]"      # or [redis], [mysql], [mongodb], [s3], [kafka]
```

`rabbitmq`, `qdrant`, `clickhouse` and `elasticsearch` need nothing — they drive plain HTTP.

### The worktree has uncommitted changes

`binky remove` refuses rather than deleting work. Commit, stash, or pass `--force` if you're sure.

---

## Ports

### Something outside Binky holds the port

Binky allocates from its own range and never guesses, but it can't stop another program from
sitting on a port first. Move Binky's range:

```toml
[project]
port_range = [42000, 43000]
```

### A service ignores the port Binky gave it

Binky injects `$PORT` and resolves `${self.port}` — it can't make a framework listen there. If your
dev server has its own default, pass it explicitly:

```toml
cmd = "pnpm dev --port ${self.port}"     # not just "pnpm dev"
```

This is the single most common cause of "two worktrees collide even with Binky".

### `up` says a port "was Binky's, but another process took it"

While a worktree is down, Binky keeps its ports reserved so a later `up` is stable — but nothing
stops an outside process from grabbing a now-idle port in the meantime. Rather than start a service
that would just crash-loop, `up` reports it. Free the process sitting on that port and `binky up`
again, or `binky remove <name>` to release the reservation and get a fresh port on the next `up`.

---

## Capacity

### `up` is queued, or a running worktree went `suspended`

Binky admits a worktree only when the machine has room: under `max_parallel`, above the RAM reserve,
and above the disk headroom. If a swarm later crosses the RAM reserve, the daemon suspends the newest
worktree to relieve pressure and resumes it — in fairness order — once room returns; a `suspended`
worktree is waiting for that room, not broken. See the current limits with `binky config`, and raise
how much you leave the machine (or lower it to pack more in) with `binky reserve --ram 15 --disk 3`.
If `up` reports the request can *never* be admitted, the reserve is larger than the machine can ever
satisfy — lower it.

---

## URLs and the proxy

### `web.login-fix.acme.localhost` doesn't resolve

`*.localhost` resolves to loopback on macOS and most Linux setups, and on Windows 10+. If yours
doesn't, add the specific name to your hosts file, or skip the proxy and hit the port directly —
`binky status` lists every service's port, and `binky env <worktree>` prints the addresses.

### HTTPS isn't working

`https = true` fronts the proxy with **Caddy**, which must be on `PATH`. If it isn't, Binky skips
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
# ~/.binky/golden/<adapter>/<project>_golden_<clone_from>
rm -f ~/.binky/golden/postgres/acme_golden_golden
```

(The marker is a file holding the hash; deleting it makes the next `binky up` rebuild the
template. `ls ~/.binky/golden/*/` if you're not sure of the name.)

This is the sharpest edge in the feature. If it bites you often, put something command-visible in
the hook (a version, a checksum) so the hash moves when your schema does.

### It silently stopped cloning

It didn't do it silently — look for `clone_fallback` in the event log:

```bash
grep clone_fallback ~/.binky/events.log
```

A clone falls back to provision+migrate+seed when it fails for an environmental reason: a client
binary too old to authenticate against the server, a missing grant, a server setting. The world you
get is identical, just built the slow way. The most common case by far is a `mysqldump` older than
the MySQL server it's dumping (`caching_sha2_password could not be loaded`) — upgrade the client, or
accept the slow path.

### My adapter doesn't support it

`clickhouse` and the search adapters report `clone: false` on purpose: there's no server-side copy,
so "cloning" would mean `INSERT ... SELECT` per table — O(rows), which is exactly the cost
`clone_from` exists to avoid. Binky provisions and seeds per worktree instead.

---

## The daemon

### It won't start, or `binky status` hangs

```bash
binky daemon status
binky doctor
```

The daemon is machine-global and starts on demand. If it's wedged, stop and start it — worktrees
keep running; the daemon reconciles them on the way back up.

```bash
binky daemon stop && binky daemon start
```

If it dies instead of wedging, the error carries the reason with it — the daemon is detached, so
its stderr goes to `~/.binky/daemon.err`, rewritten on every start attempt. Read it directly when
you want the whole traceback rather than the last fifteen lines:

```bash
cat ~/.binky/daemon.err
```

### The registry looks corrupt

State is a SQLite database at `~/.binky/binky.db`, migrated in place across upgrades. It's yours —
back it up, inspect it, or in the worst case move it aside and let Binky rebuild:

```bash
mv ~/.binky/binky.db ~/.binky/binky.db.bak
```

You lose Binky's bookkeeping, not your code: git worktrees stay on disk, and adapter slices stay on
their servers. You'll want to drop the orphaned slices by hand.

---

## `binky init`

### Most of my services came out commented

Expected, and it's the honest answer. Compose says how to run something **in a container**; Binky
runs it **on the host**, and those are rarely the same command. Rather than guessing a `cmd` that
looks right and doesn't work, `init` writes the block commented with a TODO. Fill in the dev-server
command you'd actually type.

### It didn't pick up my database URL

`init` matches known variable names (`DATABASE_URL`, `REDIS_URL`, `MONGO_URL`, …) from `.env` — by
**name only**, it never reads your values. If yours is named something else, point `admin_url` at it
yourself: `admin_url = "${env.MY_PG_URL}"`.

---

## Still stuck

`binky doctor` output plus the relevant part of `~/.binky/events.log` is what makes an issue
actionable. Binky logs event names, worktrees, services and adapter names — not your config values.

One thing to skim before pasting: failure events carry the underlying error text, and an error
raised by a database driver can quote the connection it was attempting. Give it a look.
