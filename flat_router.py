#!/usr/bin/env python3
"""flat_router.py — Phase 1 flat router: select_provider() in SHADOW MODE.

Implements the flat-hierarchy provider selection described in
~/.hermes/profiles/manager/state/flat-router-design.md (§2).

This module runs ALONGSIDE the existing best_key() routing — it does NOT
replace it. select_provider() is called in shadow mode to log what it
WOULD have chosen, so operators can compare the two strategies.

Key design principles:
  - All providers are equal (no z.ai preference).
  - Model matching is a first-class filter.
  - Health gating excludes unhealthy providers before cost comparison.
  - Cost ordering uses the existing RoutingOptimizer / Kalman infrastructure.
  - Never raises — all failures produce safe defaults.

Author: Hermes Agent (manager profile)
Date: 2026-08-24
Phase: 1 (shadow only — no routing change)
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

# ── Path bootstrap ──────────────────────────────────────────────────────────
_BOT = os.path.expanduser("~/.hermes/bot")
_MRE = os.path.expanduser("~/merchant-routing-engine")
for _p in [_BOT, _MRE, os.path.join(_MRE, "src")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ── Lazy imports from zai_proxy (avoid circular at module-load) ─────────────
# These are resolved at call time so importing this module never crashes.

def _resolve(name: str):
    """Resolve a name from zai_proxy module, returning None on failure."""
    try:
        import zai_proxy
        return getattr(zai_proxy, name, None)
    except Exception:
        return None


# ── ProviderCandidate ───────────────────────────────────────────────────────

@dataclass
class ProviderCandidate:
    """One viable provider in the flat routing candidate list.

    Attributes:
        name: provider name (e.g., "ppq", "ours", "ollama_cloud")
        model: model name to send to this provider
        effective_cost: $/M effective cost (float('inf') if unreachable)
        dispatch_fn: callable — the Handler._try_* method to invoke.
                     None for the fallback candidate.
        reason: why this provider was chosen/ranked
    """
    name: str
    model: str
    effective_cost: float
    dispatch_fn: Callable | None
    reason: str = ""


# ── PROVIDER_MODELS registry (design doc §2.6) ──────────────────────────────
# Dict mapping provider name -> set of model IDs it can serve.
# Covers ALL 12 providers from the design doc §3.
# NO CAPS on any provider — free market price discovery.

PROVIDER_MODELS: dict[str, set[str]] = {
    # z.ai keys — all z.ai models
    "ours": {
        "glm-5.2", "glm-5.3", "glm-4.5-flash", "glm-4.5-air", "glm-4.5",
        "deepseek/deepseek-v4-pro", "deepseek/deepseek-v4-flash",
    },
    "friend": {
        "glm-5.2", "glm-5.3", "glm-4.5-flash", "glm-4.5-air", "glm-4.5",
        "deepseek/deepseek-v4-pro", "deepseek/deepseek-v4-flash",
    },
    # Ollama Cloud — included subscription, wide model catalog
    "ollama_cloud": {
        "glm-5.2", "glm-4.5-flash", "kimi-k3:cloud", "kimi-k2.7-code",
        "gpt-oss:120b", "gemma4:31b", "qwen3.5:397b",
    },
    "ollama_cloud_2": {
        "glm-5.2", "glm-4.5-flash", "kimi-k3:cloud", "kimi-k2.7-code",
        "gpt-oss:120b", "gemma4:31b", "qwen3.5:397b",
    },
    # OpenCode Go — flat-rate $10/mo, native glm-5.3, 29 models
    "opencode_go": {
        "glm-5.2", "glm-5.3", "kimi-k3", "kimi-k2.7-code",
        "deepseek/deepseek-v4-pro", "deepseek/deepseek-v4-flash",
        "deepseek-v4-pro", "deepseek-v4-flash",
    },
    # NeuralWatt — per-token, deepseek-v4-flash $0.14/M
    "neuralwatt": {
        "glm-5.2", "kimi-k3", "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro", "deepseek/gemma-4-31b",
    },
    # DeepInfra — per-token, ~$1.30/M
    "deepinfra": {
        "deepseek/deepseek-v4-pro", "deepseek/deepseek-v4-flash",
        "glm-5.2",
    },
    # PPQ — per-token, ~$0.80/M
    "ppq": {
        "glm-5.2", "kimi-k3", "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro",
    },
    # OpenRouter — per-token, rates vary by model
    "openrouter": {
        "glm-5.2", "kimi-k3", "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro",
    },
    # Telnyx — Kimi-focused by operator decision
    "telnyx": {
        "kimi-k3", "kimi-k2.5", "gpt-5", "claude-haiku-4-5",
        "minimax-m3", "kimi-k3:cloud", "kimi-k2.7-code",
    },
    # Routstr — Cashu-metered, same model IDs as proxy
    "routstr": {
        "glm-5.2", "kimi-k3", "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro", "glm-5.3", "glm-4.5-flash",
    },
    # Routstrd — Cashu-metered, network catalog
    "routstrd": {
        "glm-5.2", "kimi-k3", "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro", "glm-5.3", "glm-4.5-flash",
    },
}


# ── Seed rates for providers not in the shadow optimizer ────────────────────
# These are used to seed PriceKalman for providers that don't have one yet.
_SEED_RATES: dict[str, float] = {
    "ours":          0.068,
    "friend":        0.082,   # 0.068 * 1.21
    "ollama_cloud":  0.40,
    "ollama_cloud_2": 0.40,
    "opencode_go":   0.40,
    "neuralwatt":    2.21,
    "deepinfra":     1.30,
    "ppq":           0.80,
    "openrouter":    1.50,    # estimated, varies by model
    "telnyx":        5.40,
    "routstr":       1.00,    # estimated from routstr_probe
    "routstrd":      1.00,    # estimated
}

# Map proxy provider names to shadow optimizer names
_PROXY_TO_OPTIMIZER_NAME = {
    "ours": "zai_ours",
    "friend": "zai_friend",
    "ppq": "ppq_external",
}

# ── PROVIDER_TIER: 5-tier classification (time-aware pricing design §2) ──────
# T1 quota:   z.ai keys with weekly reset — unused quota is wasted
# T2 balance: NeuralWatt prepaid balance — depletes, then per-token overage
# T3 flat:    opencode_go $10/mo — marginal cost $0
# T4 included: ollama_cloud subscriptions — marginal cost $0
# T5 per-token: pay per token — standard Kalman discovery
PROVIDER_TIER: dict[str, str] = {
    "ours":           "quota",
    "friend":         "quota",
    "neuralwatt":     "balance",
    "opencode_go":    "flat",
    "ollama_cloud":   "included",
    "ollama_cloud_2": "included",
    "deepinfra":      "per_token",
    "ppq":            "per_token",
    "telnyx":         "per_token",
    "openrouter":     "per_token",
    "routstr":        "per_token",
    "routstrd":       "per_token",
}

# ── Tier-specific constants ──────────────────────────────────────────────────
MIN_EFFECTIVE_PRICE = 0.001           # $/M floor (T3/T4, and global min)
NW_CORRECTION_FACTOR = 0.2762         # NeuralWatt 3.6× overcounting fix
NW_MAX_DEPLETION_PENALTY = 2.0        # At 0 balance, price triples
NW_INITIAL_BALANCE = 100.0            # NeuralWatt $100/mo Pro plan
QUOTA_WEEK_DAYS = 7.0                 # z.ai weekly quota window
OFF_PEAK_FACTOR = 0.5                 # Price multiplier during off-peak hours
# Off-peak hours: UTC 10:00–6:00 next day (i.e. not 06:00–10:00 peak)
_OFF_PEAK_HOURS = set(range(10, 24)) | set(range(0, 6))

# Kill switch flag files
_TIME_DECAY_FLAG = os.path.expanduser("~/.hermes/bot/.disable_time_decay")
_DEPLETION_FLAG = os.path.expanduser("~/.hermes/bot/.disable_depletion_penalty")


# ── _ALL_PROVIDERS: runtime registry of provider Kalman state ───────────────
# Populated at module init from the shadow optimizer (if available).
# Each entry: { "price_kalman": PriceKalman, "consumption_kalman": ConsumptionKalman }
_ALL_PROVIDERS: dict[str, dict[str, Any]] = {}


def _init_provider_registry():
    """Populate _ALL_PROVIDERS from the shadow optimizer's registered providers."""
    try:
        import zai_proxy
        shadow_opt = getattr(zai_proxy, "_shadow_optimizer", None)
        if shadow_opt is not None:
            for name, prov in shadow_opt._providers.items():
                _ALL_PROVIDERS[name] = {
                    "price_kalman": prov.get("price_kalman"),
                    "consumption_kalman": prov.get("consumption_kalman"),
                }
    except Exception:
        pass

    # Also seed providers not in the shadow optimizer (openrouter, routstr, routstrd, ours)
    try:
        from price_kalman import PriceKalman
        from consumption_kalman import ConsumptionKalman

        for name, rate in _SEED_RATES.items():
            opt_name = _PROXY_TO_OPTIMIZER_NAME.get(name, name)
            if opt_name not in _ALL_PROVIDERS:
                pk = PriceKalman(initial_rate=rate, process_noise=1e-6, measurement_noise=1e-4)
                pk.update(rate)
                _ALL_PROVIDERS[opt_name] = {
                    "price_kalman": pk,
                    "consumption_kalman": ConsumptionKalman(),
                }
            # Also register under the proxy name (without zai_ prefix)
            if name not in _ALL_PROVIDERS:
                pk = PriceKalman(initial_rate=rate, process_noise=1e-6, measurement_noise=1e-4)
                pk.update(rate)
                _ALL_PROVIDERS[name] = {
                    "price_kalman": pk,
                    "consumption_kalman": ConsumptionKalman(),
                }
    except Exception:
        pass


