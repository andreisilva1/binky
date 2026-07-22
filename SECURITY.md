# Security

## Threat model

Warren is a **local developer tool**: it runs on your machine, on repos you choose to run, for a **single
user**. Under that model it is as safe as the build tools you already run daily (`make`, `npm`, `docker
compose`) — and the notes below hold. Warren does **not** currently target hostile multi-user machines; that
isolation is tracked separately and is out of scope for a single-user setup.

If you find a vulnerability, please open a private report rather than a public issue.

## What to know

### `warren.toml` is executable config

Warren runs your services and hooks (`cmd`, `setup`, `migrate`, `seed`, `verify`, `hooks.*`) through a shell —
by design, so `cmd = "npm run dev"` and pipes/`&&` work, exactly like `docker compose`'s `command:`. This means
**a `warren.toml` executes arbitrary commands**, just like a `Makefile` or an npm `postinstall`.

- **Only run `warren up` in repos you trust.** Running an untrusted repo's `warren.toml` runs its commands.
- Warren restricts worktree/branch names to a shell- and path-safe charset (`A–Z a–z 0–9 . _ - /`, no `..`), so
  the `${worktree}` token can't smuggle shell metacharacters into a command.

### Database admin credentials

Adapters like Postgres need an `admin_url` (a connection allowed to `CREATE`/`DROP DATABASE`) to carve a slice
per worktree. Services only ever receive their **slice** address, never the admin one.

- **Don't write `admin_url` inline in a committed `warren.toml`.** Use `admin_url = "${env.WARREN_DB_ADMIN_URL}"`
  or an `env_file`, so the secret stays out of git. `warren check` warns when it sees an inline credential.

### The daemon control channel

The daemon listens on a Unix-domain socket (posix) or a loopback TCP port guarded by a shared-secret **token**
(Windows). Anything that can reach the channel can drive the daemon (which runs commands).

- The token lives in `daemon.json`, written **owner-only (`0600`)**. On a shared machine, keep your
  `WARREN_HOME` private — the token is the guard on the loopback transport.

## Reducing risk

- Keep secrets in the environment or an `env_file`, never inline in a committed config.
- Run `warren up` only on trusted repositories.
- Treat `WARREN_HOME` (`~/.warren` by default) as private to your user.
