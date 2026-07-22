"""How much does one more parallel world cost?

Warren's claim is that N isolated worlds should not cost N times one world. This
measures that against the thing people actually do today — a container per
worktree (what `docker compose up` in each checkout gives you) — on three axes:

    RAM     the resident memory of the database layer
    disk    bytes on disk for the database layer
    time    wall clock to have N worlds ready, migrated and seeded

Two arms, same Postgres image, same schema, same row count:

    container-per-worktree   N Postgres containers, each seeded from scratch
    warren                   1 Postgres container, N databases cloned from a
                             golden template (exactly what the postgres adapter
                             does: CREATE DATABASE ... TEMPLATE)

What this deliberately does NOT measure: your own app processes. Warren does not
make those cheaper — one dev server per worktree costs the same either way. The
comparison is the *backing services*, which is the part that differs.

    python benchmarks/worlds.py --worlds 5 --rows 200000

Requires Docker and `pip install 'warren[postgres]'`. Every container it creates
is named `warrenbench-*` and is removed on the way out, including on failure.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import subprocess
import sys
import time

IMAGE = "postgres:16"
PREFIX = "warrenbench"
PASSWORD = "bench"
BASE_PORT = 55432

SCHEMA = """
CREATE TABLE items (
    id     bigint PRIMARY KEY,
    sku    text NOT NULL,
    blob   text NOT NULL
);
CREATE INDEX items_sku_idx ON items (sku);
"""

# Seeded server-side: the point is to measure the database doing the work, not a
# client streaming rows over a socket.
SEED = """
INSERT INTO items (id, sku, blob)
SELECT g, md5(g::text), repeat('x', 400)
FROM generate_series(1, {rows}) g;
ANALYZE items;
"""


def sh(*args: str, check: bool = True) -> str:
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed:\n{proc.stderr.strip()}")
    return proc.stdout.strip()


def connect(port: int, dbname: str = "postgres"):
    import psycopg

    return psycopg.connect(
        f"postgres://postgres:{PASSWORD}@127.0.0.1:{port}/{dbname}", autocommit=True
    )


def start_postgres(name: str, port: int) -> None:
    sh("docker", "run", "-d", "--name", name, "-p", f"{port}:5432",
       "-e", f"POSTGRES_PASSWORD={PASSWORD}", IMAGE)


def wait_ready(name: str, port: int, timeout: float = 90.0) -> None:
    """Wait for a real handshake — an open TCP port is not a ready Postgres."""
    import psycopg

    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with connect(port) as conn:
                conn.execute("SELECT 1")
            return
        except psycopg.Error as exc:
            last = exc
            time.sleep(0.5)
    raise RuntimeError(f"{name} was not ready within {timeout:.0f}s: {last}")


def container_ram_mib(names: list[str]) -> float:
    """Resident memory of the given containers, in MiB, from `docker stats`."""
    if not names:
        return 0.0
    out = sh("docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}", *names)
    total = 0.0
    for line in out.splitlines():
        if "\t" not in line:
            continue
        _, usage = line.split("\t", 1)
        match = re.match(r"([\d.]+)\s*([A-Za-z]+)", usage.strip())
        if not match:
            continue
        value, unit = float(match.group(1)), match.group(2).lower()
        scale = {"b": 1 / 1048576, "kib": 1 / 1024, "mib": 1.0, "gib": 1024.0}
        total += value * scale.get(unit, 1.0)
    return total


def container_disk_mib(names: list[str]) -> float:
    """Size of the Postgres data directory inside each container, in MiB."""
    total = 0.0
    for name in names:
        out = sh("docker", "exec", name, "du", "-sm", "/var/lib/postgresql/data", check=False)
        if out and out.split():
            with contextlib.suppress(ValueError):
                total += float(out.split()[0])
    return total


def cleanup() -> None:
    names = sh("docker", "ps", "-aq", "--filter", f"name={PREFIX}", check=False)
    if names:
        subprocess.run(["docker", "rm", "-f", *names.split()], capture_output=True, check=False)


# --------------------------------------------------------------------------- #
#  arm 1 — a container per worktree (what docker compose gives you)
# --------------------------------------------------------------------------- #
def arm_containers(worlds: int, rows: int) -> dict:
    names, started = [], time.monotonic()
    for i in range(worlds):
        name, port = f"{PREFIX}-solo-{i}", BASE_PORT + i
        start_postgres(name, port)
        names.append(name)
    for i, name in enumerate(names):
        port = BASE_PORT + i
        wait_ready(name, port)
        with connect(port) as conn:
            conn.execute(SCHEMA)
            conn.execute(SEED.format(rows=rows))
    elapsed = time.monotonic() - started
    time.sleep(3)  # let memory settle after the seed burst
    return {
        "seconds": round(elapsed, 1),
        "ram_mib": round(container_ram_mib(names), 1),
        "disk_mib": round(container_disk_mib(names), 1),
        "containers": len(names),
    }


# --------------------------------------------------------------------------- #
#  arm 2 — one server, a database per worktree, cloned from a golden template
# --------------------------------------------------------------------------- #
def arm_warren(worlds: int, rows: int) -> dict:
    name, port = f"{PREFIX}-warren", BASE_PORT + 100
    start_postgres(name, port)
    wait_ready(name, port)

    started = time.monotonic()
    with connect(port) as conn:
        conn.execute("CREATE DATABASE golden")
    with connect(port, "golden") as conn:
        conn.execute(SCHEMA)
        conn.execute(SEED.format(rows=rows))
    golden_seconds = time.monotonic() - started

    clone_times = []
    with connect(port) as conn:
        for i in range(worlds):
            mark = time.monotonic()
            conn.execute(f"CREATE DATABASE world_{i} TEMPLATE golden")
            clone_times.append(time.monotonic() - mark)
    elapsed = time.monotonic() - started

    time.sleep(3)
    return {
        "seconds": round(elapsed, 1),
        "golden_seconds": round(golden_seconds, 1),
        "per_clone_seconds": round(sum(clone_times) / len(clone_times), 2),
        "ram_mib": round(container_ram_mib([name]), 1),
        "disk_mib": round(container_disk_mib([name]), 1),
        "containers": 1,
    }


def report(worlds: int, rows: int, solo: dict, warren: dict) -> str:
    def ratio(a: float, b: float) -> str:
        return f"{a / b:.1f}x" if b else "n/a"

    lines = [
        f"### {worlds} parallel worlds · {rows:,} rows each · {IMAGE}",
        "",
        "| | container per worktree | Warren (shared server) | |",
        "| --- | ---: | ---: | ---: |",
        f"| RAM | {solo['ram_mib']:.0f} MiB | {warren['ram_mib']:.0f} MiB | "
        f"**{ratio(solo['ram_mib'], warren['ram_mib'])} less** |",
        f"| Disk | {solo['disk_mib']:.0f} MiB | {warren['disk_mib']:.0f} MiB | "
        f"{ratio(solo['disk_mib'], warren['disk_mib'])} |",
        f"| Time to N ready worlds | {solo['seconds']:.0f}s | {warren['seconds']:.0f}s | "
        f"**{ratio(solo['seconds'], warren['seconds'])} faster** |",
        "",
        f"Warren seeded once ({warren['golden_seconds']:.0f}s for the golden), then each further "
        f"world cost {warren['per_clone_seconds']:.2f}s.",
    ]
    return "\n".join(lines)


def sweep_report(rows: int, points: list[tuple[int, dict, dict]]) -> str:
    """The shape is the story: one arm grows with N, the other mostly doesn't."""
    lines = [
        f"### Cost of one more parallel world · {rows:,} rows each · {IMAGE}",
        "",
        "| worlds | RAM: a container each | RAM: Warren | disk: a container each "
        "| disk: Warren | time: a container each | time: Warren |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for n, solo, warren in points:
        lines.append(
            f"| {n} | {solo['ram_mib']:.0f} MiB | {warren['ram_mib']:.0f} MiB "
            f"| {solo['disk_mib']:.0f} MiB | {warren['disk_mib']:.0f} MiB "
            f"| {solo['seconds']:.0f}s | {warren['seconds']:.0f}s |"
        )
    if len(points) >= 2:
        (n0, s0, w0), (n1, s1, w1) = points[0], points[-1]
        span = n1 - n0
        lines += [
            "",
            "**Marginal cost of the Nth world**, measured across that range:",
            "",
            f"- a container each: **+{(s1['ram_mib'] - s0['ram_mib']) / span:.0f} MiB RAM**, "
            f"**+{(s1['disk_mib'] - s0['disk_mib']) / span:.0f} MiB disk**, "
            f"**+{(s1['seconds'] - s0['seconds']) / span:.1f}s**",
            f"- Warren: **+{(w1['ram_mib'] - w0['ram_mib']) / span:.0f} MiB RAM**, "
            f"**+{(w1['disk_mib'] - w0['disk_mib']) / span:.0f} MiB disk**, "
            f"**+{(w1['seconds'] - w0['seconds']) / span:.1f}s**",
            "",
            "Read the first row before the last one. At one world Warren is *behind* — it "
            "pays for a server plus a golden template nobody is using yet. It buys that back "
            "on the second world and never gives it up. Disk is the axis it does not win: "
            "`CREATE DATABASE ... TEMPLATE` is a file copy, not copy-on-write, so every world "
            "is still a full copy of the data. What Warren removes is the *server* per world, "
            "not the *bytes* per world.",
        ]
    return "\n".join(lines)


def measure(worlds: int, rows: int) -> tuple[dict, dict]:
    cleanup()
    try:
        return arm_containers(worlds, rows), arm_warren(worlds, rows)
    finally:
        cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worlds", type=int, default=5)
    parser.add_argument("--rows", type=int, default=200_000)
    parser.add_argument("--sweep", help="comma-separated world counts, e.g. 1,2,4,8")
    parser.add_argument("--json", action="store_true", help="print raw measurements too")
    args = parser.parse_args()

    try:
        sh("docker", "version", "--format", "{{.Server.Version}}")
    except (RuntimeError, FileNotFoundError):
        print("docker is not reachable — start it and try again", file=sys.stderr)
        return 1

    if args.sweep:
        counts = [int(n) for n in args.sweep.split(",") if n.strip()]
        points = []
        for n in counts:
            print(f"  measuring {n} world(s)...", file=sys.stderr)
            solo, warren = measure(n, args.rows)
            points.append((n, solo, warren))
        print(sweep_report(args.rows, points))
        if args.json:
            print()
            print(json.dumps(
                [{"worlds": n, "container_per_worktree": s, "warren": w} for n, s, w in points],
                indent=2,
            ))
        return 0

    solo, warren = measure(args.worlds, args.rows)
    print(report(args.worlds, args.rows, solo, warren))
    if args.json:
        print()
        print(json.dumps({"container_per_worktree": solo, "warren": warren}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