_init_provider_registry()


# ── Health gate helpers (resolve from zai_proxy at call time) ───────────────

def _is_manually_disabled(name: str) -> bool:
    """Check if provider is manually disabled. Resolves from zai_proxy."""
    fn = _resolve("_is_manually_disabled")
    if fn is not None:
        return fn(name)
    return False


def _is_key_healthy(name: str) -> bool:
    """Check if key is healthy (backoff, paywall, circuit breaker). Resolves from zai_proxy."""
    fn = _resolve("_is_key_healthy")
    if fn is not None:
        return fn(name)
    return True


def _is_provider_funded(name: str) -> bool:
    """Check if external provider has credits. Resolves from zai_proxy."""
    fn = _resolve("_is_provider_funded")
    if fn is not None:
        return fn(name)
    return True


# ── _is_provider_healthy() — unified health gate (design doc §2.7) ──────────

# Names that are z.ai keys (subscription, not balance-tracked)
_ZAI_KEYS = frozenset({"ours", "friend"})

# Names that are flat-rate / included (no balance tracking)
_FLAT_RATE_PROVIDERS = frozenset({
    "ollama_cloud", "ollama_cloud_2", "opencode_go",
})


def _is_provider_healthy(name: str) -> bool:
    """Unified health gate for ALL providers.

    Combines:
      1. Manual disable check (~/.hermes/bot/.key_disabled_<name>)
      2. Key health (backoff, paywall, circuit breaker)
      3. Provider funding (for external per-token providers only)

    Returns True if the provider is healthy and can serve requests.
    Never raises — all failures default to True (optimistic) to avoid
    blocking the shadow comparison.
    """
    try:
        # 1. Manual disable — checked first, overrides everything
        if _is_manually_disabled(name):
            return False

        # 2. Key health (backoff, paywall, circuit breaker)
        if not _is_key_healthy(name):
            return False

        # 3. Funding check — only for external per-token providers
        #    z.ai keys are subscription, flat-rate providers have no balance
        if name not in _ZAI_KEYS and name not in _FLAT_RATE_PROVIDERS:
            if not _is_provider_funded(name):
                return False

        return True
    except Exception:
        # Never raise — shadow mode must not break production
        return True


