#!/usr/bin/env python3
"""buzz_signal_bridge — bidirectional Signal <-> Buzz (NIP-29) group bridge.

Purpose: expose the two Protein-RNA Signal groups (served by the local
manager hermes with full session context) to Sitarani via Buzz desktop on
relay.orangesync.tech, while her own tenant container is being finished.

Direction 1 (Signal -> Buzz): consume the signal-cli daemon SSE stream
(GET /api/v1/events) and forward group messages (Felix's, the unknown
member's, hermes's replies) to the matching buzz channel via `nak event`.

Direction 2 (Buzz -> Signal): stream kind-9 events per channel via
`nak req --stream --auth` and forward other people's messages into the
Signal group via the signal-cli JSON-RPC `send` — the exact wire format the
hermes gateway uses (gateway/platforms/signal.py), so hermes processes them
under the EXISTING group sessions (verified live 2026-08-22).

Loop prevention:
  - own buzz npub events are skipped (bridge's own posts)
  - signal-cli `send` returns a timestamp — recorded and skipped when the
    SSE echo of our own send arrives
  - content-hash of every message we inject into Signal is pre-registered;
    the SSE echo is matched on hash too
  - buzz event ids deduped (bounded ring), `since` cursor persisted

Text-only v1: attachments become "[attachment]"; reactions/edits ignored.
"""
from __future__ import annotations

import collections
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

HOME = Path.home()
CONFIG_PATH = HOME / ".hermes" / "bot" / "buzz_signal_bridge.json"
STATE_PATH = HOME / ".hermes" / "bot" / ".buzz_signal_bridge_state.json"
NSEC_PATH = HOME / ".hermes" / "profiles" / "manager" / "keys" / "buzz_bridge_nsec.txt"
LOG_PREFIX = "[buzz-signal-bridge]"

SIGNAL_HTTP = "http://127.0.0.1:8080"
SIGNAL_ACCOUNT = "+18102940908"
RELAY = "wss://relay.orangesync.tech"

# sourceUuid (Signal) -> label
UUID_LABELS = {
    "cc5bdaa4-98d2-4ce2-af1d-93aab049868a": "Felix",
    "35a17b31-f2e3-4d03-8220-aa31ff470ba6": "member",
}
# buzz pubkey hex -> label
BUZZ_LABELS = {
    "ec79b568bdea63ca6091f5b84b0c639c10a0919e175fa09a4de3154f82906f25": "Sitarani",
    "1a31189f46e89d327e6a4fa26376ba5fa81caaec453fab13b1c2f8245e42ba9d": "Felix",
}

NAK = os.path.expanduser("~/.local/bin/nak")


def log(*parts) -> None:
    print(f"{LOG_PREFIX}", *parts, flush=True)


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------
class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last_seen: dict[str, int] = {}
        self.seen_ids: collections.deque = collections.deque(maxlen=500)
        self.sent_ts: set[int] = set()          # our signal send timestamps
        self.sent_hashes: set[str] = set()      # content we injected into signal
        try:
            data = json.loads(STATE_PATH.read_text())
            self.last_seen = {k: int(v) for k, v in data.get("last_seen", {}).items()}
            self.seen_ids.extend(data.get("seen_ids", [])[-500:])
        except Exception:
            pass

    def save(self) -> None:
        try:
            STATE_PATH.write_text(json.dumps({
                "last_seen": self.last_seen,
                "seen_ids": list(self.seen_ids)[-500:],
            }))
        except Exception as e:
            log("state save failed:", e)

    def register_send(self, ts: int, text: str) -> None:
        with self.lock:
            if ts:
                self.sent_ts.add(ts)
            self.sent_hashes.add(hashlib.sha1(text.encode()).hexdigest()[:16])

    def is_own_echo(self, ts, text: str) -> bool:
        with self.lock:
            if ts and ts in self.sent_ts:
                return True
            h = hashlib.sha1(text.encode()).hexdigest()[:16]
            return h in self.sent_hashes

    def mark_seen(self, event_id: str, channel: str, created_at: int) -> bool:
        """Returns True if NEW (first sighting)."""
        with self.lock:
            if event_id in self.seen_ids:
                return False
            self.seen_ids.append(event_id)
            cur = self.last_seen.get(channel, 0)
            if created_at > cur:
                self.last_seen[channel] = created_at
            return True


