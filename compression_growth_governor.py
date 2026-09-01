#!/usr/bin/env python3
"""Context-growth-rate Kalman governor for adaptive compaction threshold.

Tracks the median context growth rate (tokens/call) from zai_usage.db
using a 1-D Kalman filter. Adjusts compression.threshold via
``hermes config set`` based on whether sessions are dense (high growth →
lower threshold → compact sooner) or sparse (low growth → raise threshold
→ preserve context longer), plus a bounded price-aware nudge from the
realized $/M of recent traffic (expensive lanes compact sooner).

2026-09-02 convergence fix: the target threshold is now computed from the
BASE (FALLBACK_THRESHOLD), not the current value — the old form was an
unbounded integrator that ratcheted every profile to MAX_THRESHOLD.

**Multi-profile**: iterates over ALL profiles in ~/.hermes/profiles/*/
and applies the same growth-rate-based threshold to each. The growth
rate is measured globally (zai_usage.db does not track per-profile
sessions); the threshold computation is per-profile because each profile
can have a different context_length.

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
PROFILES_DIR = Path.home() / ".hermes" / "profiles"
# Backward compat: tests and old callers reference CONFIG_PATH for manager
CONFIG_PATH = PROFILES_DIR / "manager" / "config.yaml"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
WINDOW_HOURS = 6           # Shorter window — growth rate is a recent signal
C0 = 17500                 # Manager fixed prefix (tokens)
MINIMUM_CONTEXT_LENGTH = 64000  # Hard floor in context_compressor

# Safety bounds (MIN_THRESHOLD is dynamic: MINIMUM_CONTEXT_LENGTH / context_length)
MAX_THRESHOLD = 0.70
# Calibrated 2026-09-02 from 0.60 → 0.45: the 0.60 base was tuned for the
# 200k-ctx profile ecosystem; at 1M-ctx manager sessions a 0.60-0.70 fill is
# the degeneration-risk zone (zombie tool-loops ballooned sessions to 12M
# tokens there on 2026-09-01/02). Post RC-1 (reasoning-injection fix) the
# thrash drivers are gone, so a mid-range base is safe and preserves context.
FALLBACK_THRESHOLD = 0.45
HYSTERESIS = 0.02          # Only change config if delta > this

# Deliberate overrides: profiles the governor must NOT touch (hand-tuned
# thresholds for specific lanes). 2026-09-02: kimi-consultant runs at a
# deliberate 0.15 (aggressive compaction, cheap bursty lane) that the old
# ratcheting governor kept stomping upward.
EXEMPT_PROFILES: frozenset = frozenset({"kimi-consultant"})

# ── Price-aware nudging (2026-09-02, plan C1) ────────────────────────────────
# Compaction decisions should care about the REALIZED $/M of the traffic at
# stake: expensive lanes make big contexts costly to re-prefill every call
# (compact sooner); cheap lanes make context preservation nearly free (compact
# later). A bounded, log-scaled nudge computed from recent provenance.
PRICE_NUDGE_MAX = 0.10            # max threshold adjustment (positive = later compaction)
PRICE_REF_USD_PER_M = 0.02        # reference realized price ($/M)
PRICE_SATURATION_RATIO = 8.0      # price/ref ratio where the nudge saturates
PRICE_MIN_TOKENS = 1_000_000      # need ≥1M tokens in window for a measurement

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
# Profile discovery
# ---------------------------------------------------------------------------

def discover_profiles(profiles_dir: Path | None = None) -> list[str]:
    """Return all profile directory names that have a ``config.yaml``.

    Scans *profiles_dir* (default: ``~/.hermes/profiles``) and returns
    sorted names of subdirectories that contain ``config.yaml``.
    Returns an empty list if the directory doesn't exist or is empty.
    """
    if profiles_dir is None:
        profiles_dir = PROFILES_DIR
    if not profiles_dir or not profiles_dir.is_dir():
        return []
    return sorted(
        entry.name
        for entry in profiles_dir.iterdir()
        if entry.is_dir() and (entry / "config.yaml").exists()
    )


def profile_config_path(profile_name: str) -> Path:
    """Return the ``config.yaml`` path for a given profile name."""
    return PROFILES_DIR / profile_name / "config.yaml"


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

    Note: The DB does not track per-profile sessions, so this returns a
    **global** growth rate applied to all profiles.

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

def compute_threshold(growth_rate: float, context_length: int,
                      current_threshold: float = None) -> float:
    """Compute optimal threshold from growth-rate estimate and context length.

    Control law (composes with cost governor)::

        threshold = FALLBACK + K × (G_BASELINE − g_estimate) − price_nudge

    CONVERGENT (2026-09-02 fix): the target is computed from the BASE, NOT
    from the current threshold. The original ``base = current_threshold``
    made this an unbounded integrator (ratchet) — with a sparse-growth
    estimate every run added the same +delta, drifting all 75 profiles to
    MAX_THRESHOLD (0.7) within hours. The target now depends only on the
    Kalman estimate (and the price nudge); ``current_threshold`` is retained
    in the signature for compatibility but is ignored here — hysteresis in
    :func:`apply_threshold` guards write churn.

    Dense sessions (high *g*) → lower threshold → compact sooner.
    Sparse sessions (low *g*) → raise threshold → preserve context.
    Expensive lanes (high realized $/M) → compact sooner (price nudge).

    Result is clamped to ``[MIN_THRESHOLD, MAX_THRESHOLD]`` where
    ``MIN_THRESHOLD = MINIMUM_CONTEXT_LENGTH / context_length``.
    """
    min_threshold = MINIMUM_CONTEXT_LENGTH / context_length
    delta = K_SENSITIVITY * (G_BASELINE - growth_rate)
    new_threshold = FALLBACK_THRESHOLD + delta + _price_nudge()
    return max(min_threshold, min(MAX_THRESHOLD, new_threshold))


def _realized_price_per_m(db_path: Path = DB_PATH, hours: int = WINDOW_HOURS) -> float | None:
    """Measure realized $/M across recent successful traffic (all providers).

    Returns None when there is insufficient data (< PRICE_MIN_TOKENS),
    so the price nudge degrades to neutral (0.0) rather than guessing.
    """
    if not db_path.exists():
        return None
    try:
        cutoff = time.time() - hours * 3600
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT SUM(cost_usd), SUM(total_tokens) FROM api_calls "
            "WHERE ts >= ? AND status_code = 200 "
            "AND cost_usd IS NOT NULL AND total_tokens > 0",
            (cutoff,)).fetchone()
        conn.close()
        cost = float(row[0] or 0.0)
        tokens = int(row[1] or 0)
        if tokens < PRICE_MIN_TOKENS or cost <= 0:
            return None
        return cost / (tokens / 1_000_000)
    except Exception:
        return None


def _price_nudge(price: float | None = None) -> float:
    """Signed threshold adjustment from realized price (added to the target).

    NEGATIVE = compact sooner (expensive traffic — costly to re-prefill).
    POSITIVE = preserve context longer (cheap traffic). Saturates at
    ±PRICE_NUDGE_MAX when price deviates from PRICE_REF_USD_PER_M by
    PRICE_SATURATION_RATIO× in either direction. Neutral (0.0) when the
    price is unknown.
    """
    if price is None or price <= 0:
        return 0.0
    try:
        ratio = price / PRICE_REF_USD_PER_M
        if ratio <= 0:
            return 0.0
        import math
        scaled = math.log(ratio) / math.log(PRICE_SATURATION_RATIO)
    except (ValueError, ZeroDivisionError):
        return 0.0
    scaled = max(-1.0, min(1.0, scaled))
    return -PRICE_NUDGE_MAX * scaled


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
    profile: str = "manager",
):
    """Write audit data to the override file."""
    audit = {
        "profile": profile,
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


def apply_threshold(
    new_threshold: float,
    config_path: Path = CONFIG_PATH,
    profile_name: str = "manager",
    growth_rate: float = 0.0,
    kalman_estimate: float = 0.0,
) -> bool:
    """Apply threshold via ``hermes config set`` if change exceeds hysteresis.

    Reads current threshold and context_length dynamically from *config_path*.

    Only applies if ``|new_threshold − current| > HYSTERESIS``.

    Returns **True** if config was updated, **False** otherwise.
    """
    context_length, current_threshold = read_config(config_path)

    # Write audit regardless (captures decision rationale)
    _write_audit(new_threshold, current_threshold, context_length,
                 False, growth_rate=growth_rate,
                 kalman_estimate=kalman_estimate, profile=profile_name)

    if abs(new_threshold - current_threshold) < HYSTERESIS:
        return False  # Within hysteresis band — no change

    try:
        result = subprocess.run(
            [
                "hermes", "--profile", profile_name, "config", "set",
                "compression.threshold", f"{new_threshold:.4f}",
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            _write_audit(new_threshold, current_threshold, context_length, True,
                         growth_rate=growth_rate,
                         kalman_estimate=kalman_estimate, profile=profile_name)
            return True
        print(f"[growth-governor] hermes config set failed for {profile_name}: {result.stderr}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[growth-governor] config set exception for {profile_name}: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Main entry point (cron)
# ---------------------------------------------------------------------------

def main(profiles: list[str] | None = None):
    """Entry point for cron.  Zero LLM cost — pure computation.

    Iterates over ALL profiles in ~/.hermes/profiles/ and applies
    the growth-rate-based threshold to each. Growth rate is measured
    globally (the DB does not track per-profile sessions); the threshold
    computation is per-profile because each profile can have a different
    context_length.
    """
    if profiles is None:
        profiles = discover_profiles()
    if not profiles:
        profiles = ["manager"]

    state = load_state()
    kf = GrowthRateKalman.from_dict(state.get("kalman", {}))

    # Measure global growth rate (DB doesn't track per-profile)
    measured_g = measure_growth_rate(DB_PATH)
    # Measure realized $/M for the price-aware nudge (plan C1, 2026-09-02)
    measured_price = _realized_price_per_m(DB_PATH)

    # Skip Kalman update when DB is missing (fake G_BASELINE measurement)
    if measured_g == G_BASELINE and not DB_PATH.exists():
        g_estimate = kf.x  # Keep last estimate, don't inject fake measurement
    else:
        kf.predict()
        g_estimate = kf.update(measured_g)

    # Iterate over all profiles
    profile_results: list[dict] = []
    for profile_name in profiles:
        config_path = profile_config_path(profile_name)
        if not config_path.exists():
            print(f"[growth-governor] skipping {profile_name}: no config.yaml",
                  file=sys.stderr)
            profile_results.append({
                "profile": profile_name,
                "skipped": True,
                "reason": "no config.yaml",
            })
            continue

        context_length, old_threshold = read_config(config_path)
        if profile_name in EXEMPT_PROFILES:
            profile_results.append({
                "profile": profile_name,
                "skipped": True,
                "reason": "exempt (deliberate threshold)",
                "threshold": old_threshold,
            })
            continue
        new_threshold = compute_threshold(g_estimate, context_length, old_threshold)
        applied = apply_threshold(
            new_threshold, config_path, profile_name,
            growth_rate=measured_g, kalman_estimate=g_estimate,
        )

        if applied:
            print(f"[growth-governor] updated {profile_name}: "
                  f"threshold {old_threshold:.4f} → {new_threshold:.4f}",
                  file=sys.stderr)

        profile_results.append({
            "profile": profile_name,
            "old_threshold": round(old_threshold, 4),
            "new_threshold": round(new_threshold, 4),
            "context_length": context_length,
            "applied": applied,
        })

    # Update persisted state
    history: list = state.get("threshold_history", [])
    last_governed = next(
        (r for r in reversed(profile_results) if "new_threshold" in r), None)
    history.append({
        "ts": time.time(),
        "new_threshold": round(last_governed["new_threshold"], 4)
            if last_governed and "new_threshold" in last_governed
            else None,
        "applied": any(r.get("applied") for r in profile_results),
    })
    history = history[-100:]  # Keep last 100 entries

    state["kalman"] = kf.to_dict()
    state["last_measurement"] = round(measured_g, 1)
    state["last_price_per_m"] = round(measured_price, 4) if measured_price else None
    state["last_ts"] = time.time()
    state["profiles_processed"] = len(profile_results)
    state["threshold_history"] = history

    try:
        save_state(state)
    except Exception as e:
        print(f"[growth-governor] save_state failed: {e}", file=sys.stderr)

    # Print JSON summary (consumed by cron / health scripts).
    # Also returned so programmatic callers (tests, watchdogs) can use it.
    summary = {
        "growth_rate": round(measured_g, 1),
        "kalman_estimate": round(g_estimate, 1),
        "price_per_m": round(measured_price, 4) if measured_price else None,
        "profiles_processed": len(profiles),
        "profile_results": profile_results,
    }
    print(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    main()