# ── Tier-specific helper functions (time-aware pricing design §2) ────────────

def _is_time_decay_disabled() -> bool:
    """Check if the time-decay kill switch is active."""
    return os.path.exists(_TIME_DECAY_FLAG)


def _is_depletion_disabled() -> bool:
    """Check if the depletion penalty kill switch is active."""
    return os.path.exists(_DEPLETION_FLAG)


def _get_off_peak_factor() -> float:
    """Return peak_factor: 0.5 during off-peak hours, 1.0 during peak (UTC 6-10)."""
    try:
        hour = time.gmtime().tm_hour
        if hour in _OFF_PEAK_HOURS:
            return OFF_PEAK_FACTOR
        return 1.0
    except Exception:
        return 1.0


def _get_quota_windows(name: str) -> list[dict]:
    """Get quota windows for a z.ai key from quota_cache (via zai_proxy)."""
    try:
        qc = _resolve("quota_cache")
        if qc is not None:
            entry = qc.get(name)
            if entry is not None:
                return entry[0]  # (windows, timestamp)
    except Exception:
        pass
    return []


def _compute_time_decay(name: str) -> float:
    """Compute time_decay for a quota provider.

    time_decay = days_to_reset / 7.0
    - At 7 days to reset: 1.0 (full price, quota is fresh)
    - At 0 days to reset: 0.0 (use-it-or-lose-it, price drops to zero)
    - Unknown reset time: 1.0 (no decay, safe default)

    Returns a value in [0.01, 1.0] — never quite zero so the floor in
    effective_price() prevents always-wins.
    """
    windows = _get_quota_windows(name)
    weekly = next((w for w in windows if w.get("name") == "weekly"), None)
    if weekly and weekly.get("resets_at"):
        seconds_to_reset = weekly["resets_at"] - time.time()
        days_to_reset = max(0.0, seconds_to_reset / 86400.0)
        return max(0.01, days_to_reset / QUOTA_WEEK_DAYS)
    # Check if it's a "1w" or "7d" window as fallback
    if weekly and weekly.get("window_hours", 0) >= 168:
        # Has a weekly window but no reset time → no decay
        return 1.0
    return 1.0  # unknown → no decay


