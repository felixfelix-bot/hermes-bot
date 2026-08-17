#!/usr/bin/env python3
"""adaptive_model_tuner — weekly calibration: tier thresholds + pressure FSM bands.

Two outputs (revived S3a, t_12f0a395):

1. LEGACY (compat): model_tier_thresholds.json — hours_left percentile
   thresholds for the 10/80/10 model mix target. The intended consumer
   chain (model_tier_router.py) is currently orphaned (zai_proxy's
   _select_model_tier hook is None), but the file contract is kept intact.

2. NEW: pressure_policy.json — escalate/de-escalate used_pct band
   thresholds for the proxy pressure FSM (pressure_fsm.py, S2b shadow
   mode). Calibrated by percentile analysis of friend-key 5h-window
   used_pct_observed history: AMBER fires on roughly the top 15% of
   observations, RED on the top 5%, with guardrails:

     escalate_amber_pct   clamped to [30, 75]
     escalate_red_pct     clamped to [escalate_amber+10, 95]
     deescalate_amber_pct == escalate_amber_pct    (design symmetry)
     deescalate_green_pct == escalate_amber_pct - 15 (design hysteresis)

   Ordering invariant (must satisfy PressureTracker._policy() range
   safety): deescalate_green < deescalate_amber <= escalate_amber
   < escalate_red.

   The merge-write PRESERVES foreign keys already present in
   pressure_policy.json (e.g. the mode=off kill switch) — the weekly cron
   can never silently re-enable killed routing. Writes are atomic
   (tempfile + os.replace) because the proxy hot-reads this file via an
   mtime+size cache — no restart needed for policy changes. Fewer than
   FSM_MIN_SAMPLES samples -> the policy write is skipped entirely and the
   FSM keeps its compiled defaults.

Reads historic Kalman data for the friend key from zai_usage.db
(kalman_samples: exhausts_in_hours for legacy, used_pct_observed for FSM
bands — both 5-hour window, the real bottleneck).

Usage:
  python3 adaptive_model_tuner.py           # calibrate + write both files
  python3 adaptive_model_tuner.py --stats   # show current distribution only
  python3 adaptive_model_tuner.py --dry-run # show what would change
"""

from __future__ import annotations
import json
import math
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

DB_PATH = Path.home() / ".hermes" / "bot" / "zai_usage.db"
THRESHOLD_FILE = Path.home() / ".hermes" / "bot" / "model_tier_thresholds.json"
PRESSURE_POLICY_FILE = Path.home() / ".hermes" / "bot" / "pressure_policy.json"

# Target mix percentages for the LEGACY tier file (user-specified)
TARGET_PCT = {
    "economy": 10,     # glm-4.5-flash — tightest quota
    "standard": 80,    # glm-5.2 — normal operation
    "premium": 10,     # glm-5.3 — most headroom
}

# Default thresholds (used when no historic data yet)
DEFAULT_THRESHOLDS = {
    "economy_max_hours": 0.5,    # hours_left < 0.5 → economy tier
    "premium_min_hours": 200.0,  # hours_left > 200 → premium tier
    "peak_cap_tier": "air",      # during peak, cap at air
    "updated_at": None,
    "source": "defaults",
}

# ── FSM band calibration constants (S3a) ─────────────────────────────────────
# Share targets: fraction of historic observations that should sit above
# each escalate threshold. AMBER = top 15%, RED = top 5%.
FSM_AMBER_SHARE = 0.15
FSM_RED_SHARE = 0.05
# De-escalation geometry, mirroring the designed defaults in
# pressure_fsm.DEFAULT_POLICY (60/60 amber boundary, 15pp green gap).
FSM_HYSTERESIS_GAP_PP = 15.0
# Guardrails: a pressure FSM must not trip on a healthy-but-nonzero usage
# floor, nor normalize away chronic saturation.
FSM_ESC_AMBER_MIN = 30.0
FSM_ESC_AMBER_MAX = 75.0
FSM_ESC_RED_MAX = 95.0
FSM_ESC_RED_MIN_GAP = 10.0
# Percentiles on fewer samples than this are noise — skip the policy write.
FSM_MIN_SAMPLES = 30


