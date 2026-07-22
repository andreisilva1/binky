"""A tiny HTTP server that binds the port Binky injected as $PORT."""

import http.server
import os

port = int(os.environ.get("PORT", "8000"))
handler = http.server.SimpleHTTPRequestHandler
with http.server.HTTPServer(("127.0.0.1", port), handler) as httpd:
    print(f"hello listening on :{port}", flush=True)
    httpd.serve_forever()
