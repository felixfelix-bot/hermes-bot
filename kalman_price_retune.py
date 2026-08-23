#!/usr/bin/env python3
"""
kalman_price_retune.py — Grid-search optimal Kalman R/Q for price estimation.

Reads hourly observations from zai_usage.db, runs KalmanPredictor with various
measurement_noise (R) values, and picks the one with lowest MAPE.

Run manually: python3 kalman_price_retune.py
Schedule: weekly via cron (no_agent=true)
"""

import json
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent))
from burn_predictor import KalmanPredictor
import numpy as np

DB = Path.home() / ".hermes" / "bot" / "zai_usage.db"
TUNING_FILE = Path(__file__).parent / "kalman_price_tuning.json"
LOOKBACK_HOURS = 168  # 7 days


def get_volume_history(db):
    """Get hourly token volumes for all keys."""
    cutoff = datetime.now(timezone.utc).timestamp() - LOOKBACK_HOURS * 3600
    keys = {}
    for key in ("ours", "friend", "ppq"):
        rows = db.execute("""
            SELECT CAST(ts / 3600 AS INTEGER) * 3600 as hour_ts,
                   SUM(total_tokens) as tokens
            FROM api_calls
            WHERE ts > ? AND status_code = 200 AND key_name = ?
            GROUP BY hour_ts
            ORDER BY hour_ts ASC
        """, (cutoff, key)).fetchall()
        volumes = [r[1] or 0 for r in rows]
        if any(v > 0 for v in volumes):
            keys[key] = volumes
    return keys


def grid_search(volumes, r_values, q_values):
    """
    Try all R×Q combinations and find the one with lowest MAPE.
    Returns best {measurement_noise, process_noise, mape}.
    """
    best = {"mape": float("inf"), "measurement_noise": 10.0, "process_noise": 0.1}

    for r in r_values:
        for q in q_values:
            kf = KalmanPredictor(process_noise=q, measurement_noise=r)
            errors = []

            for v in volumes:
                if v > 0:
                    # Predict one step ahead
                    pred = float(kf.x[0, 0])
                    kf.update(float(v))
                    kf.predict()

                    if pred > 0 and v > 0:
                        pct_error = abs(v - pred) / v * 100
                        errors.append(pct_error)
                else:
                    # Idle hour — just predict, no update
                    kf.predict()

            if errors:
                mape = sum(errors) / len(errors)
                if mape < best["mape"]:
                    best = {
                        "mape": round(mape, 2),
                        "measurement_noise": r,
                        "process_noise": q,
                    }
    return best


def main():
    db = sqlite3.connect(str(DB))
    volumes = get_volume_history(db)
    db.close()

    if not volumes:
        print("no volume data found — skipping retune")
        return

    # Search grid: R from 0.1 to 100, Q from 0.01 to 10
    r_values = [0.1, 0.5, 1.0, 5.0, 10.0, 25.0, 50.0, 100.0]
    q_values = [0.01, 0.05, 0.1, 0.5, 1.0, 2.0, 5.0, 10.0]

    tuning = {}
    for key, vols in volumes.items():
        best = grid_search(vols, r_values, q_values)
        tuning[key] = {
            "measurement_noise": best["measurement_noise"],
            "process_noise": best["process_noise"],
            "mape_at_tune": best["mape"],
            "retuned_at": datetime.now(timezone.utc).isoformat(),
        }

    # Merge with existing tuning
    existing = {}
    if TUNING_FILE.exists():
        existing = json.loads(TUNING_FILE.read_text())
    existing.update(tuning)

    TUNING_FILE.write_text(json.dumps(existing, indent=2))
    print(f"kalman_price_tuning.json updated: {json.dumps(tuning, indent=2)}")


if __name__ == "__main__":
    main()