def _compute_quota_health(name: str) -> float:
    """Compute quota_health for a quota provider.

    quota_health = 1.0 - used_pct / 100.0
    - 0% used: 1.0 (healthy, full quota available)
    - 100% used: 0.0 (exhausted — provider should be excluded by health gate)

    Returns a value in [0.0, 1.0].
    """
    windows = _get_quota_windows(name)
    weekly = next((w for w in windows if w.get("name") == "weekly"), None)
    if weekly:
        used_pct = float(weekly.get("used_pct", 0))
        return max(0.0, 1.0 - used_pct / 100.0)
    # Fallback: use max across all windows
    if windows:
        max_pct = max(w.get("used_pct", 0) for w in windows)
        return max(0.0, 1.0 - max_pct / 100.0)
    return 1.0  # unknown → optimistic


def _compute_depletion_penalty(name: str) -> float:
    """Compute depletion penalty for a balance-based provider (NeuralWatt).

    depletion_penalty = (1 - remaining_balance / initial_balance) × max_penalty
    - At 100% balance: 0.0 (no penalty)
    - At 0% balance: max_penalty (2.0 — price triples)

    Returns 0.0 if the kill switch is active.
    """
    if _is_depletion_disabled():
        return 0.0

    try:
        snap_fn = _resolve("_neuralwatt_quota_snapshot")
        if snap_fn is not None:
            entry = snap_fn()
            remaining = float(entry.get("remaining_usd", 0.0))
            initial = float(entry.get("total_credits_usd", NW_INITIAL_BALANCE))
            if initial <= 0:
                return NW_MAX_DEPLETION_PENALTY  # conservative
            depletion = max(0.0, 1.0 - remaining / initial)
            return depletion * NW_MAX_DEPLETION_PENALTY
    except Exception:
        pass

    # Bridge disabled or error → conservative (treat as depleted)
    return NW_MAX_DEPLETION_PENALTY


