#!/usr/bin/env python3
"""Quota-aware model tier routing — dynamic percentile-based thresholds.

Computes the appropriate model tier based on:
- Dynamic thresholds: p10/p90 of Kalman exhausts_in_hours (auto-adjusted weekly)
- Peak hours (06-10 UTC → always economy)
- Active key (friend vs ours: different distributions)
- Task urgency (urgent/standard/background)
- Client X-Model-Tier header override

Imported by zai_proxy.py — zero agent/worker changes needed.

THRESHOLD RULES (computed from historic Kalman data, stored as percentiles):
  - exhausts_in_hours > p90 → reasoning (glm-5.2), ~10% of time
  - p10 < exhausts_in_hours ≤ p90 → standard (glm-4.5), ~80% of time
  - exhausts_in_hours ≤ p10 → economy (glm-4.5-flash), ~10% of time
  - Peak hours (06-10 UTC): always economy, regardless of thresholds
"""
from __future__ import annotations
import json
import os
import time
from pathlib import Path

# ── Tier constants ───────────────────────────────────────────────────────────
TIER_REASONING = "reasoning"
TIER_STANDARD  = "standard"
TIER_ECONOMY   = "economy"

TIER_VALUES = {TIER_ECONOMY: 0, TIER_STANDARD: 1, TIER_REASONING: 2}
TIER_NAMES_REVERSE = {v: k for k, v in TIER_VALUES.items()}

MODEL_MAP = {
    TIER_REASONING: "glm-5.2",
    TIER_STANDARD:  "glm-4.5",
    TIER_ECONOMY:   "glm-4.5-flash",
}

# Client hint names (X-Model-Tier header values)
CLIENT_HINT_ALIASES = {
    "cheap":     TIER_ECONOMY,
    "flash":     TIER_ECONOMY,
    "air":       TIER_STANDARD,
    "mid":       TIER_STANDARD,
    "standard":  TIER_STANDARD,
    "heavy":     TIER_REASONING,
    "reasoning": TIER_REASONING,
}

# Urgency levels — controls dispatch timing in throttled_daemon
URGENCY_URGENT     = "urgent"      # dispatch anytime, even during economy
URGENCY_STANDARD   = "standard"    # follow model tier normally
URGENCY_BACKGROUND = "background"  # only dispatch when tier ≥ standard

# ── Paths ────────────────────────────────────────────────────────────────────
THRESHOLDS_PATH = Path.home() / ".hermes" / "bot" / "model_tier_thresholds.json"
PEAK_HOURS_PATH = Path.home() / ".hermes" / "bot" / "peak_hours.json"
PROXY_STATE_PATH = Path.home() / ".hermes" / "bot" / "zai_proxy_state.json"

# ── Helpers ──────────────────────────────────────────────────────────────────


def _load_peak_hours():
    """Load peak hour window. Returns (start, end) UTC hours."""
    try:
        data = json.loads(PEAK_HOURS_PATH.read_text())
        return data.get("peak_start_utc", 6), data.get("peak_end_utc", 10)
    except Exception:
        return 6, 10


def is_peak_hour() -> bool:
    """Check if current UTC time is within peak hours (3× burn)."""
    try:
        hour = time.gmtime().tm_hour
        ps, pe = _load_peak_hours()
        return ps <= hour < pe
    except Exception:
        return False


def _load_thresholds() -> dict:
    """Load dynamic thresholds from JSON. Returns empty dict on failure."""
    try:
        return json.loads(THRESHOLDS_PATH.read_text())
    except Exception:
        return {}


def _get_live_exhaust(key_name: str) -> float | None:
    """Get live exhausts_in_hours for the binding window from proxy state.

    Reads the proxy's cached predictions (kept warm by _refresh_loop).
    Returns:
      - Minimum exhausts_in_hours across all windows for this key
      - None if predictions unavailable
    """
    try:
        state = json.loads(PROXY_STATE_PATH.read_text())
        predictions = state.get(key_name, {}).get("predictions", [])
        if not predictions:
            return None
        vals = [p.get("exhausts_in_hours") for p in predictions
                if p.get("exhausts_in_hours") is not None and p.get("exhausts_in_hours") > 0]
        return min(vals) if vals else None
    except Exception:
        return None


# ── Tier computation ─────────────────────────────────────────────────────────


def compute_base_tier(active_key: str) -> str:
    """Compute the base model tier using dynamic percentile thresholds.

    1. Peak hours → always economy
    2. Read live exhausts_in_hours from proxy cache
    3. Compare against stored p10/p90 percentiles for this key
    4. Fallback: use static thresholds if no data
    """
    # Peak hours override: always economy (3× burn)
    if is_peak_hour():
        return TIER_ECONOMY

    thresholds = _load_thresholds()
    key_thresholds = thresholds.get(active_key, {})
    p10 = key_thresholds.get("p10_exhaust")
    p90 = key_thresholds.get("p90_exhaust")

    live_exhaust = _get_live_exhaust(active_key)
    if live_exhaust is None:
        # No live data — fall back to static thresholds
        # (conservative: assume we have some headroom but not full power)
        return TIER_STANDARD

    if p90 is not None and live_exhaust > p90:
        return TIER_REASONING
    elif p10 is not None and live_exhaust <= p10:
        return TIER_ECONOMY
    else:
        return TIER_STANDARD


