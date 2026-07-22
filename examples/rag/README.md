# Vector search, one corpus per worktree

Two agents are working on the same RAG app. One is rewriting the chunking
strategy; the other is tuning the ranking. Both need to re-index. On a shared
database, the first `TRUNCATE chunks` ends the second agent's afternoon.

This example gives each worktree its own Postgres database, with the corpus
already embedded, in about a second.

## Run it

Postgres needs the `vector` extension — the stock image doesn't have it:

```bash
docker run -d --name pgvector -p 5433:5432 \
  -e POSTGRES_PASSWORD=postgres pgvector/pgvector:pg16

export BINKY_PG_ADMIN=postgres://postgres:postgres@localhost:5433/postgres
pip install 'binky[postgres]'
```

Then, from this directory:

```bash
binky up feature-a       # embeds the corpus once, into a golden template
binky up feature-b       # copies it
binky status
```

Ask each worktree the same question:

```bash
curl "http://api.feature-a.rag.localhost/?q=git+worktree+isolated+database"
curl "http://api.feature-b.rag.localhost/?q=git+worktree+isolated+database"
```

Same answer, two databases. Now break one on purpose:

```bash
eval "$(binky env feature-a --dotenv)"                          # DB_URL, API_URL
psql "$DB_URL" -c "TRUNCATE chunks"
curl "http://api.feature-a.rag.localhost/?q=git+worktree+isolated+database"   # empty
curl "http://api.feature-b.rag.localhost/?q=git+worktree+isolated+database"   # untouched
```

That is the entire pitch. `feature-b` never noticed.

## What makes the second worktree cheap

`clone_from = "corpus"` in [binky.toml](binky.toml). The first `binky up`
provisions a database nobody owns, runs `migrate` and `seed` into it, and marks
it with a hash of both commands. Every `binky up` after that issues
`CREATE DATABASE ... TEMPLATE corpus` and skips the hooks entirely.

Edit `seed.py` and the hash changes, so the next `binky up` rebuilds the golden
once and clones from the new one. You never invalidate it by hand.

The saving is proportional to how expensive your seed is. Here, with a fake
encoder, it's seconds. With a real one it's the difference between a coffee and
a keystroke — which is why the encoder is the thing to swap first if you want
this example to be representative of your project:

```python
# embed.py
from sentence_transformers import SentenceTransformer
_model = SentenceTransformer("all-MiniLM-L6-v2")

def embed(text: str) -> list[float]:
    return _model.encode(text).tolist()
```

`DIM` is already 384, which is `all-MiniLM-L6-v2`'s output size, so that swap
needs no schema change. Editing `embed.py` alone does *not* rebuild the golden
template — the hash watches `migrate` and `seed`, not the files they import — so
delete the marker under `~/.binky/golden/` to force one.

## Honest limits

- **Cloning copies bytes.** `CREATE DATABASE ... TEMPLATE` is a file copy, not
  copy-on-write. Ten worktrees hold ten copies of the corpus. It buys you *time*,
  not disk. If disk is what hurts, the answer is a Postgres that branches
  copy-on-write, like Neon — and Binky does not ship an adapter for one. Pointed
  at such a server, the `postgres` adapter still does only what it claims: it
  creates databases, it does not branch them.
- **The extension must exist on the server.** Binky provisions databases; it
  does not install Postgres extensions it wasn't given. `CREATE EXTENSION` in
  `migrate.py` fails on a stock `postgres:16` image with a clear error.
- **There is no ANN index here, on purpose.** 2,000 rows is an exact scan in
  well under a millisecond. Both pgvector index types were measured *missing*
  the planted document on this corpus at their default settings — `ivfflat`
  because `probes = 1` scans one of 32 clusters, `hnsw` because these hashed
  vectors are nearly uniform on the sphere, which is the worst case for graph
  navigation:

  | search | top hit |
  | --- | --- |
  | `ivfflat`, `probes = 1` (default) | `notes/15.md`, score 0.187 |
  | `hnsw`, `ef_search = 40` (default) | `notes/15.md`, score 0.187 |
  | exact | **`handbook.md`, score 0.385** |

  Add `USING hnsw (vec vector_cosine_ops)` when your corpus is large enough to
  need it, and measure recall against an exact scan before you trust it. An
  approximate index on a toy corpus is not a smaller version of a real one; it
  is a different, wrong answer.
