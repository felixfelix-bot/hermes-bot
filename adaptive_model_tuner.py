#!/usr/bin/env python3
"""adaptive_model_tuner — dynamically adjusts model tier thresholds to hit target usage mix.

Target mix (from user):
  - glm-5.2: ~10% of requests (when most quota headroom)
  - glm-4.5 variants: ~80% of requests (normal operation)
  - glm-4.5-flash/air: ~10% of requests (tightest quota)

Reads historic Kalman hours_left data for the 5-hour window (the real bottleneck),
computes percentile thresholds, writes them to a JSON file consumed by model_tier_router.py.

Usage:
  python3 adaptive_model_tuner.py           # tune + write thresholds
  python3 adaptive_model_tuner.py --stats   # show current distribution only
  python3 adaptive_model_tuner.py --dry-run # show what would change
"""

from __future__ import annotations
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

DB_PATH = Path.home() / ".hermes" / "bot" / "zai_usage.db"
THRESHOLD_FILE = Path.home() / ".hermes" / "bot" / "model_tier_thresholds.json"

# Target mix percentages (user-specified)
TARGET_PCT = {
    "economy": 10,     # glm-4.5-flash/air — tightest quota
    "standard": 80,    # glm-4.5 variants — normal operation
    "premium": 10,     # glm-5.2 — most headroom
}

# Default thresholds (used when no historic data yet)
DEFAULT_THRESHOLDS = {
    "economy_max_hours": 0.5,    # hours_left < 0.5 → economy tier
    "premium_min_hours": 200.0,  # hours_left > 200 → premium tier
    "peak_cap_tier": "air",      # during peak, cap at air
    "updated_at": None,
    "source": "defaults",
}


def get_hours_left_samples(db_path: Path, key: str = "friend",
                            window: str = "5-hour") -> list[float]:
    """Get exhausts_in_hours samples from Kalman data for a specific key+window."""
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            """SELECT exhausts_in_hours FROM kalman_samples
               WHERE key = ? AND window = ?
                 AND exhausts_in_hours IS NOT NULL
               ORDER BY ts DESC LIMIT 1000""",
            (key, window)
        ).fetchall()
        conn.close()
        return [r[0] for r in rows if r[0] is not None]
    except Exception:
        return []


def compute_percentile_thresholds(samples: list[float]) -> dict:
    """Compute hours_left thresholds from percentiles to hit target mix."""
    if not samples:
        return dict(DEFAULT_THRESHOLDS)
    
    total = len(samples)
    sorted_s = sorted(samples)
    
    # P10 = hours_left below which we're in the lowest 10% → economy tier
    p10_idx = max(0, int(total * (TARGET_PCT["economy"] / 100)))
    p10_val = sorted_s[min(p10_idx, total - 1)]
    
    # P90 = hours_left above which we're in the top 10% → premium tier
    p90_idx = min(total - 1, int(total * ((100 - TARGET_PCT["premium"]) / 100)))
    p90_val = sorted_s[p90_idx]
    
    return {
        "economy_max_hours": round(max(p10_val, 0.1), 1),   # floor at 0.1h
        "premium_min_hours": round(p90_val, 1),
        "peak_cap_tier": "air",
        "updated_at": time.time(),
        "source": "adaptive_percentile",
        "samples_used": total,
        "target_economy_pct": TARGET_PCT["economy"],
        "target_standard_pct": TARGET_PCT["standard"],
        "target_premium_pct": TARGET_PCT["premium"],
        "p10_hours": round(p10_val, 1),
        "p90_hours": round(p90_val, 1),
        "median_hours": round(sorted_s[total // 2], 1),
    }


def write_thresholds(thresholds: dict):
    """Write thresholds to JSON file for model_tier_router.py to consume."""
    THRESHOLD_FILE.parent.mkdir(parents=True, exist_ok=True)
    THRESHOLD_FILE.write_text(json.dumps(thresholds, indent=2))
    print(f"Wrote thresholds to {THRESHOLD_FILE}")


def show_stats(samples: list[float]):
    """Print distribution statistics."""
    if not samples:
        print("No historic data available.")
        return
    
    total = len(samples)
    sorted_s = sorted(samples)
    
    print(f"Historic hours_left samples (5-hour window): {total} data points")
    print(f"  Min:     {sorted_s[0]:.1f}h")
    print(f"  Max:     {sorted_s[-1]:.1f}h")
    print(f"  Median:  {sorted_s[total//2]:.1f}h")
    print(f"  Mean:    {sum(sorted_s)/total:.1f}h")
    print(f"")
    
    # Distribution buckets
    buckets = [("0-0.5h", 0), ("0.5-2h", 0), ("2-4h", 0), ("4-6h", 0),
               ("6-12h", 0), ("12-48h", 0), (">48h", 0)]
    thresholds = [0, 0.5, 2, 4, 6, 12, 48, float("inf")]
    for h in sorted_s:
        for i, (low, high) in enumerate(zip(thresholds[:-1], thresholds[1:])):
            if low <= h < high:
                n, c = buckets[i]
                buckets[i] = (n, c + 1)
                break
    
    print("  Distribution by hours_left:")
    for name, cnt in buckets:
        bar = "#" * max(1, int(cnt / total * 50))
        print(f"    {name:>10s}: {cnt:5d} ({cnt/total*100:5.1f}%) {bar}")
    
    # Percentiles
    print(f"")
    for p in [5, 10, 20, 50, 80, 90, 95]:
        idx = int(total * p / 100)
        print(f"  P{p:02d}: < {sorted_s[idx]:.1f}h")
    
    print(f"")
    print(f"Target mix: economy={TARGET_PCT['economy']}%, standard={TARGET_PCT['standard']}%, premium={TARGET_PCT['premium']}%")
    p10_idx = max(0, int(total * (TARGET_PCT["economy"] / 100)))
    p90_idx = min(total - 1, int(total * ((100 - TARGET_PCT["premium"]) / 100)))
    print(f"  Economy (flash/air) when hours_left < {sorted_s[p10_idx]:.1f}h")
    print(f"  Standard (glm-4.5)  when hours_left between {sorted_s[p10_idx]:.1f}h and {sorted_s[p90_idx]:.1f}h")
    print(f"  Premium (glm-5.2)   when hours_left > {sorted_s[p90_idx]:.1f}h")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = set(a for a in sys.argv[1:] if a.startswith("--"))
    
    samples = get_hours_left_samples(DB_PATH)
    
    if "--stats" in flags:
        show_stats(samples)
        return
    
    thresholds = compute_percentile_thresholds(samples)
    
    if "--dry-run" in flags:
        print("DRY RUN — thresholds would be:")
        for k, v in thresholds.items():
            print(f"  {k}: {v}")
        return
    
    write_thresholds(thresholds)
    print(f"Thresholds: economy < {thresholds['economy_max_hours']}h, premium > {thresholds['premium_min_hours']}h")
    print(f"(based on {thresholds['samples_used']} samples)")


if __name__ == "__main__":
    main()