def compute_effective_tier(base_tier: str, task_hint: str | None = None,
                           urgency: str = URGENCY_STANDARD) -> str:
    """Apply client hint override and urgency constraints on top of base tier.

    Rules:
      - urgent tasks: always proceed (even in economy)
      - background tasks: only dispatch if tier ≥ standard
      - client hint 'cheap': force economy regardless
      - client hint 'reasoning': only if base ≥ standard
    """
    # Urgency override
    urg = (urgency or URGENCY_STANDARD).lower().strip()
    if urg == URGENCY_BACKGROUND and TIER_VALUES.get(base_tier, 0) < TIER_VALUES[TIER_STANDARD]:
        # Background tasks defer when in economy mode
        return TIER_ECONOMY  # Still return economy — the dispatcher will skip it
    if urg == URGENCY_URGENT:
        # Urgent tasks go through even in economy — dispatcher always runs them
        pass

    # Client hint override
    if not task_hint:
        return base_tier

    hint = task_hint.lower().strip()
    target = CLIENT_HINT_ALIASES.get(hint)
    if target is None:
        return base_tier

    if target == TIER_ECONOMY:
        return TIER_ECONOMY
    elif target == TIER_REASONING:
        if TIER_VALUES.get(base_tier, 0) >= TIER_VALUES[TIER_STANDARD]:
            return TIER_REASONING
        return base_tier
    else:
        return base_tier


def compute_effective_model(active_key: str, task_hint: str | None = None,
                            urgency: str = URGENCY_STANDARD) -> str:
    """Shortcut: compute the effective model name."""
    base = compute_base_tier(active_key)
    effective = compute_effective_tier(base, task_hint, urgency)
    return MODEL_MAP.get(effective, "glm-4.5-flash")


def compute_tier(active_key: str, task_hint: str | None = None,
                 urgency: str = URGENCY_STANDARD) -> dict:
    """Full tier computation with metadata.

    Returns dict with:
        tier:         str (reasoning/standard/economy)
        model:        str (actual GLM model name)
        base_tier:    str (before hint/urgency override)
        reason:       str (human-readable explanation)
        peak:         bool (in peak hours?)
        p10:          float (dynamic lower threshold)
        p90:          float (dynamic upper threshold)
        live_exhaust: float (current exhausts_in_hours for binding window)
        active_key:   str (which API key is active)
        urgency:      str (client task urgency)
        dispatch_ok:  bool (true if dispatch should proceed)
    """
    peak = is_peak_hour()
    thresholds = _load_thresholds().get(active_key, {})
    p10 = thresholds.get("p10_exhaust")
    p90 = thresholds.get("p90_exhaust")
    live_exhaust = _get_live_exhaust(active_key)

    base_tier = compute_base_tier(active_key)
    effective_tier = compute_effective_tier(base_tier, task_hint, urgency)

    # Determine if dispatch should proceed
    urg = (urgency or URGENCY_STANDARD).lower().strip()
    if urg == URGENCY_BACKGROUND and TIER_VALUES.get(effective_tier, 0) < TIER_VALUES[TIER_STANDARD]:
        dispatch_ok = False  # background tasks defer
    else:
        dispatch_ok = True  # urgent and standard always dispatch

    # Reason string
    parts = []
    if peak:
        parts.append("peak hours 3x burn")
    if live_exhaust is not None and p10 is not None and p90 is not None:
        if live_exhaust > p90:
            parts.append(f"exhaust {live_exhaust:.0f}h > p90 ({p90:.0f}h)")
        elif live_exhaust <= p10:
            parts.append(f"exhaust {live_exhaust:.1f}h ≤ p10 ({p10:.1f}h)")
        else:
            parts.append(f"exhaust {live_exhaust:.0f}h (normal)")
    else:
        parts.append("no kalman data — fallback")
    if task_hint:
        parts.append(f"hint={task_hint}")
    if urg != URGENCY_STANDARD:
        parts.append(f"urgency={urg}")
    reason = f"{effective_tier} ({', '.join(parts)})"

    return {
        "tier": effective_tier,
        "model": MODEL_MAP.get(effective_tier, "glm-4.5-flash"),
        "base_tier": base_tier,
        "reason": reason,
        "peak": peak,
        "p10": p10,
        "p90": p90,
        "live_exhaust": live_exhaust,
        "active_key": active_key,
        "urgency": urg,
        "dispatch_ok": dispatch_ok,
    }
