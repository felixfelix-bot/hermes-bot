#!/usr/bin/env python3
"""kalman_nostr_publisher.py — Publish Kalman pricing state as a kind-30315
replaceable Nostr event for cross-VPS state sharing (contextvm data plane).

Each Kalman instance publishes its exhaustion-gate output (kalman_pricing.json)
as a parameterized replaceable event (NIP-33) so peers can fetch the latest
state without polling a DB or rsyncing files.

Usage:
  python kalman_nostr_publisher.py   # publishes ~/.hermes/bot/kalman_pricing.json

State file: ~/.hermes/bot/kalman_pricing.json (written by exhaustion-gate.py)
Nostr key: ~/.hermes/bot/kalman_npub.nsec (raw hex private key)
Relays: public relays + local strfry if available
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

HOME = Path.home()
STATE_FILE = HOME / ".hermes" / "bot" / "kalman_pricing.json"
NSEC_FILE = HOME / ".hermes" / "bot" / "kalman_npub.nsec"

RELAYS = [
    "wss://relay.primal.net",
    "wss://nostr.oxtr.dev",
    "wss://relay.damus.io",
]

EVENT_KIND = 30315
EVENT_D_TAG = "kalman-pricing-state"


def _load_nsec():
    raw = NSEC_FILE.read_text().strip()
    if raw.startswith("nsec1"):
        from pynostr.key import PrivateKey
        return PrivateKey.from_nsec(raw)
    else:
        from pynostr.key import PrivateKey
        return PrivateKey(bytes.fromhex(raw))


def _local_relay():
    import socket
    for port in (7777, 7778):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.5)
            s.connect(("127.0.0.1", port))
            s.close()
            return f"ws://127.0.0.1:{port}"
        except Exception:
            pass
    return None


def publish():
    if not STATE_FILE.exists():
        print(f"[nostr-pub] No state file at {STATE_FILE} — skipping")
        return False

    state = json.loads(STATE_FILE.read_text())

    content = json.dumps({
        "generated_at": state.get("generated_at"),
        "providers": {k: {
            "effective_rate_per_m": v.get("effective_rate_per_m"),
            "p_exhaust": v.get("p_exhaust"),
            "weekly_used_pct": v.get("weekly_used_pct"),
            "session_used_pct": v.get("session_used_pct"),
            "delisted": v.get("delisted", False),
        } for k, v in state.get("providers", {}).items()},
        "accuracy": state.get("accuracy"),
        "sat_per_usd": state.get("sat_per_usd"),
        "node_hostname": os.uname().nodename,
    }, separators=(",", ":"))

    try:
        from pynostr.event import Event
        from pynostr.key import PrivateKey
        from pynostr.relay import Relay

        sk = _load_nsec()
        ev = Event(
            kind=EVENT_KIND,
            content=content,
            tags=[["d", EVENT_D_TAG]],
            created_at=int(time.time()),
        )
        ev.sign(sk.hex())

        relays = list(RELAYS)
        local = _local_relay()
        if local:
            relays.insert(0, local)

        published = 0
        for url in relays:
            try:
                r = Relay(url)
                r.connect(timeout=3)
                r.publish(ev)
                r.close()
                published += 1
            except Exception:
                pass

        print(f"[nostr-pub] Published kind-{EVENT_KIND} to {published}/{len(relays)} relays "
              f"({len(content)} bytes, npub={sk.public_key.hex()[:8]}...)")
        return published > 0

    except Exception as e:
        print(f"[nostr-pub] Error: {e}")
        return False


if __name__ == "__main__":
    publish()