def compute_effective_price(
    provider: str,
    base_rate: float,
    context: dict | None = None,
) -> float:
    """Compute the effective $/M price for a provider based on its tier.

    This applies the tier-specific formula from the time-aware pricing design:

    T1 (quota): base × (days_to_reset/7) × (1 - used_pct/100) × peak_factor × health_factor
    T2 (balance): base × (1 + depletion_penalty) × correction_factor
    T3 (flat): MIN_EFFECTIVE_PRICE ($0.001/M)
    T4 (included): MIN_EFFECTIVE_PRICE ($0.001/M)
    T5 (per_token): base_rate (Kalman-measured, no time decay or balance factor)

    The Kalman filter CONTINUES measuring real $/M from traffic. The tier formula
    adjusts the EFFECTIVE cost for routing decisions only.

    Args:
        provider: provider name (e.g., "ours", "neuralwatt", "ppq")
        base_rate: Kalman-measured base $/M rate for this provider
        context: optional dict with pre-computed values:
            - "quota_windows": list of window dicts (for T1)
            - "depletion_penalty": float (for T2, overrides computation)
            - "health_factor": float (for T1, overrides computation)
            - "peak_factor": float (for T1, overrides computation)

    Returns:
        Effective $/M price, always >= MIN_EFFECTIVE_PRICE.
        Returns float('inf') if the provider is exhausted/unavailable.
    """
    ctx = context or {}
    tier = PROVIDER_TIER.get(provider, "per_token")  # default to per-token

    try:
        if tier == "quota":
            # T1: z.ai keys — time-decay + quota-health + peak + health
            if _is_time_decay_disabled():
                # Kill switch: skip time-decay, use base × peak × health
                peak_factor = ctx.get("peak_factor", _get_off_peak_factor())
                health_factor = ctx.get("health_factor", 1.0)
                effective = base_rate * peak_factor * health_factor
            else:
                time_decay = ctx.get("time_decay", _compute_time_decay(provider))
                quota_health = ctx.get("quota_health", _compute_quota_health(provider))

                # If quota exhausted, provider is unavailable
                if quota_health <= 0.0:
                    return float("inf")

                peak_factor = ctx.get("peak_factor", _get_off_peak_factor())
                health_factor = ctx.get("health_factor", 1.0)

                if health_factor <= 0:
                    return float("inf")

                effective = base_rate * time_decay * quota_health * peak_factor * health_factor

        elif tier == "balance":
            # T2: NeuralWatt — depletion penalty + correction factor
            depletion_penalty = ctx.get("depletion_penalty", _compute_depletion_penalty(provider))
            effective = base_rate * (1.0 + depletion_penalty) * NW_CORRECTION_FACTOR

        elif tier in ("flat", "included"):
            # T3/T4: flat-rate / included — $0.001/M floor
            effective = MIN_EFFECTIVE_PRICE

        else:
            # T5: per-token — Kalman-measured rate, no time decay
            effective = base_rate

        return max(MIN_EFFECTIVE_PRICE, effective)

    except Exception:
        # Never crash — return base_rate as safe fallback
        return max(MIN_EFFECTIVE_PRICE, base_rate)


# ── Cost evaluation ─────────────────────────────────────────────────────────

def _get_effective_cost(name: str, model: str | None, difficulty: str = "medium") -> float:
    """Get the effective $/M cost for a provider.

    Uses the shadow optimizer's PriceKalman to get the base rate, then applies
    the 5-tier time-aware pricing formula via compute_effective_price().

    Falls back to seed rate with tier formula, then to raw seed rate.
    """
    try:
        # Try shadow optimizer first
        import zai_proxy
        shadow_opt = getattr(zai_proxy, "_shadow_optimizer", None)
        if shadow_opt is not None:
            opt_name = _PROXY_TO_OPTIMIZER_NAME.get(name, name)
            prov = shadow_opt._providers.get(opt_name)
            if prov is None:
                prov = shadow_opt._providers.get(name)
            if prov is not None:
                pk = prov.get("price_kalman")
                quota_remaining = prov.get("quota_remaining", float("inf"))
                quota_total = prov.get("quota_total")
                failure_count = _get_failure_count(name)
                breaker = failure_count > 10

                from price_kalman import peak_multiplier, scarcity_factor, health_pricing_factor
                prov_ph = prov.get("peak_hours_utc")
                hour = None
                if prov_ph:
                    prov_peak = peak_multiplier(
                        hour=hour, peak_hours_utc=prov_ph,
                        peak_mult=prov.get("peak_mult", 1.0))
                else:
                    prov_peak = 1.0

                # Scarcity
                if quota_total and quota_total > 0:
                    quota_used_pct = max(0.0, (1.0 - quota_remaining / quota_total) * 100.0)
                else:
                    quota_used_pct = 0.0
                scarcity = scarcity_factor(quota_used_pct)

                # Health
                health = health_pricing_factor(failure_count=failure_count, breaker_tripped=breaker)

                if math.isinf(health):
                    return float("inf")

                # Get the Kalman-measured base rate ($/M)
                base_rate = float(pk.predict())

                # Build context for tier-specific computation
                tier = PROVIDER_TIER.get(name, "per_token")
                context: dict[str, Any] = {
                    "health_factor": health if not math.isinf(health) else 0.0,
                    "peak_factor": prov_peak,
                }

                # For per-token providers (T5), apply the existing Kalman
                # effective_price multipliers (peak, scarcity, health) on top
                # of the base rate — this preserves the original behavior.
                if tier == "per_token":
                    effective = pk.effective_price(
                        peak_mult=prov_peak, scarcity=scarcity,
                        health=health, pace_mult=1.0)
                    return float(effective)

                # For all other tiers, use compute_effective_price() which
                # applies the tier-specific formula on top of the Kalman base.
                effective = compute_effective_price(name, base_rate, context)
                return float(effective)
    except Exception:
        pass

    # Fall back: use seed rate with tier formula
    try:
        base_rate = _SEED_RATES.get(name, 999.0)
        effective = compute_effective_price(name, base_rate)
        return float(effective)
    except Exception:
        pass

    # Ultimate fallback: seed rate
    return _SEED_RATES.get(name, 999.0)


