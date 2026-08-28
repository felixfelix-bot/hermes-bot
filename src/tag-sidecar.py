#!/usr/bin/env python3
"""tag-sidecar.py — Tiny HTTP proxy that adds X-Hermes-Task-Type header to
requests before forwarding to zai_proxy.

This runs on hermes NVMe alongside zai_proxy. The reverse SSH tunnel from
testserver2 connects to this sidecar (port 9097) instead of zai_proxy (9099)
directly. The sidecar injects the task_type header so _log_api_call in
zai_proxy correctly tags buyer traffic as 'routstrd_sale' for attribution.

Usage:
  python tag-sidecar.py   # listens on 127.0.0.1:9097, forwards to 127.0.0.1:9099

Systemd: ~/.config/systemd/user/tag-sidecar.service
"""
from __future__ import annotations

import http.server
import http.client
import threading

LISTEN_PORT = 9097
FORWARD_HOST = "127.0.0.1"
FORWARD_PORT = 9099
TAG_HEADER_KEY = "X-Task-Type"
TAG_HEADER_VALUE = "routstrd_sale"


class TagProxyHandler(http.server.BaseHTTPRequestHandler):
    def _forward(self):
        body_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(body_len) if body_len > 0 else None

        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in ("host", "x-task-type")}
        headers[TAG_HEADER_KEY] = TAG_HEADER_VALUE
        headers["Host"] = f"{FORWARD_HOST}:{FORWARD_PORT}"

        try:
            conn = http.client.HTTPConnection(FORWARD_HOST, FORWARD_PORT, timeout=120)
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()

            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() not in ("transfer-encoding", "connection"):
                    self.send_header(k, v)
            if "content-length" not in {k.lower() for k, _ in resp.getheaders()}:
                self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
            conn.close()
        except Exception as e:
            self.send_error(502, f"Proxy error: {e}")

    def do_GET(self): self._forward()
    def do_POST(self): self._forward()
    def do_PUT(self): self._forward()
    def do_DELETE(self): self._forward()
    def do_OPTIONS(self): self._forward()
    def do_PATCH(self): self._forward()

    def log_message(self, format, *args):
        import sys
        print(f"[tag-sidecar] {self.client_address[0]} {self.command} {self.path}", file=sys.stderr)


def main():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", LISTEN_PORT), TagProxyHandler)
    print(f"[tag-sidecar] Listening on 127.0.0.1:{LISTEN_PORT} → {FORWARD_HOST}:{FORWARD_PORT} "
          f"(injecting {TAG_HEADER_KEY}: {TAG_HEADER_VALUE})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
