#!/usr/bin/env python3
"""relatr — ContextVM that maintains a Nostr web-of-trust graph for relay filtering.

Fetches kind-3 (contact) lists from root npubs, computes multi-hop trust scores,
serves an HTTP API that strfry's write-policy plugin queries for each event.

Architecture:
  background thread → fetches kind-3 from relays every 15 min
  trust graph → root(1.0) → 1-hop(0.5) → 2-hop(0.25) → 3-hop(0.125)
  HTTP API (:7778):
    GET  /allowed        → JSON list of npubs above threshold
    GET  /score/<pubkey> → {"score": 0.5, "hop": 1}
    GET  /stats          → graph statistics
    POST /check          → {"pubkey":"hex"} → {"allowed":true, "score":0.5}
    GET  /health         → liveness check

Config via env vars or ~/.relatr/config.json:
  ROOT_NPUBS     — comma-separated hex pubkeys (trust anchors)
  NOSTR_RELAYS   — comma-separated ws(s):// relay URLs
  HOPS           — trust depth (default: 2)
  MIN_SCORE      — threshold for inclusion (default: 0.1)
  REFRESH_SEC    — background refresh interval (default: 900 = 15 min)
  PORT           — HTTP listen port (default: 7778)
"""
from __future__ import annotations
import json, os, sys, time, threading, urllib.request, ssl, struct
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

CONFIG_PATH = Path.home() / ".relatr" / "config.json"
DB_PATH = Path.home() / ".relatr" / "trust_graph.json"

DEFAULTS = {
    "root_npubs": [],
    "nostr_relays": ["wss://relay.damus.io", "wss://nos.lol", "wss://relay.ngit.dev"],
    "hops": 2,
    "min_score": 0.1,
    "refresh_sec": 900,
    "port": 7778,
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    env_roots = os.environ.get("ROOT_NPUBS", "")
    if env_roots:
        cfg["root_npubs"] = [h.strip() for h in env_roots.split(",") if h.strip()]
    env_relays = os.environ.get("NOSTR_RELAYS", "")
    if env_relays:
        cfg["nostr_relays"] = [r.strip() for r in env_relays.split(",") if r.strip()]
    cfg["hops"] = int(os.environ.get("HOPS", cfg["hops"]))
    cfg["min_score"] = float(os.environ.get("MIN_SCORE", cfg["min_score"]))
    cfg["refresh_sec"] = int(os.environ.get("REFRESH_SEC", cfg["refresh_sec"]))
    cfg["port"] = int(os.environ.get("PORT", cfg["port"]))
    if CONFIG_PATH.exists():
        try:
            file_cfg = json.loads(CONFIG_PATH.read_text())
            cfg.update(file_cfg)
        except Exception:
            pass
    return cfg


CONFIG = load_config()
trust_graph: dict[str, dict] = {}
graph_lock = threading.Lock()
last_refresh = 0


_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"

def _bech32_polymod(values):
    GEN = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = (chk & 0x1ffffff) << 5 ^ v
        for i in range(5):
            chk ^= GEN[i] if ((b >> i) & 1) else 0
    return chk

def _bech32_hrp_expand(hrp):
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]

def _convertbits(data, frombits, tobits, pad=True):
    acc = 0
    bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    for v in data:
        acc = (acc << frombits) | v
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad and bits:
        ret.append((acc << (tobits - bits)) & maxv)
    return ret

def bech32_decode(bech32: str) -> str | None:
    """Decode npub1... or hex → hex pubkey."""
    if len(bech32) == 64 and all(c in '0123456789abcdef' for c in bech32):
        return bech32
    if not bech32.startswith("npub1"):
        return None
    pos = bech32.rfind("1")
    if pos < 1 or pos + 7 > len(bech32):
        return None
    hrp = bech32[:pos]
    data = [_BECH32_CHARSET.find(c) for c in bech32[pos + 1:]]
    if any(d == -1 for d in data):
        return None
    if _bech32_polymod(_bech32_hrp_expand(hrp) + data) != 1:
        return None
    decoded = _convertbits(data[:-6], 5, 8, False)
    if decoded is None or len(decoded) != 32:
        return None
    return bytes(decoded).hex()


