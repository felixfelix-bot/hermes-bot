#!/usr/bin/env python3
"""
ppq_data_collector.py — Collect PPQ query history and store in zai_usage.db.

Polls api.ppq.ai's query history endpoint for per-query cost data,
maps each query to the zai_usage.db api_calls table by timestamp matching,
or inserts new rows when no match exists.

Runs every 5 min via cron (no_agent=true).
Silent on success, outputs only on error or when new data collected.

Requires: PPQ_API_KEY in ~/.hermes/.env
Without it, exits silently with "no PPQ_API_KEY".
"""

import json
import os
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DB = Path.home() / ".hermes" / "bot" / "zai_usage.db"
ENV_FILE = Path.home() / ".hermes" / ".env"
PPQ_HISTORY_URL = "https://api.ppq.ai/queries/history"
LOOKBACK_HOURS = 72  # how far back to check for missed PPQ rows


def load_ppq_key():
    """Load PPQ_API_KEY from .env file."""
    if not ENV_FILE.exists():
        return None
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line.startswith("PPQ_API_KEY="):
            val = line.split("=", 1)[1].strip("\"'")
            return val
    return None


def fetch_ppq_history(api_key, limit=100, offset=0):
    """Fetch PPQ query history. Returns list of query records."""
    url = f"{PPQ_HISTORY_URL}?limit={limit}&offset={offset}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        return data.get("queries", data if isinstance(data, list) else [])
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"ERROR: PPQ API HTTP {e.code}: {body}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"ERROR: PPQ fetch failed: {e}", file=sys.stderr)
        return None


def store_ppq_rows(db, queries):
    """Store PPQ query records into zai_usage.db as api_calls rows."""
    inserted = 0
    for q in queries:
        q_ts = q.get("timestamp") or q.get("created_at") or q.get("ts")
        if not q_ts:
            continue

        # Convert to unix timestamp
        if isinstance(q_ts, str):
            try:
                q_ts = datetime.fromisoformat(q_ts.replace("Z", "+00:00")).timestamp()
            except ValueError:
                q_ts = time.mktime(time.strptime(q_ts, "%Y-%m-%dT%H:%M:%S"))
        ts = float(q_ts)

        # Extract token counts
        total_tokens = q.get("total_tokens") or q.get("tokens") or 0
        prompt_tokens = q.get("prompt_tokens") or 0
        completion_tokens = q.get("completion_tokens") or 0
        model = q.get("model") or q.get("engine") or "unknown"
        cost_usd = q.get("cost") or q.get("cost_usd") or q.get("price") or 0

        # Check if this query already exists (by approximate timestamp + tokens)
        existing = db.execute("""
            SELECT id FROM api_calls
            WHERE ABS(ts - ?) < 5 AND ppq_hit = 1 AND total_tokens = ?
            LIMIT 1
        """, (ts, total_tokens)).fetchone()

        if existing:
            continue

        # Insert new PPQ row
        db.execute("""
            INSERT INTO api_calls
                (ts, key_name, model, prompt_tokens, completion_tokens,
                 total_tokens, status_code, ppq_hit, duration_ms)
            VALUES (?, 'ppq', ?, ?, ?, ?, 200, 1, ?)
        """, (ts, model, prompt_tokens, completion_tokens, total_tokens,
              q.get("duration_ms") or q.get("response_time", 0)))
        inserted += 1

    return inserted


def collect():
    """Main collection routine."""
    api_key = load_ppq_key()
    if not api_key:
        print("no PPQ_API_KEY — skipping collection", file=sys.stderr)
        return 0

    queries = fetch_ppq_history(api_key, limit=200)
    if queries is None:
        return -1

    if not queries:
        print("no PPQ queries found in history", file=sys.stderr)
        return 0

    db = sqlite3.connect(str(DB))
    try:
        inserted = store_ppq_rows(db, queries)
        if inserted > 0:
            db.commit()
            print(f"inserted {inserted} new PPQ rows into zai_usage.db")
        else:
            # Silent on no new data
            pass
    finally:
        db.close()

    return inserted


if __name__ == "__main__":
    count = collect()
    if count > 0:
        print(f"PPQ collector: {count} new rows")
    elif count == -1:
        sys.exit(1)