STATE = State()
PAIRS: list[dict] = []
SIGNAL_TO_PAIR: dict[str, dict] = {}
BUZZ_TO_PAIR: dict[str, dict] = {}


def load_config() -> None:
    global PAIRS
    cfg = json.loads(CONFIG_PATH.read_text())
    PAIRS = cfg["pairs"]
    for p in PAIRS:
        SIGNAL_TO_PAIR[p["signal_group"]] = p
        BUZZ_TO_PAIR[p["buzz_channel"]] = p


# ---------------------------------------------------------------------------
# buzz side helpers (nak subprocess)
# ---------------------------------------------------------------------------
def buzz_send(channel: str, text: str, nsec: str) -> None:
    for attempt in range(3):
        try:
            r = subprocess.run(
                [NAK, "event", "-k", "9", "-t", f"h={channel}", "-c", text,
                 "--auth", "--sec", nsec, RELAY],
                capture_output=True, text=True, timeout=60)
            if r.returncode == 0 and "success" in (r.stdout + r.stderr):
                return
            log("buzz send retry", attempt, (r.stdout + r.stderr)[-160:])
        except Exception as e:
            log("buzz send error:", e)
        time.sleep(3 * (attempt + 1))
    log("buzz send FAILED for channel", channel)


def buzz_stream(channel: str, nsec: str, out_q) -> None:
    """Long-running nak req --stream subprocess; pushes raw event lines."""
    while True:
        since = int(STATE.last_seen.get(channel, int(time.time()) - 5))
        filt = json.dumps({"kinds": [9], "#h": [channel], "since": since})
        try:
            proc = subprocess.Popen(
                [NAK, "req", "--stream", "--auth", "--sec", nsec, RELAY],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1)
            proc.stdin.write(filt + "\n")
            proc.stdin.flush()
            log("buzz stream connected:", channel)
            for line in proc.stdout:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    out_q.put(json.loads(line))
                except json.JSONDecodeError:
                    continue
            # stream ended unexpectedly
            proc.wait(timeout=10)
        except Exception as e:
            log("buzz stream error:", e)
        time.sleep(5)


# ---------------------------------------------------------------------------
# signal side helpers (JSON-RPC)
# ---------------------------------------------------------------------------
def signal_send(group_id: str, message: str) -> None:
    payload = {
        "jsonrpc": "2.0", "id": f"bridge-{int(time.time()*1000)}",
        "method": "send",
        "params": {"account": SIGNAL_ACCOUNT, "groupId": group_id,
                   "message": message},
    }
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                f"{SIGNAL_HTTP}/api/v1/rpc",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            if "error" in data:
                log("signal rpc error:", str(data["error"])[:160])
                time.sleep(5 * (attempt + 1))
                continue
            # record timestamps from the result to skip our SSE echo
            results = (data.get("result") or {}).get("results", [])
            if isinstance(data.get("result"), dict) and data["result"].get("timestamp"):
                STATE.register_send(int(data["result"]["timestamp"]), message)
            STATE.register_send(0, message)  # hash registration
            return
        except Exception as e:
            log("signal send error:", e)
        time.sleep(5 * (attempt + 1))
    log("signal send FAILED to group", group_id[:12])


# ---------------------------------------------------------------------------
# Signal -> Buzz (SSE consumer)
# ---------------------------------------------------------------------------
def extract_group_text(envelope: dict):
    """Return (group_id, text, label) or None. Mirrors gateway logic."""
    env = envelope.get("envelope", envelope)
    label = None
    # syncMessage: hermes's own outgoing group replies (promoted like the
    # gateway does), or our own send echo (skipped by caller via timestamp)
    if "syncMessage" in env:
        sm = env.get("syncMessage") or {}
        sent = sm.get("sentMessage") or {}
        gi = sent.get("groupInfo") or {}
        gid = gi.get("groupId")
        if not gid:
            return None
        text = sent.get("message") or ""
        atts = sent.get("attachments") or []
        src = env.get("sourceNumber") or env.get("sourceUuid") or ""
        label = "hermes" if not src or src == SIGNAL_ACCOUNT else UUID_LABELS.get(env.get("sourceUuid", ""), "signal-user")
        return gid, (text or ("[attachment]" if atts else "")), label
    dm = env.get("dataMessage") or {}
    gi = dm.get("groupInfo") or {}
    gid = gi.get("groupId")
    if not gid:
        return None
    if env.get("storyMessage"):
        return None
    text = dm.get("message") or ""
    atts = dm.get("attachments") or []
    uuid = env.get("sourceUuid") or ""
    num = env.get("sourceNumber") or ""
    if num == SIGNAL_ACCOUNT:
        label = "hermes"
    else:
        label = UUID_LABELS.get(uuid) or (env.get("sourceName") or "signal-user")
    return gid, (text or ("[attachment]" if atts else "")), label