def fetch_kind3(pubkey_hex: str, relay_url: str) -> set[str]:
    """Fetch a pubkey's kind-3 contact list from a relay.

    Tries nak (WebSocket-capable) first, falls back to REST.
    """
    try:
        import subprocess
        result = subprocess.run(
            ["nak", "req", "-k", "3", "-a", pubkey_hex, relay_url],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            follows = set()
            for line in result.stdout.strip().splitlines():
                try:
                    ev = json.loads(line)
                    for tag in ev.get("tags", []):
                        if len(tag) >= 2 and tag[0] == "p":
                            follows.add(tag[1])
                except Exception:
                    continue
            if follows:
                return follows
    except Exception:
        pass

    try:
        ssl_ctx = ssl.create_default_context()
        url = relay_url.replace("wss://", "https://").replace("ws://", "http://")
        payload = json.dumps({
            "authors": [pubkey_hex],
            "kinds": [3],
            "limit": 1,
        }).encode()
        req = urllib.request.Request(
            url.rstrip("/") + "/n1",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10, context=ssl_ctx) as r:
            events = json.loads(r.read())
            if isinstance(events, list):
                for ev in events:
                    tags = ev.get("tags", [])
                    return {t[1] for t in tags if len(t) >= 2 and t[0] == "p"}
    except Exception:
        pass
    return set()


def fetch_follows(pubkey_hex: str, relays: list[str]) -> set[str]:
    """Try multiple relays to get a pubkey's follows."""
    for relay in relays:
        follows = fetch_kind3(pubkey_hex, relay)
        if follows:
            return follows
    return set()


def build_graph():
    """Build multi-hop trust graph from root npubs."""
    global last_refresh
    roots = CONFIG["root_npubs"]
    hops = CONFIG["hops"]
    relays = CONFIG["nostr_relays"]
    decay = 0.5

    new_graph: dict[str, dict] = {}
    frontier = set()

    for root in roots:
        hex_key = bech32_decode(root) or root
        new_graph[hex_key] = {"score": 1.0, "hop": 0}
        frontier.add(hex_key)

    for hop in range(1, hops + 1):
        next_frontier = set()
        score = decay ** hop
        for pubkey in frontier:
            if pubkey not in new_graph:
                continue
            follows = fetch_follows(pubkey, relays)
            for f in follows:
                if f not in new_graph or new_graph[f]["score"] < score:
                    new_graph[f] = {"score": score, "hop": hop}
                if f not in new_graph or new_graph[f]["hop"] == hop:
                    next_frontier.add(f)
            time.sleep(0.1)
        frontier = next_frontier

    with graph_lock:
        trust_graph.clear()
        trust_graph.update(new_graph)
        last_refresh = time.time()

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    DB_PATH.write_text(json.dumps({
        "graph": trust_graph,
        "built_at": last_refresh,
        "root_count": len(roots),
        "total_npubs": len(trust_graph),
    }, indent=2))

    print(f"[relatr] graph built: {len(trust_graph)} npubs "
          f"({len(roots)} roots, {hops} hops)")


def refresh_loop():
    while True:
        try:
            build_graph()
        except Exception as e:
            print(f"[relatr] refresh error: {e}", file=sys.stderr)
        time.sleep(CONFIG["refresh_sec"])


def allowed_pubkeys() -> list[str]:
    with graph_lock:
        return [k for k, v in trust_graph.items()
                if v["score"] >= CONFIG["min_score"]]


def score_for(pubkey: str) -> dict | None:
    with graph_lock:
        entry = trust_graph.get(pubkey)
        if entry:
            return {"score": entry["score"], "hop": entry["hop"], "allowed": entry["score"] >= CONFIG["min_score"]}
        return {"score": 0.0, "hop": -1, "allowed": False}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        elif self.path == "/allowed":
            pubkeys = allowed_pubkeys()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"count": len(pubkeys), "pubkeys": pubkeys}).encode())
        elif self.path == "/stats":
            with graph_lock:
                stats = {
                    "total_npubs": len(trust_graph),
                    "roots": len(CONFIG["root_npubs"]),
                    "hops": CONFIG["hops"],
                    "min_score": CONFIG["min_score"],
                    "last_refresh": last_refresh,
                    "age_sec": int(time.time() - last_refresh) if last_refresh else -1,
                }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(stats, indent=2).encode())
        elif self.path.startswith("/score/"):
            pubkey = self.path.split("/score/")[1].strip()
            result = score_for(pubkey)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/check":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            try:
                data = json.loads(body)
                pubkey = data.get("pubkey", "")
                result = score_for(pubkey)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())
            except Exception:
                self.send_response(400)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not CONFIG["root_npubs"]:
        print("WARNING: no ROOT_NPUBS configured. Set ROOT_NPUBS env or edit ~/.relatr/config.json", file=sys.stderr)

    t = threading.Thread(target=refresh_loop, daemon=True)
    t.start()
    time.sleep(2)
    print(f"[relatr] WoT ContextVM on :{CONFIG['port']}  "
          f"roots={len(CONFIG['root_npubs'])}  hops={CONFIG['hops']}  "
          f"graph={len(trust_graph)} npubs")
    ThreadingHTTPServer(("127.0.0.1", CONFIG["port"]), Handler).serve_forever()
