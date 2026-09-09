#!/usr/bin/env python3
"""tag-sidecar.py — Tiny HTTP proxy that adds X-Priority: sold + X-Task-Type to
routstr-sourced requests before forwarding to zai_proxy, and appends one canary
decision-log line per sold request (Phase B / ADR-007 shadow canary).

This runs on hermes NVMe alongside zai_proxy. The reverse SSH tunnel from
testserver2 connects to this sidecar (port 9097) instead of zai_proxy (9099)
directly. The sidecar injects the task_type header so _log_api_call in
zai_proxy correctly tags buyer traffic as 'routstrd_sale' for attribution,
and injects X-Priority: sold to identify routstr-sourced requests as sold
traffic (Phase B canary). After forwarding, one canary decision-log line is
written per sold request to the sold-canary JSONL file and mirrored into the
routing decision tables (caller_class='sold').

Usage:
  python tag-sidecar.py   # listens on 127.0.0.1:9097, forwards to 127.0.0.1:9099

Systemd: ~/.config/systemd/user/tag-sidecar.service
"""
from __future__ import annotations

import http.server
import http.client
import json
import os
import threading
import time

# ── import the canary module from merchant-routing-engine repo ──────────────
_MRE_PATH = os.path.expanduser("~/merchant-routing-engine")
if _MRE_PATH not in os.environ.get("PYTHONPATH", ""):
    import sys
    if _MRE_PATH not in sys.path:
        sys.path.insert(0, _MRE_PATH)

from src.routstr_sold_canary import (
    CANARY_KEY,
    append_sold_canary,
    log_sold_decision,
    sold_headers,
)

LISTEN_PORT = 9097
FORWARD_HOST = "127.0.0.1"
FORWARD_PORT = 9099
TAG_HEADER_KEY = "X-Task-Type"
TAG_HEADER_VALUE = "routstrd_sale"

# Canary log path: one JSONL line per sold request
_CANARY_LOG = os.path.expanduser("~/.hermes/bot/routstr_sold_canary.jsonl")
# zai_usage.db path for decision-table mirror (makes canary visible to P&L + Gate-1)
_ZAI_DB = os.path.expanduser("~/.hermes/bot/zai_usage.db")


class TagProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    """Advertise HTTP/1.1 so our Transfer-Encoding: chunked re-framing is
    spec-compliant. BaseHTTPRequestHandler defaults to HTTP/1.0 where chunked
    transfer is deprecated and some clients reject it."""

    def _forward(self):
        body_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(body_len) if body_len > 0 else None

        # Build headers: inject X-Task-Type: routstrd_sale AND X-Priority: sold
        orig = {k: v for k, v in self.headers.items() if k.lower() not in (
            "host", "x-task-type", "x-priority", "authorization")}
        headers = sold_headers(orig)
        headers[TAG_HEADER_KEY] = TAG_HEADER_VALUE
        headers["Host"] = f"{FORWARD_HOST}:{FORWARD_PORT}"

        # Extract model for the canary line
        body_str = body.decode("utf-8", errors="replace") if body else ""
        model = None
        if body_str:
            try:
                payload = json.loads(body_str)
                model = payload.get("model")
            except Exception:
                pass

        try:
            conn = http.client.HTTPConnection(FORWARD_HOST, FORWARD_PORT, timeout=120)
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()
            status = resp.status
            resp_headers = resp.getheaders()
            resp_header_map = {k.lower(): v for k, v in resp_headers}

            self.send_response(status)
            for k, v in resp_headers:
                if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                    self.send_header(k, v)

            # Streaming (no Content-Length) responses must be re-framed correctly
            # for the downstream client. http.client auto-de-chunks HTTP chunked
            # responses, so resp.read() yields the raw body. BaseHTTPRequestHandler
            # will add its own Transfer-Encoding: chunked for HTTP/1.1 clients, but
            # we must write actual chunk framing ourselves — otherwise a
            # chunked-aware client (httpx/openai) parses the raw SSE bytes as a hex
            # chunk-size and dies with RemoteProtocolError after the first frame.
            if "content-length" not in resp_header_map:
                self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            if "content-length" in resp_header_map:
                # Known length: pass through raw bytes unchanged.
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            else:
                # Unknown length (streaming): re-frame as HTTP chunked.
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(b"%x\r\n" % len(chunk))
                    self.wfile.write(chunk)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            conn.close()

        except Exception as e:
            status = 502
            self.send_error(502, f"Proxy error: {e}")

        # ── Canary decision-log line (never raises, never alters routing) ──
        append_sold_canary(_CANARY_LOG, model=model, ts=time.time(), status=status)
        log_sold_decision(_ZAI_DB, model=model)

    def do_GET(self):
        self._forward()

    def do_POST(self):
        self._forward()

    def do_PUT(self):
        self._forward()

    def do_DELETE(self):
        self._forward()

    def do_OPTIONS(self):
        self._forward()

    def do_PATCH(self):
        self._forward()

    def log_message(self, format, *args):
        import sys
        print(f"[tag-sidecar] {self.client_address[0]} {self.command} {self.path}",
              file=sys.stderr)


def main():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", LISTEN_PORT), TagProxyHandler)
    print(f"[tag-sidecar] Listening on 127.0.0.1:{LISTEN_PORT} → {FORWARD_HOST}:{FORWARD_PORT} "
          f"(injecting {TAG_HEADER_KEY}: {TAG_HEADER_VALUE} + X-Priority: sold canary "
          f"→ {_CANARY_LOG})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()