def _get_failure_count(name: str) -> int:
    """Get the consecutive failure count for a provider."""
    try:
        import zai_proxy
        health = getattr(zai_proxy, "_zai_key_health", {})
        h = health.get(name)
        if h:
            return int(h.get("consecutive_failures", 0))
    except Exception:
        pass
    return 0


# ── _dispatch_to_provider() — maps provider name to dispatch method ─────────

def _make_dispatch_fn(name: str) -> Callable | None:
    """Create a dispatch function for the given provider name.

    Returns a callable that, when invoked with (handler, body, model, buffer, t0),
    calls the right _try_* method on the handler.
    """
    # z.ai keys → _try_zai_key (extracted from the old retry loop in _proxy)
    if name in ("ours", "friend"):
        def _dispatch_zai(handler, body, model, buffer, t0):
            return handler._try_zai_key(name, body, model, buffer, t0)
        return _dispatch_zai

    # ollama_cloud → _try_ollama_cloud_any
    if name in ("ollama_cloud", "ollama_cloud_2"):
        def _dispatch_ollama(handler, body, model, buffer, t0):
            return handler._try_ollama_cloud_any(body, model, buffer, t0)
        return _dispatch_ollama

    # opencode_go → _try_opencode_go
    if name == "opencode_go":
        def _dispatch_opencode(handler, body, model, buffer, t0):
            return handler._try_opencode_go(body, model, buffer, t0)
        return _dispatch_opencode

    # telnyx → _try_telnyx
    if name == "telnyx":
        def _dispatch_telnyx(handler, body, model, buffer, t0):
            return handler._try_telnyx(body, model, buffer, t0)
        return _dispatch_telnyx

    # External providers (deepinfra, ppq, openrouter, neuralwatt, routstr, routstrd)
    # → _try_external_failover with preferred=name
    def _dispatch_external(handler, body, model, buffer, t0):
        return handler._try_external_failover(body, model, buffer, t0, preferred=name)
    return _dispatch_external


def _dispatch_to_provider(handler, name: str, body: bytes, model: str,
                          response_buffer: bytearray, t0: float) -> bool:
    """Unified dispatch: call the right _try_* method for this provider.

    Maps provider name to dispatch function:
      ours/friend → z.ai upstream (retry loop handles this)
      ollama_cloud/ollama_cloud_2 → _try_ollama_cloud_any
      opencode_go → _try_opencode_go
      telnyx → _try_telnyx
      deepinfra/ppq/openrouter/neuralwatt/routstr/routstrd → _try_external_failover (single provider)

    Returns True on success (response already sent),
    False on failure (caller should try next provider).
    """
    try:
        dispatch_fn = _make_dispatch_fn(name)
        if dispatch_fn is None:
            return False
        return dispatch_fn(handler, body, model, response_buffer, t0)
    except Exception:
        return False


# ── _update_kalman_after_request() — post-request Kalman update ─────────────

