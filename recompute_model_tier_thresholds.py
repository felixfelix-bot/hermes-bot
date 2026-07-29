#!/usr/bin/env python3
"""Recompute model tier thresholds from historic Kalman data.

Computes p10 and p90 percentiles of exhausts_in_hours for each key's
binding window (friend=5-hour, ours=weekly). These thresholds define
the 10%/80%/10% model tier split: economy < p10, reasoning > p90,
standard in between.

Run weekly to auto-adjust as usage patterns change. Updates
model_tier_thresholds.json in ~/.hermes/bot/.

Exit codes: 0 = updated, non-zero = error (silent — cron anomaly-only).
"""
import json
import sqlite3
import sys
import time
from pathlib import Path

DB_PATH = Path.home() / ".hermes" / "bot" / "zai_usage.db"
OUT_PATH = Path.home() / ".hermes" / "bot" / "model_tier_thresholds.json"

# For each key, which window is the binding constraint
BINDING_WINDOWS = {
    "friend": "5-hour",
    "ours": "weekly",
}


def compute_percentiles(values, n):
    """Compute p10 and p90 from sorted values list."""
    if not values:
        return None, None
    idx10 = int(len(values) * 0.10)
    idx90 = int(len(values) * 0.90)
    idx10 = min(idx10, len(values) - 1)
    idx90 = min(idx90, len(values) - 1)
    return values[idx10], values[idx90]


def main() -> int:
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}", file=sys.stderr)
        return 0  # silent — no data yet, no problem

    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()

    thresholds = {}
    for key, window in BINDING_WINDOWS.items():
        rows = cur.execute(
            "SELECT exhausts_in_hours FROM kalman_samples "
            "WHERE key=? AND window=? "
            "AND exhausts_in_hours IS NOT NULL AND exhausts_in_hours > 0 "
            "ORDER BY ts DESC LIMIT 3000",
            (key, window)
        ).fetchall()

        vals = sorted(r[0] for r in rows)
        if len(vals) < 20:
            # Not enough data — skip
            continue

        p10, p90 = compute_percentiles(vals, len(vals))
        thresholds[key] = {
            "window": window,
            "p10_exhaust": round(p10, 1),
            "p90_exhaust": round(p90, 1),
            "n_samples": len(vals),
            "computed_at": int(time.time()),
            "note": "Dynamic percentile: flash < p10, reasoning > p90, standard in middle",
        }

    conn.close()

    # Keep existing thresholds for keys we didn't recompute
    if OUT_PATH.exists():
        try:
            existing = json.loads(OUT_PATH.read_text())
            for k in existing:
                if k not in thresholds:
                    thresholds[k] = existing[k]
        except (json.JSONDecodeError, OSError):
            pass

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(thresholds, indent=2))
    print(f"Updated thresholds: {json.dumps(thresholds)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
