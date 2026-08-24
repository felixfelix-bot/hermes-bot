#!/usr/bin/env python3
"""
alert_dedup.py — Shared alert de-duplication + exponential backoff for cron scripts.

Stops "chatty cron" notification storms. When a cron's reported state is
unchanged since the last notification, it stays silent until a backoff
schedule elapses. When the state changes, it notifies immediately and resets
the backoff counter.

FINGERPRINT
-----------
Each caller picks a "source" name (e.g. "worker-watchdog") and provides the
set of items it would report. The module computes a stable SHA256:

    sha256( "\\n".join( sorted( "id|status" for each item ) ) )

and stores, per source, in ~/.hermes/state/alert_hashes.json:

    {
      "<source>": {
        "hash": "<sha256>",
        "consecutive":   <int>,        # unchanged notifications since last change
        "last_delivered": <unix_ts>,   # when we last actually emitted
        "last_change":    <unix_ts>,   # when the fingerprint last changed
        "last_checked":   <unix_ts>    # when this source was last evaluated
      },
      ...
    }

DECISION (state machine)
------------------------
Given the current fingerprint F and stored state for the source:

  * F is empty (nothing to report)   -> SILENT; drop the source's state (reset).
  * F differs from the stored hash    -> NOTIFY; consecutive=0; last_delivered=now.
  * F == stored hash:
      level  = min(consecutive, len(BACKOFF)-1)
      if now - last_delivered >= BACKOFF[level] -> NOTIFY; consecutive++.
      else                                       -> SILENT.

BACKOFF (seconds): 15m, 30m, 1h, 2h, 4h, 8h, 24h (cap).

PYTHON API
----------
    import alert_dedup

    items = [{"id": "t_abc", "status": "blocked"}, ...]
    if alert_dedup.gate_items("fips_autoheal", items).notify:
        print(report)
    # decide() persists state atomically under a flock; no separate mark call.

    # Arbitrary text fingerprint (e.g. for bash-fed output):
    if alert_dedup.gate_text("anomaly-notify", raw_output).notify: ...

    # Bypass backoff (weekly digest / forced flush):
    alert_dedup.gate_text("kanban-weekly-digest", text, force=True)

CLI (for bash scripts)
----------------------
  gate (default): read content from stdin; exit 0=NOTIFY, 1=SILENT, 2=ERROR.
      echo "$OUTPUT" | python3 alert_dedup.py gate --source worker-watchdog
      echo "$ITEMS_JSON" | python3 alert_dedup.py gate --source X --items
      echo "$OUTPUT"     | python3 alert_dedup.py gate --source X --force

  hash : print the SHA256 fingerprint of stdin (no state change).
      echo "$OUT" | python3 alert_dedup.py hash [--items]

  status : print per-source state as JSON.
      python3 alert_dedup.py status [--source NAME]

  reset  : clear state for a source (or --all).
      python3 alert_dedup.py reset --source NAME
      python3 alert_dedup.py reset --all

Exit codes for `gate`: 0 = deliver, 1 = suppress (backoff), 2 = error. Bash
callers should treat non-{0,1} as "fail-open" (deliver anyway) so a broken
dedup module can never silence real alerts.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
HOME = Path.home()
STATE_DIR = HOME / ".hermes" / "state"
STATE_FILE = STATE_DIR / "alert_hashes.json"
LOCK_FILE = STATE_DIR / ".alert_hashes.lock"

# Backoff schedule (seconds): 15m, 30m, 1h, 2h, 4h, 8h, 24h (cap).
BACKOFF_SECONDS = [900, 1800, 3600, 7200, 14400, 28800, 86400]

# Exit codes for the `gate` CLI command.
EXIT_NOTIFY = 0
EXIT_SILENT = 1
EXIT_ERROR = 2


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------
@dataclass
class Decision:
    """Outcome of a dedup evaluation."""
    notify: bool
    reason: str            # empty | changed | forced | backoff-elapsed | backoff-active
    source: str
    hash: str = ""
    level: int = 0         # current backoff level (1-based) when relevant
    consecutive: int = 0   # consecutive unchanged deliveries so far
    next_in_s: float = 0.0  # seconds until next eligible notification (when silent)
    last_delivered: float = 0.0


# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------
def _fingerprint_items(items: Iterable[Any]) -> str:
    """SHA256 over the sorted 'id|status' of a list of items.

    Items may be dicts (uses 'id'/'status' keys) or 2-tuples/lists. The sort
    makes the hash order-independent so reshuffling a report doesn't count as
    a change.
    """
    keys: list[str] = []
    for it in (items or []):
        if isinstance(it, dict):
            k = f"{it.get('id', '')}|{it.get('status', '')}"
        elif isinstance(it, (list, tuple)):
            k = f"{it[0]}|{it[1]}" if len(it) >= 2 else str(it)
        else:
            k = str(it)
        keys.append(k)
    if not keys:
        return ""  # empty input -> empty sentinel -> decide() treats as "nothing to report"
    canon = "\n".join(sorted(keys)).encode()
    return hashlib.sha256(canon).hexdigest()


def _fingerprint_text(text: str) -> str:
    """SHA256 over raw text (for callers that have no structured items)."""
    if not (text or "").strip():
        return ""  # empty/whitespace -> "nothing to report" sentinel
    return hashlib.sha256(text.encode()).hexdigest()


# ---------------------------------------------------------------------------
# State I/O (atomic, flock-guarded)
# ---------------------------------------------------------------------------
def _load_state() -> dict:
    try:
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text())
            if isinstance(data, dict):
                return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------------------
# Core decision
# ---------------------------------------------------------------------------
def decide(source: str, fingerprint: str, *, force: bool = False) -> Decision:
    """Evaluate one source against its stored state, persisting atomically.

    Thread- and process-safe via an exclusive flock on LOCK_FILE. The
    read-modify-write critical section is short.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.touch(exist_ok=True)
    now = time.time()

    with open(LOCK_FILE, "r+") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            state = _load_state()
            src = state.get(source, {})

            # Nothing to report -> stay silent and clear this source's state.
            if not fingerprint:
                if source in state:
                    del state[source]
                    _save_state(state)
                return Decision(False, "empty", source, "")

            prev_hash = src.get("hash")

            # State changed (or forced) -> notify and reset the counter.
            if force or prev_hash != fingerprint:
                src = {
                    "hash": fingerprint,
                    "consecutive": 0,
                    "last_delivered": now,
                    "last_change": now,
                    "last_checked": now,
                }
                state[source] = src
                _save_state(state)
                return Decision(
                    notify=True,
                    reason="forced" if force else "changed",
                    source=source,
                    hash=fingerprint,
                    level=1,
                    consecutive=0,
                    last_delivered=now,
                )

            # Same fingerprint -> honour the backoff schedule.
            consecutive = int(src.get("consecutive", 0))
            idx = min(consecutive, len(BACKOFF_SECONDS) - 1)
            gap = BACKOFF_SECONDS[idx]
            last_delivered = float(src.get("last_delivered", 0))

            if now - last_delivered >= gap:
                # Enough time elapsed -> deliver and step up the backoff.
                src["consecutive"] = consecutive + 1
                src["last_delivered"] = now
                src["last_checked"] = now
                state[source] = src
                _save_state(state)
                return Decision(
                    notify=True,
                    reason="backoff-elapsed",
                    source=source,
                    hash=fingerprint,
                    level=idx + 1,
                    consecutive=consecutive + 1,
                    last_delivered=now,
                )

            # Still within the backoff window -> suppress.
            src["last_checked"] = now
            state[source] = src
            _save_state(state)
            return Decision(
                notify=False,
                reason="backoff-active",
                source=source,
                hash=fingerprint,
                level=idx + 1,
                consecutive=consecutive,
                next_in_s=max(0.0, gap - (now - last_delivered)),
                last_delivered=last_delivered,
            )
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


