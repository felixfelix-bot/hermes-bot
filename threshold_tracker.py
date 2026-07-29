#!/usr/bin/env python3
"""threshold_tracker — adjusts model tier thresholds from real usage data.

Runs as a cron every 1h. Reads model_decisions table from zai_usage.db,
computes actual usage percentages per tier, and adjusts thresholds to
match the 10/80/10 target distribution (glm-5.2=10%, glm-4.5=80%, lower=10%).

PID controller: if heavy usage > 15%, raise heavy_min_hours by 5%.
If heavy usage < 5%, lower heavy_min_hours by 5%.
Same for CRITICAL/flash tier thresholds.
"""

from __future__ import annotations
import json, sqlite3, sys, time
from pathlib import Path

USAGE_DB = Path.home() / ".hermes" / "bot" / "zai_usage.db"
THRESHOLD_FILE = Path.home() / ".hermes" / "bot" / "tier_thresholds.json"

# Target distribution
TARGET_HEAVY_PCT = 10.0   # glm-5.2
TARGET_MID_PCT = 80.0     # glm-4.5
TARGET_LOWER_PCT = 10.0   # flash/air

# PID parameters
ADJUSTMENT_STEP = 0.05    # 5% per adjustment cycle
TOLERANCE = 0.03          # 3% dead zone — no adjustment if within target ±3%

MODEL_TIER_MAP = {
    "glm-5.2":       "heavy",
    "glm-4.5":       "mid",
    "glm-4.5-air":   "air",
    "glm-4.5-flash": "flash",
}


def get_actual_distribution() -> dict:
    """Query model_decisions table for actual tier usage over last 72h."""
    if not USAGE_DB.exists():
        return {}
    try:
        conn = sqlite3.connect(str(USAGE_DB))
        cur = conn.execute("""
            SELECT model, tier, COUNT(*) as cnt
            FROM model_decisions
            WHERE ts > strftime('%s', 'now', '-72 hours')
              AND model IS NOT NULL
            GROUP BY model, tier
            ORDER BY cnt DESC
        """)
        rows = cur.fetchall()
        conn.close()
        if not rows:
            return {}
        total = sum(r[2] for r in rows)
        tiers = {}
        for model, tier, cnt in rows:
            tier_name = tier or MODEL_TIER_MAP.get(model, "unknown")
            tiers[tier_name] = tiers.get(tier_name, 0) + cnt
        return {k: v * 100.0 / total for k, v in tiers.items()}
    except Exception:
        return {}


def load_thresholds() -> dict:
    try:
        if THRESHOLD_FILE.exists():
            return json.loads(THRESHOLD_FILE.read_text())
    except Exception:
        pass
    return {}


def save_thresholds(t: dict):
    try:
        THRESHOLD_FILE.parent.mkdir(parents=True, exist_ok=True)
        THRESHOLD_FILE.write_text(json.dumps(t, indent=2))
    except Exception:
        pass


def adjust_thresholds(t: dict, actual: dict) -> dict:
    """PID-style adjustment: move thresholds toward target distribution."""
    t = dict(t)
    heavy_pct = actual.get("heavy", 0)
    lower_pct = actual.get("flash", 0) + actual.get("air", 0)

    heavy_min = t.get("heavy_min_hours", 10.0)
    tight_max = t.get("tight_max_hours", 0.1)

    # Heavy adjustment: target 10%
    if heavy_pct > TARGET_HEAVY_PCT + TOLERANCE:
        heavy_min *= (1.0 + ADJUSTMENT_STEP)
    elif heavy_pct < TARGET_HEAVY_PCT - TOLERANCE and heavy_pct > 0:
        heavy_min *= (1.0 - ADJUSTMENT_STEP)

    # Lower tier adjustment: target 10%
    if lower_pct > TARGET_LOWER_PCT + TOLERANCE:
        tight_max *= (1.0 + ADJUSTMENT_STEP)
    elif lower_pct < TARGET_LOWER_PCT - TOLERANCE and lower_pct > 0:
        tight_max *= (1.0 - ADJUSTMENT_STEP)

    # Sanity bounds
    t["heavy_min_hours"] = max(4.0, min(48.0, heavy_min))
    t["tight_max_hours"] = max(0.05, min(2.0, tight_max))
    t["mid_max_hours"] = max(4.0, min(48.0, heavy_min))
    t["last_adjustment"] = time.time()
    t["heavy_pct_observed"] = round(heavy_pct, 1)
    t["lower_pct_observed"] = round(lower_pct, 1)

    return t


def main():
    actual = get_actual_distribution()
    if not actual:
        print("No model_decisions data yet. Skipping adjustment.")
        return

    t = load_thresholds()
    if not t:
        # Fall back to defaults from historical Kalman data
        from model_tier_router import compute_thresholds_from_history
        t = compute_thresholds_from_history()

    old_heavy = t.get("heavy_min_hours", 10.0)
    old_tight = t.get("tight_max_hours", 0.1)
    t = adjust_thresholds(t, actual)

    print(f"heavy: {actual.get('heavy', 0):.1f}% actual (target {TARGET_HEAVY_PCT:.0f}%)")
    print(f"mid:   {actual.get('mid', 0):.1f}% actual (target {TARGET_MID_PCT:.0f}%)")
    print(f"lower: {actual.get('flash',0)+actual.get('air',0):.1f}% actual (target {TARGET_LOWER_PCT:.0f}%)")
    print(f"heavy_min_hours: {old_heavy:.1f}h → {t['heavy_min_hours']:.1f}h")
    print(f"tight_max_hours: {old_tight:.2f}h → {t['tight_max_hours']:.2f}h")

    save_thresholds(t)


if __name__ == "__main__":
    main()
