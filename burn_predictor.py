#!/usr/bin/env python3
"""Burn-rate predictor for z.ai API keys — Kalman filter edition.

Replaces the old EWMA predictor with a 2-state Kalman filter that tracks
both volume (tokens/hour) and velocity (rate of change). This gives:
  - Faster adaptation to traffic surges (velocity captures trends)
  - Linear multi-step forecasting (not flat like EWMA)
  - Uncertainty estimates via the covariance matrix

Pure numpy + stdlib — no ML libraries, no pip installs beyond numpy (already
in the Hermes venv via faster-whisper).
"""
from __future__ import annotations
import json
import os
import sqlite3
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False

DB_PATH = "~/.hermes/bot/zai_usage.db"
QUOTA_URL = "http://localhost:9099/quota"
MIN_DATA_POINTS = 5   # Kalman converges faster than EWMA — lowered from 10
LOOKBACK_HOURS = 12   # More history for better velocity estimation

TUNING_FILE = Path(__file__).parent / "kalman_tuning.json"


def _load_tuning() -> dict:
    """Load Kalman parameter overrides from the tuning file if it exists.

    Written by ``kalman_retune.py``. Holds per-key ``measurement_noise`` (R) and
    ``process_noise`` (Q) overrides so the auto-tuner can adjust the filter
    without editing this source file. Returns an empty dict when the file is
    missing or unreadable, in which case callers fall back to the adaptive
    variance estimate.
    """
    try:
        if TUNING_FILE.exists():
            return json.loads(TUNING_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        pass
    return {}


# ── Kalman filter (2-state: volume + velocity) ──────────────────────────────

class KalmanPredictor:
    """Local-linear-trend Kalman filter for token burn rate prediction.

    State vector:  x = [volume, velocity]  (tokens/hour, delta tokens/hour²)
    Observation:   z = [volume]            (we only measure tokens/hour)

    The filter tracks both how fast we're burning AND whether the burn rate
    is accelerating or decelerating. This lets us project forward linearly
    instead of assuming a constant rate.
    """

    def __init__(self, process_noise: float = 1.0, measurement_noise: float = 50.0):
        if not _HAS_NUMPY:
            raise RuntimeError("numpy required for KalmanPredictor")
        # State: [volume, velocity]
        self.x = np.array([[0.0], [0.0]])
        # State transition: next_vol = cur_vol + velocity; next_vel = velocity
        self.F = np.array([[1.0, 1.0],
                           [0.0, 1.0]])
        # Measurement: we observe volume only
        self.H = np.array([[1.0, 0.0]])
        # Covariance (high initial uncertainty — scale to match R so the filter can actually converge)
        self.P = np.eye(2) * measurement_noise
        # Process noise (how erratic is the system)
        self.Q = np.array([[process_noise, 0.0],
                           [0.0, process_noise]])
        # Measurement noise (how noisy are observations)
        self.R = np.array([[measurement_noise]])
        self._initialized = False

    def update(self, measurement: float) -> None:
        """Incorporate a new hourly token measurement."""
        z = np.array([[float(measurement)]])
        if not self._initialized:
            self.x[0, 0] = float(measurement)
            self._initialized = True
            return
        # Innovation
        y = z - self.H @ self.x
        # Innovation covariance
        S = self.H @ self.P @ self.H.T + self.R
        # Kalman gain
        K = self.P @ self.H.T @ np.linalg.inv(S)
        # State update
        self.x = self.x + K @ y
        # Covariance update
        I = np.eye(2)
        self.P = (I - K @ self.H) @ self.P

    def predict(self) -> float:
        """Predict next-hour volume."""
        self.x = self.F @ self.x
        self.P = self.F @ self.P @ self.F.T + self.Q
        return float(self.x[0, 0])

    def predict_steps_ahead(self, steps: int) -> list[float]:
        """Project N hours ahead using current volume + velocity (no update)."""
        vol = float(self.x[0, 0])
        vel = float(self.x[1, 0])
        return [max(0.0, vol + vel * s) for s in range(1, steps + 1)]

    @property
    def volume(self) -> float:
        return float(self.x[0, 0])

    @property
    def velocity(self) -> float:
        return float(self.x[1, 0])

    @property
    def uncertainty(self) -> float:
        """Position uncertainty (standard deviation)."""
        return float(np.sqrt(self.P[0, 0]))


# ── Per-key predictor instances (persisted across calls) ────────────────────

_predictors: dict[str, KalmanPredictor] = {}


def _get_predictor(key_name: str) -> KalmanPredictor:
    """Get or create the Kalman predictor for a key."""
    if key_name not in _predictors:
        _predictors[key_name] = KalmanPredictor()
    return _predictors[key_name]


# ── Data access ─────────────────────────────────────────────────────────────

def _utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _get_burn_history(key_name: str, hours: int = LOOKBACK_HOURS) -> list[dict]:
    """Read token usage from DB, bucketed by hour."""
    import os
    db_path = os.path.expanduser(DB_PATH)
    if not os.path.exists(db_path):
        return []

    cutoff = time.time() - (hours * 3600)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT ts, total_tokens FROM api_calls WHERE key_name = ? AND ts >= ? AND status_code = 200 ORDER BY ts",
            (key_name, cutoff)
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return []

    # Bucket by hour
    hourly = defaultdict(int)
    for row in rows:
        hour_bucket = int(row["ts"] // 3600) * 3600
        hourly[hour_bucket] += row["total_tokens"]

    return [{"hour_ts": ts, "tokens": tokens} for ts, tokens in sorted(hourly.items())]


def _train_kalman(key_name: str, history: list[dict]) -> KalmanPredictor | None:
    """Train a Kalman predictor from hourly history data."""
    if not _HAS_NUMPY or len(history) < 2:
        return None

    volumes = [h.get("tokens", 0) for h in history]

    # Check for a manual override from the tuning file (written by kalman_retune.py)
    tuning = _load_tuning()
    override_r = tuning.get("measurement_noise", {}).get(key_name)
    override_q = tuning.get("process_noise", {}).get(key_name, 1.0)

    if override_r is not None:
        R = override_r
        Q = override_q
    else:
        # ADAPTIVE: compute measurement noise from the data variance.
        # Auto-scales R to the actual signal magnitude — hourly token buckets
        # run ~1M–10M, so a fixed R=50 was catastrophically miscalibrated
        # (assumed σ≈7 tokens → gain→1, covariance collapsed, 0% coverage).
        mean_v = sum(volumes) / len(volumes)
        variance = sum((v - mean_v) ** 2 for v in volumes) / max(len(volumes) - 1, 1)
        R = max(variance, 1e6)  # floor prevents collapse on near-constant data
        Q = 1.0  # process noise stays small — burn rate doesn't swing wildly hour-to-hour

    kf = KalmanPredictor(process_noise=Q, measurement_noise=R)
    for h in history:
        kf.update(h["tokens"])
    # Run one predict step to estimate the "current" burn rate
    kf.predict()
    _predictors[key_name] = kf
    return kf


def _get_quota_windows(key_name: str) -> list[dict]:
    """Fetch current quota windows from the proxy."""
    try:
        req = urllib.request.Request(QUOTA_URL)
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode())
        windows = data.get(key_name, {}).get("windows", [])
        return windows
    except Exception:
        return []


# ── Prediction API ──────────────────────────────────────────────────────────

def predict_exhaustion(key_name: str) -> list[dict]:
    """Predict whether a key will exhaust quota in each window.

    Uses a Kalman filter trained on historical burn data to project
    volume + velocity forward. Returns per-window projection dicts.
    """
    history = _get_burn_history(key_name)
    windows = _get_quota_windows(key_name)

    if not _HAS_NUMPY or len(history) < MIN_DATA_POINTS:
        return [{
            "key": key_name,
            "window": w.get("name", "unknown"),
            "used_pct": w.get("used_pct", 0),
            "burn_rate_tph": 0,
            "velocity_tph2": 0,
            "uncertainty": 0,
            "hours_left": max(0, (w.get("resets_at", 0) - time.time()) / 3600),
            "projected_additional_pct": 0,
            "will_exhaust": False,
            "exhausts_in_hours": None,
            "note": f"Insufficient data ({len(history)} points, need {MIN_DATA_POINTS})"
                    + ("" if _HAS_NUMPY else " — numpy not available")
        } for w in windows]

    # Train the Kalman filter
    kf = _train_kalman(key_name, history)
    if kf is None:
        return [{
            "key": key_name,
            "window": w.get("name", "unknown"),
            "used_pct": w.get("used_pct", 0),
            "burn_rate_tph": 0,
            "velocity_tph2": 0,
            "uncertainty": 0,
            "hours_left": max(0, (w.get("resets_at", 0) - time.time()) / 3600),
            "projected_additional_pct": 0,
            "will_exhaust": False,
            "exhausts_in_hours": None,
            "note": "Kalman training failed"
        } for w in windows]

    burn_rate = kf.volume       # tokens/hour (smoothed)
    velocity = kf.velocity      # delta tokens/hour (trend)
    uncertainty = kf.uncertainty

    now = time.time()
    predictions = []

    for w in windows:
        window_name = w.get("name", "unknown")
        used_pct = w.get("used_pct", 0)
        resets_at = w.get("resets_at", 0)
        hours_left = max(0, (resets_at - now) / 3600)

        # Proportional analysis: are we burning faster than the clock?
        window_hours = w.get("window_hours", 168)
        started_at = resets_at - window_hours * 3600 if resets_at > 0 else now - window_hours * 3600
        elapsed_hours = max(0, (now - started_at) / 3600)
        elapsed_pct = min(100, (elapsed_hours / window_hours) * 100) if window_hours > 0 else 0
        proportional_over = used_pct - elapsed_pct  # positive = ahead of schedule (bad)

        # Estimate capacity from observed data (needed before proportional rate)
        elapsed = window_hours - hours_left
        if elapsed > 0 and used_pct > 0 and burn_rate > 0:
            estimated_capacity = (burn_rate * elapsed) / (used_pct / 100)
        else:
            estimated_capacity = burn_rate * window_hours if burn_rate > 0 else 0

        # What burn rate would keep us on track? (proportional rate)
        remaining_pct = 100 - used_pct
        proportional_rate_tph = (remaining_pct / max(hours_left, 0.1)) * (estimated_capacity / 100) if estimated_capacity > 0 else 0

        # Project forward using Kalman volume + velocity
        h = int(hours_left)
        if h > 0:
            projected_tokens = h * burn_rate + velocity * h * (h + 1) / 2
            projected_tokens = max(0, projected_tokens)
        else:
            projected_tokens = 0

        projected_additional_pct = (projected_tokens / estimated_capacity * 100) if estimated_capacity > 0 else 0

        will_exhaust = (used_pct + projected_additional_pct) >= 100
        # Also flag if we're proportionally over budget by more than 10pp
        proportionally_over = proportional_over > 10

        if burn_rate > 0 and estimated_capacity > 0:
            remaining_tokens = estimated_capacity * (100 - used_pct) / 100
            effective_rate = burn_rate + max(0, velocity) * h / 2
            exhausts_in_hours = remaining_tokens / effective_rate if effective_rate > 0 and remaining_tokens > 0 else 0
        else:
            exhausts_in_hours = None

        # Trend indicator
        if velocity > burn_rate * 0.1:
            trend = "accelerating"
        elif velocity < -burn_rate * 0.1:
            trend = "decelerating"
        else:
            trend = "stable"

        predictions.append({
            "key": key_name,
            "window": window_name,
            "used_pct": used_pct,
            "elapsed_pct": round(elapsed_pct, 1),
            "proportional_over": round(proportional_over, 1),
            "proportionally_over_budget": proportionally_over,
            "burn_rate_tph": round(burn_rate),
            "proportional_rate_tph": round(proportional_rate_tph) if proportional_rate_tph else 0,
            "velocity_tph2": round(velocity, 1),
            "uncertainty": round(uncertainty),
            "trend": trend,
            "hours_left": round(hours_left, 1),
            "projected_additional_pct": round(projected_additional_pct, 1),
            "projected_total_pct": round(used_pct + projected_additional_pct, 1),
            "will_exhaust": will_exhaust,
            "exhausts_in_hours": round(exhausts_in_hours, 1) if exhausts_in_hours is not None else None,
            "estimated_capacity_tokens": round(estimated_capacity),
            "note": f"{'OVER' if proportionally_over > 0 else 'under'} budget by {abs(proportional_over):.1f}pp"
        })

    return predictions


def predict_all() -> dict:
    """Get predictions for both keys."""
    return {
        "ours": predict_exhaustion("ours"),
        "friend": predict_exhaustion("friend"),
        "timestamp": _utc_now(),
        "method": "kalman" if _HAS_NUMPY else "none",
    }


# ── Unified routing decision function (matrix-driven) ───────────────────────

_MATRIX_CACHE: dict | None = None
_MATRIX_CACHE_TS: float = 0


def _load_matrix() -> dict:
    """Load model_matrix.json directly (cached for 60s)."""
    global _MATRIX_CACHE, _MATRIX_CACHE_TS
    now = time.time()
    if _MATRIX_CACHE and (now - _MATRIX_CACHE_TS) < 60:
        return _MATRIX_CACHE
    try:
        matrix_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model_matrix.json")
        with open(matrix_path) as f:
            matrix = json.load(f)
        _MATRIX_CACHE = matrix
        _MATRIX_CACHE_TS = now
        return matrix
    except Exception:
        return _MATRIX_CACHE or {}


def _is_peak_hour(cost_model: dict) -> bool:
    """Check if current hour is a peak hour."""
    peak_hours = cost_model.get("peak_hours_utc", [6, 7, 8, 9])
    return datetime.now(timezone.utc).hour in peak_hours


def route_request(estimated_tokens: int = 0,
                  difficulty: str = "medium",
                  prefer_free: bool = True) -> dict:
    """Decide which key and model to use for a request.

    Args:
        estimated_tokens: Rough token count for this prompt (0 = unknown)
        difficulty: "simple", "medium", or "complex"
        prefer_free: If True, strongly prefer free z.ai over paid PPQ

    Returns:
        {
            "tier": "zai" | "ppq" | "ollama",
            "key": "ours" | "friend" | None,
            "model": model_id,
            "base_url": url,
            "reason": explanation,
            "cost_estimate_usd": float,
        }
    """
    # Load the model decision matrix
    matrix = _load_matrix()
    cost_model = matrix.get("cost_model", {})
    is_peak = _is_peak_hour(cost_model)
    cost_key = "cost_per_1m_peak" if is_peak else "cost_per_1m_offpeak"
    peak_note = "PEAK" if is_peak else "off-peak"

    # Get Kalman predictions for both keys
    preds = predict_all()
    ours_preds = preds.get("ours", [])
    friend_preds = preds.get("friend", [])

    def _key_ok(preds_list):
        if not preds_list:
            return True
        for p in preds_list:
            if p.get("will_exhaust"):
                return False
        return True  # proportional overage is a cost penalty, NOT a hard filter

    def _max_overage(preds_list):
        if not preds_list:
            return 0
        return max(p.get("proportional_over", 0) for p in preds_list)

    ours_ok = _key_ok(ours_preds)
    friend_ok = _key_ok(friend_preds)
    ours_over = _max_overage(ours_preds)
    friend_over = _max_overage(friend_preds)

    # Collect ALL candidates from the matrix with their effective costs
    quality_threshold = {"simple": 60, "medium": 75, "complex": 85}.get(difficulty, 75)
    candidates = []

    for model_id, model_data in matrix.get("models", {}).items():
        bench = model_data.get("benchmarks", {})
        quality = bench.get("coding", bench.get("aa_quality", bench.get("lmsys_elo", 70)))
        if isinstance(quality, (int, float)) and quality > 100:
            quality = min(100, int((quality - 1000) / 5))
        ctx = model_data.get("context_length", 32768)
        if quality < quality_threshold:
            continue
        if estimated_tokens > 0 and ctx < estimated_tokens:
            continue

        for key_id, key_data in model_data.get("keys", {}).items():
            if key_id.startswith("zai/"):
                key_name = key_id.split("/")[1]
                if key_name == "ours" and not ours_ok:
                    continue
                if key_name == "friend" and not friend_ok:
                    continue

            base_cost = key_data.get(cost_key, key_data.get("cost_per_1m_offpeak", 999))
            penalty = 1 + key_data.get("penalty_pct", 0) / 100

            # Proportional overage penalty: if a z.ai key is burning faster than
            # the clock, add a cost penalty (not a hard block). +1% per pp over.
            if key_id.startswith("zai/"):
                key_name = key_id.split("/")[1]
                overage = ours_over if key_name == "ours" else friend_over
                if overage > 0:
                    penalty += overage / 100  # 10pp over = 10% extra penalty

            effective_cost = base_cost * penalty

            candidates.append({
                "tier": key_id.split("/")[0],
                "key": key_id.split("/")[1] if "/" in key_id else None,
                "model": model_data.get("name", model_id),
                "model_id": model_id,
                "base_url": key_data.get("base_url", ""),
                "effective_cost": round(effective_cost, 4),
                "quality": quality,
                "key_id": key_id,
            })

    if not candidates:
        return {
            "tier": "ollama", "key": "local", "model": "qwen2.5-coder:3b",
            "base_url": "http://localhost:11434/v1",
            "reason": "All providers exhausted - Ollama last resort",
            "cost_estimate_usd": 0.0,
        }

    candidates.sort(key=lambda c: (c["effective_cost"], -c["quality"]))
    best = candidates[0]

    out_tokens = min(estimated_tokens * 0.5, 4096) if estimated_tokens > 0 else 1000
    cost_usd = best["effective_cost"] * (estimated_tokens + out_tokens) / 1e6 if estimated_tokens > 0 else 0.001

    over_note = ""
    if best["key"] == "ours" and abs(ours_over) > 1:
        over_note = f" ({ours_over:+.1f}pp vs schedule)"
    elif best["key"] == "friend":
        over_note = f" (+21% penalty, {friend_over:+.1f}pp)" if abs(friend_over) > 1 else " (+21% penalty)"

    return {
        "tier": best["tier"],
        "key": best["key"],
        "model": best["model"],
        "base_url": best["base_url"],
        "reason": f"{best['model']} via {best['key_id']} - ${best['effective_cost']}/1M ({peak_note}){over_note}",
        "cost_estimate_usd": round(cost_usd, 5),
        "effective_cost_per_1m": best["effective_cost"],
        "quality_score": best["quality"],
        "is_peak_hour": is_peak,
    }


if __name__ == "__main__":
    predictions = predict_all()
    print(json.dumps(predictions, indent=2))
    print("\n=== Routing Decision (medium difficulty, 5000 tokens) ===")
    decision = route_request(estimated_tokens=5000, difficulty="medium")
    print(json.dumps(decision, indent=2))