def sse_consumer(nsec: str) -> None:
    from urllib.parse import quote
    url = f"{SIGNAL_HTTP}/api/v1/events?account={quote(SIGNAL_ACCOUNT, safe='')}"
    backoff = 2
    while True:
        try:
            req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
            with urllib.request.urlopen(req, timeout=None) as resp:
                log("signal SSE connected")
                backoff = 2
                buf = ""
                for chunk in iter(lambda: resp.read(4096), b""):
                    buf += chunk.decode("utf-8", "replace")
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.strip()
                        if not line or line.startswith(":"):
                            continue
                        if not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if not data_str:
                            continue
                        try:
                            envelope = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        try:
                            handle_signal_envelope(envelope, nsec)
                        except Exception as e:
                            log("envelope handler error:", e)
        except Exception as e:
            log("signal SSE error:", e, f"reconnecting in {backoff}s")
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)


def handle_signal_envelope(envelope: dict, nsec: str) -> None:
    env = envelope.get("envelope", envelope)
    # capture the sync timestamp for echo detection
    ts = None
    if "syncMessage" in env:
        sent = (env.get("syncMessage") or {}).get("sentMessage") or {}
        ts = sent.get("timestamp")
    extracted = extract_group_text(envelope)
    if not extracted:
        return
    gid, text, label = extracted
    if not text:
        return
    pair = SIGNAL_TO_PAIR.get(gid)
    if not pair:
        return
    if STATE.is_own_echo(ts, f"[buzz] {label}: {text}" if False else text):
        # our own bridge send echo — skip
        return
    out = f"[signal] {label}: {text}"
    log(f"signal->buzz [{pair['name']}] {label}: {text[:60]}")
    buzz_send(pair["buzz_channel"], out, nsec)


# ---------------------------------------------------------------------------
# Buzz -> Signal (stream consumer)
# ---------------------------------------------------------------------------
def handle_buzz_event(ev: dict) -> None:
    if ev.get("kind") != 9:
        return
    gid = None
    for t in ev.get("tags", []):
        if len(t) >= 2 and t[0] == "h":
            gid = t[1]
    if not gid:
        return
    pair = BUZZ_TO_PAIR.get(gid)
    if not pair:
        return
    pub = ev.get("pubkey", "")
    if pub == BRIDGE_NPUB_HEX:
        return  # own message
    created = int(ev.get("created_at", 0))
    eid = ev.get("id", "")
    if not STATE.mark_seen(eid, gid, created):
        return
    STATE.save()
    text = (ev.get("content") or "").strip()
    if not text:
        return
    label = BUZZ_LABELS.get(pub) or f"npub…{pub[-6:]}"
    msg = f"[buzz] {label}: {text}"
    log(f"buzz->signal [{pair['name']}] {label}: {text[:60]}")
    signal_send(pair["signal_group"], msg)


BRIDGE_NPUB_HEX = ""


def main() -> None:
    global BRIDGE_NPUB_HEX
    load_config()
    nsec = NSEC_PATH.read_text().strip()
    BRIDGE_NPUB_HEX = subprocess.run(
        [NAK, "key", "public", nsec], capture_output=True, text=True).stdout.strip()

    import queue
    q: "queue.Queue[dict]" = queue.Queue()

    # buzz streams (one thread per channel)
    for pair in PAIRS:
        threading.Thread(
            target=buzz_stream, args=(pair["buzz_channel"], nsec, q),
            daemon=True, name=f"buzz-{pair['name']}").start()

    # signal SSE
    threading.Thread(target=sse_consumer, args=(nsec,), daemon=True,
                     name="signal-sse").start()

    log("bridge started:", len(PAIRS), "pairs; npub", BRIDGE_NPUB_HEX[:8] + "…")
    # periodic state save
    last_save = time.time()
    while True:
        try:
            ev = q.get(timeout=30)
            handle_buzz_event(ev)
        except queue.Empty:
            pass
        if time.time() - last_save > 60:
            STATE.save()
            last_save = time.time()


if __name__ == "__main__":
    main()