def _update_kalman_after_request(
    provider: str,
    cost_usd: float | None,
    total_tokens: int,
) -> None:
    """Update the provider's PriceKalman + ConsumptionKalman after a request.

    PriceKalman: updated with measured $/M = (cost_usd / total_tokens) * 1_000_000
    ConsumptionKalman: updated with token count

    Called after successful dispatch (but NOT wired into routing yet —
    just available for Phase 2). Never raises.

    Args:
        provider: provider name (proxy name, e.g., "ppq", "ours")
        cost_usd: measured cost in USD (from _extract_cost), or None
        total_tokens: total tokens consumed in the request
    """
    try:
        opt_name = _PROXY_TO_OPTIMIZER_NAME.get(provider, provider)
        prov = _ALL_PROVIDERS.get(opt_name) or _ALL_PROVIDERS.get(provider)
        if prov is None:
            return

        # Update PriceKalman with measured $/M
        pk = prov.get("price_kalman")
        if pk is not None and cost_usd is not None and total_tokens > 0:
            measured_rate = (cost_usd / total_tokens) * 1_000_000
            if measured_rate >= 0 and not math.isinf(measured_rate) and not math.isnan(measured_rate):
                pk.update(measured_rate)

        # Update ConsumptionKalman with token count
        ck = prov.get("consumption_kalman")
        if ck is not None and total_tokens > 0:
            ck.update(float(total_tokens))
    except Exception:
        pass  # Never raise — Kalman update failure must not break routing


# ── select_provider() — the flat router core function ───────────────────────

def select_provider(
    model: str | None,
    task_type: str = "coding",
    estimated_tokens: int = 10000,
    difficulty: str = "medium",
) -> list[ProviderCandidate]:
    """Flat-hierarchy provider selection.

    Returns an ORDERED LIST of viable providers, cheapest first.
    The caller iterates the list: try each provider, on failure try the next.

    Each ProviderCandidate contains:
        - name: str (provider name)
        - model: str (model name to send to this provider)
        - effective_cost: float ($/M effective)
        - dispatch_fn: callable (the _try_* method to invoke)
        - reason: str (why this provider was chosen/ranked)

    Never returns empty list — if no provider is viable, returns
    [ProviderCandidate(name="fallback", ...)] so the caller can send a 503.

    Phase 1: runs in SHADOW MODE only. best_key() still drives all routing.
    """
    try:
        model_id = model or "glm-5.2"
        candidates: list[ProviderCandidate] = []

        for name, models in PROVIDER_MODELS.items():
            # 1. Model filter — only providers that can serve this model
            if model_id not in models:
                # Also check if the model might be served under a different name
                # (e.g., glm-5.3 → glm-5.2 on ollama_cloud)
                # For now, exact match only — model translation happens at dispatch time
                continue

            # 2. Health gate — exclude unhealthy providers
            if not _is_provider_healthy(name):
                continue

            # 3. Cost evaluation (via Kalman / shadow optimizer)
            cost = _get_effective_cost(name, model_id, difficulty)

            # 4. Build dispatch function
            dispatch_fn = _make_dispatch_fn(name)

            # 5. Determine the model name to send to this provider
            provider_model = _resolve_model_for_provider(name, model_id)

            reason = f"effective ${cost:.6f}/M (model={provider_model})"
            candidates.append(ProviderCandidate(
                name=name,
                model=provider_model,
                effective_cost=cost,
                dispatch_fn=dispatch_fn,
                reason=reason,
            ))

        # Sort cheapest first (inf sorts to end)
        candidates.sort(key=lambda c: c.effective_cost)

        if not candidates:
            # Fallback candidate — no viable providers
            candidates.append(ProviderCandidate(
                name="fallback",
                model=model_id,
                effective_cost=float("inf"),
                dispatch_fn=None,
                reason="no viable provider found",
            ))

        return candidates
    except Exception:
        # Never raise — return a fallback candidate
        return [ProviderCandidate(
            name="fallback",
            model=model or "unknown",
            effective_cost=float("inf"),
            dispatch_fn=None,
            reason="select_provider error",
        )]