def get_hours_left_samples(db_path: Path, key: str = "friend",
                            window: str = "5-hour") -> list[float]:
    """Get exhausts_in_hours samples from Kalman data for a specific key+window.

    Non-finite values (sqlite stores 9e999 as REAL inf) are dropped so they
    cannot poison percentiles or JSON output. Errors are logged, not silent —
    the weekly cron output must show why calibration data is missing.
    """
    if not db_path.exists():
        return []
    conn = None
    try:
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            """SELECT exhausts_in_hours FROM kalman_samples
               WHERE key = ? AND window = ?
                 AND exhausts_in_hours IS NOT NULL
               ORDER BY ts DESC LIMIT 1000""",
            (key, window)
        ).fetchall()
        return [float(r[0]) for r in rows if math.isfinite(float(r[0]))]
    except Exception as exc:
        print(f"WARN: get_hours_left_samples failed ({exc!r}) — treating as no data",
              file=sys.stderr)
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def get_used_pct_samples(db_path: Path, key: str = "friend",
                          window: str = "5-hour", limit: int = 2000) -> list[float]:
    """Get used_pct_observed samples for the FSM band calibration.

    Same table/filter as get_hours_left_samples but reads the 5h-window
    used_pct column the FSM actually bands on. LIMIT 2000 ≈ 3 weeks at
    the ~15-min Kalman sample cadence — weekly runs stay in recent data.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return []
    conn = None
    try:
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            """SELECT used_pct_observed FROM kalman_samples
               WHERE key = ? AND window = ?
                 AND used_pct_observed IS NOT NULL
               ORDER BY ts DESC LIMIT ?""",
            (key, window, int(limit))
        ).fetchall()
        return [float(r[0]) for r in rows if math.isfinite(float(r[0]))]
    except Exception as exc:
        print(f"WARN: get_used_pct_samples failed ({exc!r}) — treating as no data",
              file=sys.stderr)
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _percentile(sorted_vals: list[float], p: float) -> float:
    """Linear-interpolation percentile of a pre-sorted list. p in [0, 100]."""
    if not sorted_vals:
        raise ValueError("percentile of empty list")
    if p <= 0.0:
        return float(sorted_vals[0])
    if p >= 100.0:
        return float(sorted_vals[-1])
    idx = (p / 100.0) * (len(sorted_vals) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = idx - lo
    return float(sorted_vals[lo]) + frac * (float(sorted_vals[hi]) - float(sorted_vals[lo]))


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def compute_fsm_band_policy(samples: list[float], *,
                            amber_share: float = FSM_AMBER_SHARE,
                            red_share: float = FSM_RED_SHARE,
                            hysteresis_gap_pp: float = FSM_HYSTERESIS_GAP_PP,
                            now: float | None = None) -> dict:
    """Calibrate pressure FSM band thresholds from used_pct history.

    Pure percentile math: escalate_amber = P(100*(1-amber_share)),
    escalate_red = P(100*(1-red_share)), then guardrails (see module
    docstring). Returns {} when there are fewer than FSM_MIN_SAMPLES
    samples — the caller must skip the policy write in that case so the
    FSM keeps its compiled defaults.

    The returned dict contains ONLY the four band keys plus metadata;
    PressureTracker ignores unknown metadata keys.
    """
    if len(samples) < FSM_MIN_SAMPLES:
        return {}

    s = sorted(samples)
    p_amber_raw = _percentile(s, 100.0 * (1.0 - amber_share))
    p_red_raw = _percentile(s, 100.0 * (1.0 - red_share))

    esc_amber = _clamp(p_amber_raw, FSM_ESC_AMBER_MIN, FSM_ESC_AMBER_MAX)
    esc_red = _clamp(p_red_raw, esc_amber + FSM_ESC_RED_MIN_GAP, FSM_ESC_RED_MAX)
    desc_amber = esc_amber
    desc_green = _clamp(esc_amber - hysteresis_gap_pp, 5.0, desc_amber - 5.0)

    return {
        "escalate_amber_pct": round(esc_amber, 1),
        "escalate_red_pct": round(esc_red, 1),
        "deescalate_amber_pct": round(desc_amber, 1),
        "deescalate_green_pct": round(desc_green, 1),
        "source": "adaptive_percentile_fsm",
        "updated_at": now if now is not None else time.time(),
        "samples_used": len(samples),
        "amber_share": amber_share,
        "red_share": red_share,
        "p_amber_used_pct_raw": round(p_amber_raw, 1),
        "p_red_used_pct_raw": round(p_red_raw, 1),
    }


def write_pressure_policy(bands: dict, path: Path | None = None) -> dict:
    """Merge-write band thresholds into pressure_policy.json.

    Foreign keys already in the file (mode, dwell_seconds, ...) are
    PRESERVED — the tuner owns only its band keys + metadata. The write
    is atomic (tempfile + os.replace) because the proxy hot-reads this
    file. Unlike the FSM itself, a failure here is allowed to raise:
    the weekly cron should surface a broken write as a failed run
    instead of silently keeping a stale policy.

    Returns the merged dict that was written.
    """
    path = Path(path) if path else PRESSURE_POLICY_FILE
    existing: dict = {}
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            existing = data
    except Exception:
        existing = {}  # missing or corrupt -> start from bands only
    merged = {**existing, **bands}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".pressure_policy_",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(merged, indent=2))
            fh.flush()
            os.fsync(fh.fileno())  # survive power-loss mid-write (cold review)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise
    return merged


def compute_percentile_thresholds(samples: list[float]) -> dict:
    """Compute hours_left thresholds from percentiles to hit target mix.

    LEGACY output (model_tier_thresholds.json) — contract unchanged.
    """
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


def write_thresholds(thresholds: dict, path: Path | None = None):
    """Write thresholds to JSON file (legacy contract for model_tier_router)."""
    path = Path(path) if path else THRESHOLD_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(thresholds, indent=2))
    print(f"Wrote thresholds to {path}")


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
    print(f"  Standard (glm-5.2)  when hours_left between {sorted_s[p10_idx]:.1f}h and {sorted_s[p90_idx]:.1f}h")
    print(f"  Premium (glm-5.3)   when hours_left > {sorted_s[p90_idx]:.1f}h")


def show_fsm_stats(used: list[float]):
    """Print FSM band calibration preview for used_pct samples."""
    if len(used) < FSM_MIN_SAMPLES:
        print(f"FSM bands: only {len(used)} used_pct samples (<{FSM_MIN_SAMPLES}) "
              f"— policy write would be SKIPPED, FSM keeps defaults.")
        return
    pol = compute_fsm_band_policy(used)
    print(f"FSM bands from {len(used)} used_pct samples (friend 5h window):")
    print(f"  escalate:   AMBER >= {pol['escalate_amber_pct']}%, RED >= {pol['escalate_red_pct']}%")
    print(f"  deescalate: RED->AMBER <= {pol['deescalate_amber_pct']}%, AMBER->GREEN <= {pol['deescalate_green_pct']}%")
    print(f"  raw percentiles: amber P{int(100*(1-FSM_AMBER_SHARE))}={pol['p_amber_used_pct_raw']}%, "
          f"red P{int(100*(1-FSM_RED_SHARE))}={pol['p_red_used_pct_raw']}%")


def run(db_path: Path | None = None, threshold_path: Path | None = None,
        policy_path: Path | None = None, *, dry_run: bool = False,
        stats: bool = False) -> dict:
    """One calibration pass. Returns {'legacy_written': bool, 'policy_written': bool}."""
    db_path = Path(db_path) if db_path else DB_PATH
    threshold_path = Path(threshold_path) if threshold_path else THRESHOLD_FILE
    policy_path = Path(policy_path) if policy_path else PRESSURE_POLICY_FILE

    exhaust = get_hours_left_samples(db_path)
    used = get_used_pct_samples(db_path)

    if stats:
        show_stats(exhaust)
        print("")
        show_fsm_stats(used)
        return {"legacy_written": False, "policy_written": False}

    thresholds = compute_percentile_thresholds(exhaust)
    bands = compute_fsm_band_policy(used)

    if dry_run:
        print("DRY RUN — legacy thresholds would be:")
        for k, v in thresholds.items():
            print(f"  {k}: {v}")
        print("DRY RUN — FSM pressure policy bands would be:")
        if bands:
            for k, v in bands.items():
                print(f"  {k}: {v}")
        else:
            print(f"  SKIPPED ({len(used)} used_pct samples < {FSM_MIN_SAMPLES})")
        return {"legacy_written": False, "policy_written": False}

    write_thresholds(thresholds, path=threshold_path)
    summary = {"legacy_written": True, "policy_written": False}
    print(f"Thresholds: economy < {thresholds['economy_max_hours']}h, premium > {thresholds['premium_min_hours']}h")
    print(f"(based on {thresholds.get('samples_used', 0)} samples)")

    if bands:
        merged = write_pressure_policy(bands, path=policy_path)
        summary["policy_written"] = True
        print(f"Wrote FSM bands to {policy_path}: "
              f"AMBER>={merged['escalate_amber_pct']}% RED>={merged['escalate_red_pct']}% "
              f"(de-esc {merged['deescalate_green_pct']}/{merged['deescalate_amber_pct']}%)")
        print(f"(based on {bands['samples_used']} used_pct samples; foreign keys preserved)")
    else:
        print(f"Skipped FSM policy write ({len(used)} used_pct samples < {FSM_MIN_SAMPLES}) "
              f"— pressure_fsm keeps compiled defaults")
    return summary


def main():
    flags = set(a for a in sys.argv[1:] if a.startswith("--"))
    run(stats=("--stats" in flags), dry_run=("--dry-run" in flags))


if __name__ == "__main__":
    main()