# Convenience wrappers -------------------------------------------------------
def gate_items(source: str, items: Iterable[Any], *, force: bool = False) -> Decision:
    """Decide using a list of {id, status} items."""
    return decide(source, _fingerprint_items(items), force=force)


def gate_text(source: str, text: str, *, force: bool = False) -> Decision:
    """Decide using a raw text fingerprint."""
    return decide(source, _fingerprint_text(text), force=force)


def reset(source: str | None = None) -> int:
    """Clear state for one source (or all if source is None). Returns count removed."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.touch(exist_ok=True)
    removed = 0
    with open(LOCK_FILE, "r+") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            state = _load_state()
            if source is None:
                removed = len(state)
                state = {}
            elif source in state:
                del state[source]
                removed = 1
            if removed:
                _save_state(state)
            return removed
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def get_state(source: str | None = None) -> dict:
    """Read state (one source, or the whole map)."""
    state = _load_state()
    if source is None:
        return state
    return state.get(source, {})


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------
def _read_items_from_stdin() -> list:
    """Parse stdin into a list of items for --items mode.

    Accepts a JSON array of {id,status,...} dicts, or one 'id|status' /
    'id<space>status' per line.
    """
    data = sys.stdin.read()
    data = data.strip()
    if not data:
        return []
    # JSON array?
    try:
        parsed = json.loads(data)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            return [parsed]
    except (json.JSONDecodeError, ValueError):
        pass
    # Line-oriented fallback.
    items = []
    for line in data.splitlines():
        line = line.strip()
        if not line:
            continue
        if "|" in line:
            iid, _, status = line.partition("|")
        else:
            parts = line.split(None, 1)
            iid = parts[0]
            status = parts[1] if len(parts) > 1 else ""
        items.append({"id": iid, "status": status})
    return items


def _format_seconds(s: float) -> str:
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="alert_dedup.py",
        description="Shared alert de-duplication + exponential backoff for cron scripts.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gate", help="Decide notify/silent from stdin; exit 0/1/2.")
    g.add_argument("--source", required=True, help="Caller namespace (e.g. worker-watchdog).")
    g.add_argument("--items", action="store_true",
                   help="Fingerprint stdin as JSON [{id,status},...] items (default: raw text).")
    g.add_argument("--force", action="store_true",
                   help="Always notify (resets backoff). For weekly digests.")

    h = sub.add_parser("hash", help="Print the SHA256 fingerprint of stdin (no state change).")
    h.add_argument("--items", action="store_true",
                   help="Fingerprint stdin as JSON items (default: raw text).")

    s = sub.add_parser("status", help="Print stored state as JSON.")
    s.add_argument("--source", default=None, help="Limit to one source.")

    r = sub.add_parser("reset", help="Clear stored state.")
    r.add_argument("--source", default=None, help="Clear one source (default: --all).")
    r.add_argument("--all", action="store_true", help="Clear every source.")

    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.cmd == "hash":
        if args.items:
            fp = _fingerprint_items(_read_items_from_stdin())
        else:
            fp = _fingerprint_text(sys.stdin.read())
        print(fp)
        return 0

    if args.cmd == "gate":
        if args.items:
            fp = _fingerprint_items(_read_items_from_stdin())
        else:
            fp = _fingerprint_text(sys.stdin.read())
        d = decide(args.source, fp, force=args.force)
        if d.notify:
            return EXIT_NOTIFY
        # Silent: emit a one-line rationale to stderr (cron captures it as
        # script error text but stdout stays empty, so delivery is suppressed).
        print(
            f"[dedup] {d.source}: {d.reason} — level {d.level}/"
            f"{len(BACKOFF_SECONDS)}, next in ~{_format_seconds(d.next_in_s)}",
            file=sys.stderr,
        )
        return EXIT_SILENT

    if args.cmd == "status":
        print(json.dumps(get_state(args.source), indent=2, sort_keys=True))
        return 0

    if args.cmd == "reset":
        if args.source:
            n = reset(args.source)
        else:
            n = reset(None)
        print(f"reset {n} source(s)")
        return 0

    return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
