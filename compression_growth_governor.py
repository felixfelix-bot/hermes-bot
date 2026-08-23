#!/usr/bin/env python3
"""Context-growth-rate Kalman governor for adaptive compaction threshold.

Tracks the median context growth rate (tokens/call) from zai_usage.db
using a 1-D Kalman filter. Adjusts compression.threshold via
``hermes config set`` based on whether sessions are dense (high growth →
lower threshold → compact sooner) or sparse (low growth → raise threshold
→ preserve context longer).

Runs AFTER compression_cost_governor.py (chained in same cron slot).

State:  ~/.hermes/bot/compression_growth_state.json
Output: ``hermes config set compression.threshold <value>``
Audit:  ~/.hermes/bot/compression_growth_override.json

Fallback: On any failure, leaves config.yaml unchanged.

CRITICAL: context_length is read dynamically from config.yaml — NOT hardcoded.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BOT_DIR = Path.home() / ".hermes" / "bot"
DB_PATH = BOT_DIR / "zai_usage.db"
STATE_FILE = BOT_DIR / "compression_growth_state.json"
AUDIT_FILE = BOT_DIR / "compression_growth_override.json"
CONFIG_PATH = Path.home() / ".hermes" / "profiles" / "manager" / "config.yaml"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
WINDOW_HOURS = 6           # Shorter window — growth rate is a recent signal
C0 = 17500                 # Manager fixed prefix (tokens)
MINIMUM_CONTEXT_LENGTH = 64000  # Hard floor in context_compressor

# Safety bounds (MIN_THRESHOLD is dynamic: MINIMUM_CONTEXT_LENGTH / context_length)
MAX_THRESHOLD = 0.70
FALLBACK_THRESHOLD = 0.40
HYSTERESIS = 0.02          # Only change config if delta > this

# Growth-rate bounds
G_BASELINE = 1800          # Measured average (tokens/call)
G_MIN = 200                # Floor for growth estimate
G_MAX = 20000              # Ceiling for growth estimate

# Control-law sensitivity
# threshold = FALLBACK_THRESHOLD + K * (G_BASELINE - g_estimate)
# At g=10000 (dense):  delta = 0.00004 * (-8200) = -0.328 → clamped to MIN_THRESHOLD
# At g=5000:          delta = 0.00004 * (-3200) = -0.128
# At g=1800 (normal): delta = 0 (baseline)
# At g=681 (sparse):  delta = 0.00004 * (1119)  = +0.045
# At g=200:           delta = 0.00004 * (1600)  = +0.064 → clamped to ≤ MAX
K_SENSITIVITY = 0.00004


# ---------------------------------------------------------------------------
# GrowthRateKalman
# ---------------------------------------------------------------------------

class GrowthRateKalman:
    """1-D Kalman filter on context growth rate (tokens/call).

    Initial state: x=1800, p=500000.0, q=50000.0, r=300000.0
    """

    def __init__(self, initial_g: float = G_BASELINE):
        self.x = initial_g          # State estimate
        self.p = 500000.0           # Estimate uncertainty
        self.q = 50000.0            # Process noise
        self.r = 300000.0           # Measurement noise
        self.n = 0                  # Update count

    def predict(self) -> float:
        """Prediction step (random walk): uncertainty grows by Q.

        State does not change (random walk model has F=1).
        """
        self.p = self.p + self.q
        return self.x

    def update(self, measurement: float) -> float:
        """Correction step: incorporate measurement into state estimate."""
        self.n += 1
        k = self.p / (self.p + self.r)
        self.x = self.x + k * (measurement - self.x)
        self.x = max(G_MIN, min(G_MAX, self.x))  # Clamp to bounds
        self.p = (1 - k) * self.p
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


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def read_config(config_path: Path = CONFIG_PATH) -> tuple[int, float]:
    """Read context_length and current compression.threshold from config.yaml.

    Returns (context_length, current_threshold).
    Falls back to (131072, FALLBACK_THRESHOLD) when config is missing
    or malformed.
    """
    try:
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        context_length = cfg.get("model", {}).get("context_length", 131072)
        current_threshold = cfg.get("compression", {}).get("threshold", FALLBACK_THRESHOLD)
        return int(context_length), float(current_threshold)
    except Exception:
        return 131072, FALLBACK_THRESHOLD


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

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
        "threshold_history": [],
    }


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------

def measure_growth_rate(db_path: Path = DB_PATH, hours: int = WINDOW_HOURS) -> float:
    """Measure median positive context growth per call in recent sessions.

    Queries the ``api_calls`` table in *zai_usage.db*, computes per-session
    positive deltas (excluding post-compression resets and ``task_type``
    rows), and returns the median across all sessions.

    Falls back to **G_BASELINE** on any error or insufficient data.
    """
    if not db_path.exists():
        return G_BASELINE

    cutoff = time.time() - hours * 3600
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                """
                SELECT session_id, prompt_tokens, ts
                FROM api_calls
                WHERE ts >= ? AND status_code = 200
                  AND session_id IS NOT NULL
                  AND task_type IS NULL
                  AND prompt_tokens IS NOT NULL
                ORDER BY session_id, ts
                """,
                (cutoff,),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return G_BASELINE

    if len(rows) < 10:
        return G_BASELINE

    # Compute per-session positive deltas
    deltas: list[float] = []
    current_sid: str | None = None
    prev_tokens: int | None = None

    for sid, pt, _ts in rows:
        if sid != current_sid:
            current_sid = sid
            prev_tokens = pt
            continue
        if pt is not None and prev_tokens is not None and pt > prev_tokens:
            deltas.append(float(pt - prev_tokens))
        prev_tokens = pt

    if not deltas:
        return G_BASELINE

    # Median (robust to outliers — tool outputs can spike 40K+)
    deltas.sort()
    median = deltas[len(deltas) // 2]
    return float(median)


# ---------------------------------------------------------------------------
# Control law
# ---------------------------------------------------------------------------

def compute_threshold(growth_rate: float, context_length: int) -> float:
    """Compute optimal threshold from growth-rate estimate and context length.

    Control law::

        threshold = FALLBACK_THRESHOLD + K × (G_BASELINE − g_estimate)

    Dense sessions (high *g*) → lower threshold → compact sooner.
    Sparse sessions (low *g*) → raise threshold → preserve context.

    Result is clamped to ``[MIN_THRESHOLD, MAX_THRESHOLD]`` where
    ``MIN_THRESHOLD = MINIMUM_CONTEXT_LENGTH / context_length``.
    """
    min_threshold = MINIMUM_CONTEXT_LENGTH / context_length
    delta = K_SENSITIVITY * (G_BASELINE - growth_rate)
    new_threshold = FALLBACK_THRESHOLD + delta
    return max(min_threshold, min(MAX_THRESHOLD, new_threshold))


# ---------------------------------------------------------------------------
# Apply threshold
# ---------------------------------------------------------------------------

def _write_audit(
    new_threshold: float,
    old_threshold: float,
    context_length: int,
    applied: bool,
    growth_rate: float = 0.0,
    kalman_estimate: float = 0.0,
):
    """Write audit data to the override file."""
    audit = {
        "threshold": round(new_threshold, 4),
        "old_threshold": round(old_threshold, 4),
        "applied": applied,
        "context_length": context_length,
        "growth_rate": round(growth_rate, 1),
        "kalman_estimate": round(kalman_estimate, 1),
        "min_threshold": round(MINIMUM_CONTEXT_LENGTH / context_length, 4),
        "max_threshold": MAX_THRESHOLD,
        "k_sensitivity": K_SENSITIVITY,
        "updated_at": time.time(),
    }
    try:
        AUDIT_FILE.write_text(json.dumps(audit, indent=2))
    except Exception as e:
        print(f"[growth-governor] audit write failed: {e}", file=sys.stderr)


def apply_threshold(new_threshold: float, config_path: Path = CONFIG_PATH) -> bool:
    """Apply threshold via ``hermes config set`` if change exceeds hysteresis.

    Reads current threshold and context_length dynamically from *config_path*.

    Only applies if ``|new_threshold − current| > HYSTERESIS``.

    Returns **True** if config was updated, **False** otherwise.
    """
    context_length, current_threshold = read_config(config_path)

    # Write audit regardless (captures decision rationale)
    _write_audit(new_threshold, current_threshold, context_length,
                 False)  # will overwrite below if applied

    if abs(new_threshold - current_threshold) < HYSTERESIS:
        return False  # Within hysteresis band — no change

    try:
        result = subprocess.run(
            [
                "hermes", "--profile", "manager", "config", "set",
                "compression.threshold", f"{new_threshold:.4f}",
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            _write_audit(new_threshold, current_threshold, context_length, True)
            return True
        print(f"[growth-governor] hermes config set failed: {result.stderr}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[growth-governor] config set exception: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Main entry point (cron)
# ---------------------------------------------------------------------------

def main():
    """Entry point for cron.  Zero LLM cost — pure computation."""
    state = load_state()
    kf = GrowthRateKalman.from_dict(state.get("kalman", {}))

    # Read context_length dynamically from config.yaml
    context_length, old_threshold = read_config()

    # Measure current growth rate
    measured_g = measure_growth_rate(DB_PATH)

    # Skip Kalman update when DB is missing (fake G_BASELINE measurement)
    if measured_g == G_BASELINE and not DB_PATH.exists():
        g_estimate = kf.x  # Keep last estimate, don't inject fake measurement
    else:
        kf.predict()
        g_estimate = kf.update(measured_g)

    # Compute optimal threshold
    new_threshold = compute_threshold(g_estimate, context_length)

    # Apply with hysteresis (reads config again inside)
    applied = apply_threshold(new_threshold)

    # Update persisted state
    history: list = state.get("threshold_history", [])
    history.append({
        "ts": time.time(),
        "old_threshold": round(old_threshold, 4),
        "new_threshold": round(new_threshold, 4),
        "applied": applied,
    })
    history = history[-100:]  # Keep last 100 entries

    state["kalman"] = kf.to_dict()
    state["last_measurement"] = round(measured_g, 1)
    state["last_ts"] = time.time()
    state["current_threshold"] = new_threshold if applied else old_threshold
    state["threshold_history"] = history
    state["context_length"] = context_length

    try:
        save_state(state)
    except Exception as e:
        print(f"[growth-governor] save_state failed: {e}", file=sys.stderr)

    # Print JSON summary (consumed by cron / health scripts)
    summary = {
        "growth_rate": round(measured_g, 1),
        "kalman_estimate": round(g_estimate, 1),
        "old_threshold": round(old_threshold, 4),
        "new_threshold": round(new_threshold, 4),
        "context_length": context_length,
        "applied": applied,
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
