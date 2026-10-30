import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health", "/ping"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK - Krishna Zx Store Bot is alive")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass  # silent logs


def start_keep_alive():
    port = int(os.getenv("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"✅ Keep-alive HTTP server listening on port {port}", flush=True)
