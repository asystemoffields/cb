"""Tiny fixture server for the cb test suite: static files from tests/site plus a few API routes."""
import json
import sys
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SITE = Path(__file__).parent / "site"


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/api/items"):
            time.sleep(0.3)
            return self._json({"items": ["alpha", "beta", "gamma"]})
        if self.path.startswith("/api/fail"):
            return self._json({"error": "nope"}, 500)
        return super().do_GET()

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(port=0):
    srv = ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, directory=str(SITE)))
    return srv


if __name__ == "__main__":
    srv = serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8765)
    print(f"http://127.0.0.1:{srv.server_address[1]}", flush=True)
    srv.serve_forever()
