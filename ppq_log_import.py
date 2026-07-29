#!/usr/bin/env python3
"""
ppq_log_import.py — Import PPQ usage data into zai_usage.db api_calls table.

PPQ calls are made directly to api.ppq.ai bypassing the z.ai proxy,
so they never get logged to zai_usage.db. This script bridges the gap by:
1. Reading ppq_usage.json usage_history (tokens per call)
2. Reading api_burn.db balance snapshots for USD cost
3. Writing synthetic api_calls rows with key_name='ppq'

Run manually: python3 ppq_log_import.py
Or via cron: every 15 min to catch recent PPQ calls.
"""

import json, sqlite3, time
from datetime import datetime, timezone
from pathlib import Path

USAGE_FILE = Path.home() / ".hermes" / "bot" / "ppq_usage.json"
API_BURN_DB = Path.home() / ".hermes" / "bot" / "api_burn.db"
ZAI_USAGE_DB = Path.home() / ".hermes" / "bot" / "zai_usage.db"


def get_last_imported_ts():
    """Get the most recent PPQ call timestamp already in api_calls."""
    db = sqlite3.connect(str(ZAI_USAGE_DB))
    row = db.execute(
        "SELECT MAX(ts) FROM api_calls WHERE key_name='ppq'"
    ).fetchone()
    db.close()
    return row[0] or 0


def import_ppq_usage(last_ts):
    """Import PPQ usage history from ppq_usage.json into api_calls."""
    if not USAGE_FILE.exists():
        print(f"  No {USAGE_FILE}, nothing to import")
        return 0

    usage = json.loads(USAGE_FILE.read_text())
    history = usage.get("usage_history", [])

    if not history:
        print(f"  ppq_usage.json has no usage_history entries")
        return 0

    db = sqlite3.connect(str(ZAI_USAGE_DB))
    count = 0
    for entry in history:
        ts_str = entry.get("timestamp")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str).timestamp()
        except Exception:
            continue
        if ts <= last_ts:
            continue  # already imported

        tokens = entry.get("tokens", 0)
        cost_usd = entry.get("cost_usd", 0)
        model = entry.get("model", "unknown")

        # Estimate token breakdown (PPQ doesn't split prompt/completion)
        prompt_tokens = int(tokens * 0.6) if tokens else 0
        completion_tokens = tokens - prompt_tokens

        try:
            db.execute(
                """INSERT INTO api_calls
                   (ts, key_name, key_suffix, model,
                    prompt_tokens, completion_tokens, total_tokens,
                    tier, cache_hit, ollama_hit, ppq_hit,
                    status_code, error, duration_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ts,
                    "ppq",
                    "ppq",
                    model,
                    prompt_tokens,
                    completion_tokens,
                    tokens,
                    "ppq",
                    0,  # cache_hit
                    0,  # ollama_hit
                    1,  # ppq_hit
                    200,  # status_code
                    None,
                    1000,  # estimated duration ms
                ),
            )
            count += 1
        except Exception as e:
            print(f"  Error inserting PPQ call at {ts_str}: {e}")

    db.commit()
    db.close()
    return count


def import_ppq_balance_spend(last_ts):
    """Estimate PPQ token usage from balance snapshots in api_burn.db."""
    if not API_BURN_DB.exists():
        return 0

    db = sqlite3.connect(str(API_BURN_DB))
    snaps = db.execute(
        "SELECT ts, balance_usd, total_usage, raw "
        "FROM balance_snapshots "
        "WHERE provider='ppq' AND balance_usd IS NOT NULL "
        "ORDER BY ts ASC"
    ).fetchall()
    db.close()

    if len(snaps) < 2:
        return 0

    zai_db = sqlite3.connect(str(ZAI_USAGE_DB))
    count = 0
    prev_ts, prev_bal, prev_usage, prev_raw = None, None, None, None

    for s_ts, s_bal, s_usage, s_raw in snaps:
        if prev_ts is None:
            prev_ts, prev_bal, prev_usage = s_ts, s_bal, s_usage
            continue
        if s_ts <= last_ts:
            prev_ts, prev_bal, prev_usage = s_ts, s_bal, s_usage
            continue

        # Check if balance dropped (spend happened)
        if prev_bal is not None and s_bal is not None and s_bal < prev_bal:
            spent = prev_bal - s_bal
            if spent > 0 and spent < 1.0:  # small spend events
                # Estimate tokens: PPQ models ~$0.15/M tokens for flash
                tokens_est = int(spent / 0.15 * 1_000_000)
                mid_ts = (prev_ts + s_ts) / 2

                try:
                    zai_db.execute(
                        """INSERT INTO api_calls
                           (ts, key_name, key_suffix, model,
                            prompt_tokens, completion_tokens, total_tokens,
                            tier, cache_hit, ollama_hit, ppq_hit,
                            status_code, error, duration_ms)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            mid_ts,
                            "ppq",
                            "ppq",
                            "glm-4.5-flash",
                            int(tokens_est * 0.6),
                            tokens_est - int(tokens_est * 0.6),
                            tokens_est,
                            "ppq",
                            0, 0, 1,  # cache_hit, ollama_hit, ppq_hit
                            200, None,
                            1000,
                        ),
                    )
                    count += 1
                    print(f"  Estimated PPQ spend: ${spent:.4f} ≈ {tokens_est:,} tokens at {datetime.fromtimestamp(mid_ts, tz=timezone.utc)}")
                except Exception as e:
                    print(f"  Error inserting PPQ balance-based row: {e}")

        prev_ts, prev_bal, prev_usage = s_ts, s_bal, s_usage

    zai_db.commit()
    zai_db.close()
    return count


if __name__ == "__main__":
    print("=== PPQ Log Import ===")
    last_ts = get_last_imported_ts()
    print(f"Last imported PPQ ts: {datetime.fromtimestamp(last_ts, tz=timezone.utc) if last_ts > 0 else 'never'}")

    c1 = import_ppq_usage(last_ts)
    print(f"Imported {c1} PPQ calls from usage_history")

    c2 = import_ppq_balance_spend(last_ts)
    print(f"Imported {c2} PPQ calls from balance snapshots")

    print(f"Total: {c1 + c2} new rows")
    print("Done")
