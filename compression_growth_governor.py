#!/usr/bin/env python3
"""Context-growth-rate Kalman governor for adaptive compaction threshold.

Tracks the average context growth rate (tokens/call) from zai_usage.db
using a 1-D Kalman filter. Adjusts compression.threshold via
`hermes config set` based on whether sessions are dense (high growth ->
lower threshold -> compact sooner) or sparse (low growth -> raise threshold
-> preserve context longer).

Runs AFTER compression_cost_governor.py (chained in same cron slot).

State: ~/.hermes/bot/compression_growth_state.json
Output: hermes config set compression.threshold <value>
Audit: ~/.hermes/bot/compression_growth_override.json

Fallback: On any failure, leaves config.yaml unchanged.

CONSTANTS CORRECTED for 131072 context length (glm-5.2):
  - MIN_THRESHOLD = 64000/131072 = 0.4883
  - MAX_THRESHOLD = 0.70
  - FALLBACK_THRESHOLD = 0.60
  - G_BASELINE = 1800
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from pathlib import Path

BOT_DIR = Path.home() / ".hermes" / "bot"
DB_PATH = BOT_DIR / "zai_usage.db"
STATE_FILE = BOT_DIR / "compression_growth_state.json"
AUDIT_FILE = BOT_DIR / "compression_growth_override.json"
CONFIG_PATH = Path.home() / ".hermes" / "profiles" / "manager" / "config.yaml"

# --- Constants ---
WINDOW_HOURS = 6           # Shorter window — growth rate is more recent signal
C0 = 17500                 # Manager fixed prefix (tokens)

# CRITICAL: context_length=131072 (NOT 202752 — design doc was wrong)
CONTEXT_LENGTH = 131072

# Safety bounds (corrected for 131K context)
MIN_THRESHOLD = 64000 / 131072   # = 0.4883 — the MINIMUM_CONTEXT_LENGTH floor
MAX_THRESHOLD = 0.70             # Don't let context grow past 70% of window
FALLBACK_THRESHOLD = 0.60        # If anything goes wrong, stay at current baseline
HYSTERESIS = 0.02                # Only change config if delta > this

# Growth rate bounds
G_BASELINE = 1800          # Measured average (tokens/call)
G_MIN = 200                # Floor for growth estimate
G_MAX = 20000              # Ceiling for growth estimate

# Control law sensitivity — recalculated for 131K context.
#
# threshold = base + K * (g_baseline - g_estimate), clamped to [MIN, MAX]
#
# Design doc used K=0.00004 for 202752 context. For 131072 the available
# threshold range is [0.488, 0.70] = 0.212 wide. We want:
#   - g=10000 (dense):  delta = K * (1800-10000) = K * (-8200) should reach ~MIN
#   - g=200 (sparse):   delta = K * (1800-200)   = K * (1600)  should reach ~MAX
#
# For dense: need K * 8200 >= (0.60 - 0.488) = 0.112 → K >= 0.112/8200 = 0.0000137
# For sparse: need K * 1600 <= (0.70 - 0.60) = 0.10  → K <= 0.10/1600 = 0.0000625
#
# Pick K = 0.00003 (midpoint, gives good spread on both sides):
#   - g=10000: delta = 0.00003 * (-8200) = -0.246 → clamped at MIN (0.488)
#   - g=5000:  delta = 0.00003 * (-3200) = -0.096 → threshold = 0.504
#   - g=1800:  delta = 0 (baseline)
#   - g=681:   delta = 0.00003 * (1119)  = +0.034  → threshold = 0.634
#   - g=200:   delta = 0.00003 * (1600)  = +0.048  → threshold = 0.648
K_SENSITIVITY = 0.00003


class GrowthRateKalman:
    """1-D Kalman filter on context growth rate (tokens/call)."""

    def __init__(self, initial_g: float = G_BASELINE):
        self.x = initial_g        # State estimate
        self.p = 500000.0         # Estimate uncertainty
        self.q = 50000.0          # Process noise
        self.r = 300000.0         # Measurement noise
        self.n = 0                # Update count

    def update(self, measurement: float) -> float:
        self.n += 1
        k = self.p / (self.p + self.r)
        self.x = self.x + k * (measurement - self.x)
        self.x = max(G_MIN, min(G_MAX, self.x))  # Clamp to bounds
        self.p = (1 - k) * self.p + self.q
        return self.x

    def to_dict(self):
        return {"x": self.x, "p": self.p, "q": self.q, "r": self.r, "n": self.n}

    @classmethod
    def from_dict(cls, d):
        kf = cls(d.get("x", G_BASELINE))
        kf.p = d.get("p", 500000.0)
        kf.q = d.get("q", 50000.0)
        kf.r = d.get("r", 300000.0)
        kf.n = d.get("n", 0)
        return kf


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {
        "kalman": GrowthRateKalman().to_dict(),
        "last_measurement": 0.0,
        "last_ts": 0,
        "current_threshold": FALLBACK_THRESHOLD,
        "last_config_threshold": FALLBACK_THRESHOLD,
    }


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def measure_growth_rate(db_path: Path, hours: int = WINDOW_HOURS) -> float:
    """Measure median positive context growth per call in recent sessions.

    Queries the api_calls table in zai_usage.db, computes per-session
    positive deltas (excluding post-compression resets and compression
    task_type rows), and returns the median across all sessions.

    Falls back to G_BASELINE on any error or insufficient data.
    """
    if not db_path.exists():
        return G_BASELINE  # Fallback

    cutoff = time.time() - hours * 3600
    try:
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("""
            SELECT session_id, prompt_tokens, ts
            FROM api_calls
            WHERE ts >= ? AND status_code = 200
              AND session_id IS NOT NULL
              AND task_type IS NULL
              AND prompt_tokens IS NOT NULL
            ORDER BY session_id, ts
        """, (cutoff,)).fetchall()
        conn.close()
    except Exception:
        return G_BASELINE

    if len(rows) < 10:
        return G_BASELINE

    # Compute per-session growth, then collect all positive deltas
    deltas = []
    current_sid = None
    prev_tokens = None

    try:
        for sid, pt, ts in rows:
            if sid != current_sid:
                current_sid = sid
                prev_tokens = pt
                continue
            if pt is not None and prev_tokens is not None and pt > prev_tokens:
                deltas.append(pt - prev_tokens)
            prev_tokens = pt
    except Exception:
        return G_BASELINE

    if not deltas:
        return G_BASELINE

    # Use median (robust to outliers — tool outputs can spike 40K+)
    deltas.sort()
    median = deltas[len(deltas) // 2]
    return float(median)


def compute_threshold(g_estimate: float, base_threshold: float = FALLBACK_THRESHOLD) -> float:
    """Compute absolute threshold from growth rate estimate.

    Control law (absolute, NOT incremental): dense sessions (high g) -> lower
    threshold -> compact sooner.  Sparse sessions (low g) -> raise threshold
    -> preserve context longer.

    threshold = base + K * (g_baseline - g_estimate), clamped to [MIN, MAX]

    This is an absolute law: the same g always produces the same threshold
    regardless of how many times the governor has run.  The hysteresis in
    apply_threshold prevents churn when g is near baseline.
    """
    delta = K_SENSITIVITY * (G_BASELINE - g_estimate)
    new_threshold = base_threshold + delta
    return max(MIN_THRESHOLD, min(MAX_THRESHOLD, new_threshold))


def apply_threshold(threshold: float, old_threshold: float) -> bool:
    """Apply threshold via `hermes config set` if change exceeds hysteresis.

    Returns True if config was updated, False if skipped or failed.
    On any failure, config is left unchanged — backward-compatible.
    """
    if abs(threshold - old_threshold) < HYSTERESIS:
        return False  # No change needed

    try:
        result = subprocess.run(
            ["hermes", "--profile", "manager", "config", "set",
             "compression.threshold", f"{threshold:.4f}"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            return True
        else:
            print(f"[growth-governor] hermes config set failed: {result.stderr}")
            return False
    except Exception as e:
        print(f"[growth-governor] config set exception: {e}")
        return False


def main():
    state = load_state()
    kf = GrowthRateKalman.from_dict(state.get("kalman", {}))

    # Measure current growth rate
    measured_g = measure_growth_rate(DB_PATH)

    # Skip Kalman update when DB fallback returns G_BASELINE (fake measurement)
    if measured_g == G_BASELINE and not DB_PATH.exists():
        g_estimate = kf.x  # Keep last estimate, don't update
    else:
        g_estimate = kf.update(measured_g)

    # Absolute control law: compute from FALLBACK_THRESHOLD as base
    # (NOT incremental — same g always produces same threshold)
    new_threshold = compute_threshold(g_estimate, FALLBACK_THRESHOLD)

    # Compare against last-applied threshold for hysteresis
    current_threshold = state.get("current_threshold", FALLBACK_THRESHOLD)

    # Apply with hysteresis
    applied = apply_threshold(new_threshold, current_threshold)
    if applied:
        state["last_config_threshold"] = current_threshold
        state["current_threshold"] = new_threshold
    else:
        state["current_threshold"] = current_threshold

    # Save state
    state["kalman"] = kf.to_dict()
    state["last_measurement"] = measured_g
    state["last_ts"] = time.time()
    try:
        save_state(state)
    except Exception as e:
        print(f"[growth-governor] save_state failed: {e}")

    # Write audit file
    audit = {
        "growth_estimate": round(g_estimate, 1),
        "growth_measured": round(measured_g, 1),
        "growth_baseline": G_BASELINE,
        "target_threshold": round(new_threshold, 4),
        "current_threshold": round(state["current_threshold"], 4),
        "applied": applied,
        "implied_turns_to_compaction": int(
            (state["current_threshold"] * CONTEXT_LENGTH - C0) / max(g_estimate, 1)
        ),
        "context_length": CONTEXT_LENGTH,
        "min_threshold": round(MIN_THRESHOLD, 4),
        "max_threshold": MAX_THRESHOLD,
        "k_sensitivity": K_SENSITIVITY,
        "updated_at": time.time(),
    }
    try:
        AUDIT_FILE.write_text(json.dumps(audit, indent=2))
    except Exception as e:
        print(f"[growth-governor] audit write failed: {e}")

    tag = "ADJUSTED" if applied else "stable"
    print(f"[growth-governor] g={measured_g:.0f} est={g_estimate:.0f} "
          f"threshold={state['current_threshold']:.4f} "
          f"turns_to_compact={audit['implied_turns_to_compaction']} "
          f"[{tag}] n={kf.n}")


if __name__ == "__main__":
    main()