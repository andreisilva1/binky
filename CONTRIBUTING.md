# Contributing to Binky

Thanks for wanting to help. Binky is a small, focused project — bug reports, new
adapters, and pull requests are all welcome. This file covers the few things about
Binky's setup that aren't obvious.

## Before a large change

For anything beyond a small fix, **open an issue first**. Binky is maintained by one
person; a quick conversation about the approach saves you from writing a large PR that
has to be reworked. Small, obvious fixes can go straight to a PR.

## Development setup

Binky targets **Python 3.11+** and runs on macOS, Linux, and Windows.

```bash
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev,all-adapters]"
```

## Tests

```bash
pytest
```

Binky has **two tiers of tests**, and this trips people up the first time:

- **Unit tests** run entirely locally with no external services. This is the default
  `pytest` run and what you'll use most.
- **Adapter integration tests** talk to *real* servers (Postgres, Redis, MySQL, Mongo,
  S3/MinIO, Kafka, …). They **skip automatically** unless you point them at a server via
  `BINKY_TEST_*` environment variables — so a local run showing a batch of `skipped`
  is **expected**, not a failure. CI provides all of these servers, so integration
  coverage runs there on every PR.

  To exercise one locally, start the server and set its variable, e.g.:

  ```bash
  docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=postgres postgres:16
  BINKY_TEST_PG=postgres://postgres:postgres@localhost:5432/postgres pytest tests/test_adapters.py
  ```

  The full list of variables is in the README's *Development* section.

## Linting

CI runs, and PRs must pass, exactly:

```bash
ruff check src tests examples benchmarks
```

The enabled rule set lives in `pyproject.toml` (`[tool.ruff.lint]`). Please run it
before pushing.

## Commit conventions

- Commit messages follow **Conventional Commits** (`type(scope): summary`), matching the
  existing history — e.g. `fix(daemon): …`, `docs: …`, `test: …`.
- **Sign off every commit** with the Developer Certificate of Origin:

  ```bash
  git commit -s
  ```

  This adds a `Signed-off-by:` line certifying you have the right to submit the code
  under the project's license (see [DCO](https://developercertificate.org/)). Binky uses
  the DCO rather than a CLA on purpose: it keeps contribution lightweight while keeping
  the project's licensing honest and permissive. Contributors retain copyright over their
  own contributions.

## Pull requests

Keep PRs focused. A good PR:

- passes `pytest` and `ruff check`,
- updates docs when behavior changes (the README's adapter matrix is **CI-enforced** by
  `tests/test_docs.py`, so add new adapters to the table),
- adds a `Signed-off-by:` line to each commit,
- and, for anything non-trivial, links the issue where the approach was discussed.

## Architecture

Working on the daemon, the port allocator, admission, or an adapter? Start with
[ARCHITECTURE.md](ARCHITECTURE.md) for a map of how the pieces fit together.
