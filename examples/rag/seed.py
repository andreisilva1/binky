"""Embed a corpus and build the index — the expensive step this example is about.

With a real encoder this is minutes, not seconds. Binky pays it once, into a
golden template, and every worktree after that is a copy.

Set CHUNKS to make the cost obvious; the default keeps `binky up` quick.
"""

from __future__ import annotations

import os
import random

import psycopg
from embed import embed, literal

CHUNKS = int(os.environ.get("CHUNKS", "2000"))

SUBJECTS = [
    "the retrieval pipeline", "the embedding cache", "a background worker",
    "the scheduler", "the rate limiter", "an upload handler", "the billing job",
    "the notification fanout", "a cron sweep", "the audit trail",
]
VERBS = ["rebuilds", "invalidates", "throttles", "reconciles", "batches", "retries"]
OBJECTS = [
    "every stale entry", "the queue it drains", "yesterday's partitions",
    "the webhook backlog", "an expired session", "the dead letter topic",
]

# The one document the README's example query is expected to find. Nothing else
# in the corpus talks about worktrees, so a working search must surface it.
PLANTED = (
    "handbook.md",
    "Binky gives every git worktree its own isolated database, "
    "so parallel agents never share a schema.",
)


def sentences(n: int) -> list[tuple[str, str]]:
    rng = random.Random(1234)  # deterministic: the same corpus in every worktree
    out = [PLANTED]
    for i in range(n - 1):
        # The trailing token keeps every chunk distinct — a corpus of duplicates
        # makes the demo's top-5 look broken even when the search is fine.
        body = (
            f"{rng.choice(SUBJECTS)} {rng.choice(VERBS)} "
            f"{rng.choice(OBJECTS)} in region {i:04d}."
        )
        out.append((f"notes/{i // 100:02d}.md", body.capitalize()))
    return out


with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
    with conn.cursor() as cur:
        cur.execute("TRUNCATE chunks")
        with cur.copy("COPY chunks (source, body, vec) FROM STDIN") as copy:
            for source, body in sentences(CHUNKS):
                copy.write_row((source, body, literal(embed(body))))
    # Deliberately no ANN index — see README.md. At this size an exact scan is
    # sub-millisecond, and both ivfflat and hnsw were measured *missing* the
    # planted document at their default settings on these synthetic vectors.
    conn.execute("ANALYZE chunks")

print(f"seeded {CHUNKS} chunks")