def _resolve_model_for_provider(name: str, model: str) -> str:
    """Resolve the model name to send to a specific provider.

    Uses _PROVIDER_MODEL_NAMES from zai_proxy for translation.
    Falls back to the original model name if no mapping exists.
    """
    try:
        model_names = _resolve("_PROVIDER_MODEL_NAMES")
        if model_names is not None:
            mapping = model_names.get(name, {})
            if model in mapping:
                return mapping[model]
    except Exception:
        pass
    return model


# ── Shadow logging — records the comparison for observability ───────────────

_CREATE_SHADOW_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS routing_shadow_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    live_provider TEXT,
    live_model TEXT,
    shadow_provider TEXT,
    shadow_model TEXT,
    shadow_cost REAL,
    live_cost REAL,
    tokens INTEGER,
    agree INTEGER,
    reason TEXT,
    pressure_provider TEXT,
    pressure_model TEXT,
    pressure_cost REAL,
    actual_cost REAL,
    divergence REAL,
    is_429 INTEGER DEFAULT 0,
    paid_provider INTEGER DEFAULT 0,
    requested_model TEXT,
    per_model_base_rate REAL,
    per_model_source TEXT,
    quota_regime TEXT
);
"""

_CREATE_FLAT_ROUTER_SHADOW_SQL = """\
CREATE TABLE IF NOT EXISTS flat_router_shadow_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    best_key_choice TEXT,
    flat_router_top TEXT,
    flat_router_top_cost REAL,
    agreement INTEGER,
    model TEXT,
    candidate_list TEXT
);
"""


def _log_flat_router_shadow(
    db_path: str | None = None,
    best_key_choice: str | None = None,
    candidates: list[ProviderCandidate] | None = None,
    model: str | None = None,
) -> None:
    """Log a flat router shadow comparison to the DB.

    Records:
      - timestamp
      - best_key choice
      - select_provider top candidate
      - agreement (yes=1/no=0)
      - full candidate list with prices (JSON)

    Uses a separate table 'flat_router_shadow_decisions' so it doesn't
    interfere with the existing shadow logging. Falls back to the existing
    'routing_shadow_decisions' table if db_path is the production DB.

    Never raises — logging must not break the hot path.
    """
    try:
        if db_path is None:
            db_path = os.path.expanduser("~/.hermes/bot/zai_usage.db")

        if candidates is None:
            candidates = []

        top = candidates[0] if candidates else None
        top_name = top.name if top else None
        top_cost = top.effective_cost if top else None

        # Agreement: does best_key's choice match the flat router's top pick?
        # Normalize names: "zai_friend" == "friend", "zai_ours" == "ours"
        bk_norm = best_key_choice
        if bk_norm == "ours":
            bk_norm = "ours"
        elif bk_norm == "friend":
            bk_norm = "friend"

        top_norm = top_name
        if top_norm == "zai_ours":
            top_norm = "ours"
        elif top_norm == "zai_friend":
            top_norm = "friend"

        agreement = 1 if bk_norm == top_norm else 0

        # Serialize candidate list
        candidate_list = json.dumps([
            {
                "name": c.name,
                "model": c.model,
                "effective_cost": c.effective_cost if not math.isinf(c.effective_cost) else None,
                "reason": c.reason[:200] if c.reason else "",
            }
            for c in candidates
        ], default=str)

        conn = sqlite3.connect(db_path, timeout=5)
        conn.execute(_CREATE_FLAT_ROUTER_SHADOW_SQL)
        conn.execute(
            "INSERT INTO flat_router_shadow_decisions "
            "(ts, best_key_choice, flat_router_top, flat_router_top_cost, "
            " agreement, model, candidate_list) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (time.time(), best_key_choice, top_name, top_cost,
             agreement, model, candidate_list)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass  # Never raise


# ── Shadow hook — called from _proxy() after best_key() decision ────────────

def shadow_compare(best_key_choice: str | None, model: str | None) -> None:
    """Run select_provider() in shadow and log the comparison.

    Called after best_key() makes its decision. Runs select_provider()
    with the same model and logs what it WOULD have chosen alongside
    the best_key() pick. Never raises — shadow mode must not break production.
    """
    try:
        candidates = select_provider(model=model)
        _log_flat_router_shadow(
            best_key_choice=best_key_choice,
            candidates=candidates,
            model=model,
        )
    except Exception:
        pass  # Shadow mode never blocks production