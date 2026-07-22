"""A search endpoint over this worktree's own vector database.

Binds $PORT (Warren allocated it) and reads $DATABASE_URL (Warren provisioned
the slice behind it). Neither is hardcoded, which is the whole point: the same
file runs unchanged in every worktree, against a different database each time.

    GET /?q=isolated+database
"""

from __future__ import annotations

import http.server
import json
import os
import urllib.parse

import psycopg
from embed import embed, literal

PORT = int(os.environ.get("PORT", "8000"))
DATABASE_URL = os.environ["DATABASE_URL"]
WORKTREE = os.environ.get("WARREN_WORKTREE", "?")

SEARCH = """
SELECT source, body, 1 - (vec <=> %s::vector) AS score
FROM chunks
ORDER BY vec <=> %s::vector
LIMIT 5
"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("q", [""])[0]
        vector = literal(embed(query))
        with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
            cur.execute(SEARCH, (vector, vector))
            hits = [
                {"source": s, "body": b, "score": round(float(score), 4)}
                for s, b, score in cur.fetchall()
            ]
        body = json.dumps({"worktree": WORKTREE, "query": query, "hits": hits}, indent=2)
        payload = body.encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args) -> None:
        pass  # Warren already logs; a second copy on stderr is noise


with http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler) as httpd:
    print(f"rag api for worktree {WORKTREE} on :{PORT}", flush=True)
    httpd.serve_forever()
