"""A trivial HTTP server the end-to-end tests use as their `web` service.

Not `python -m http.server`: its `server_bind()` calls `socket.getfqdn()` on the
bind host *before* it starts listening, and on a macOS CI runner that reverse-DNS
lookup stalls long enough that the port never opens inside the health timeout —
so every `up` test failed there with `web` never becoming healthy. This binds
loopback and neutralises the lookup, so the socket accepts connections at once.
"""

WEB_SERVER_PY = """\
import socket, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

socket.getfqdn = lambda *a: "localhost"  # skip the reverse-DNS stall before listen()


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
"""
