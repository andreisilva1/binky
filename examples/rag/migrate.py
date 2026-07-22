"""Create the schema. Warren runs this against $DATABASE_URL.

It runs once into the golden template, not once per worktree — see README.md.
"""

from __future__ import annotations

import os

import psycopg
from embed import DIM

SQL = f"""
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS chunks (
    id       bigserial PRIMARY KEY,
    source   text NOT NULL,
    body     text NOT NULL,
    vec      vector({DIM}) NOT NULL
);
"""

with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
    conn.execute(SQL)

print("schema ready")
