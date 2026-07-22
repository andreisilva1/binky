# Security

## Threat model

Binky is a **local developer tool**: it runs on your machine, on repos you choose to run, for a **single
user**. Under that model it is as safe as the build tools you already run daily (`make`, `npm`, `docker
compose`) — and the notes below hold. Binky does **not** currently target hostile multi-user machines; that
isolation is tracked separately and is out of scope for a single-user setup.

If you find a vulnerability, please open a private report rather than a public issue.

## What to know

### `binky.toml` is executable config

Binky runs your services and hooks (`cmd`, `setup`, `migrate`, `seed`, `verify`, `hooks.*`) through a shell —
by design, so `cmd = "npm run dev"` and pipes/`&&` work, exactly like `docker compose`'s `command:`. This means
**a `binky.toml` executes arbitrary commands**, just like a `Makefile` or an npm `postinstall`.

- **Only run `binky up` in repos you trust.** Running an untrusted repo's `binky.toml` runs its commands.
- Binky restricts worktree/branch names to a shell- and path-safe charset (`A–Z a–z 0–9 . _ - /`, no `..`), so
  the `${worktree}` token can't smuggle shell metacharacters into a command.

### Database admin credentials

Adapters like Postgres need an `admin_url` (a connection allowed to `CREATE`/`DROP DATABASE`) to carve a slice
per worktree. Services only ever receive their **slice** address, never the admin one.

- **Don't write `admin_url` inline in a committed `binky.toml`.** Use `admin_url = "${env.BINKY_DB_ADMIN_URL}"`
  or an `env_file`, so the secret stays out of git. `binky check` warns when it sees an inline credential.

### The daemon control channel

The daemon listens on a Unix-domain socket (posix) or a loopback TCP port guarded by a shared-secret **token**
(Windows). Anything that can reach the channel can drive the daemon (which runs commands).

- The token lives in `daemon.json`, written **owner-only (`0600`)**. On a shared machine, keep your
  `BINKY_HOME` private — the token is the guard on the loopback transport.

## Reducing risk

- Keep secrets in the environment or an `env_file`, never inline in a committed config.
- Run `binky up` only on trusted repositories.
- Treat `BINKY_HOME` (`~/.binky` by default) as private to your user.
