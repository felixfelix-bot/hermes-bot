#!/usr/bin/env python3
"""Tests for the 5-tier time-aware pricing model in flat_router.py.

Tests verify compute_effective_price() for each tier:
  T1 (quota):   SUNK COST model — $0.001 floor × time_decay × peak × health
                quota_health GATES availability but does NOT multiply price.
  T2 (balance): depletion penalty + correction factor
  T3 (flat):    MIN_EFFECTIVE_PRICE floor
  T4 (included): MIN_EFFECTIVE_PRICE floor
  T5 (per_token): base rate, no time decay

T1 corrected formula (Felix's insight):
  - z.ai quota is a SUNK COST (already paid for) → marginal cost = $0
  - effective = MIN_EFFECTIVE_PRICE × max(0.0001, days_to_reset/7) × peak × health
  - At 7 days:  $0.001 × 1.0 = $0.001 (same as T3/T4)
  - At 3 days:  $0.001 × 0.43 = $0.00043 (prefer z.ai over per-token)
  - At 1 day:   $0.001 × 0.14 = $0.00014 (strongly prefer z.ai)
  - At 100% used: inf (unavailable, failover)
  - After reset: back to $0.001 (fresh quota)
"""
import os
import sys
import time
import math
from unittest.mock import patch, MagicMock

import pytest

# ── Path setup ──────────────────────────────────────────────────────────────
BOT = os.path.expanduser("~/.hermes/bot")
MRE = os.path.expanduser("~/merchant-routing-engine")
for p in [BOT, MRE, os.path.join(MRE, "src")]:
    if p not in sys.path:
        sys.path.insert(0, p)

from flat_router import (
    compute_effective_price,
    PROVIDER_TIER,
    MIN_EFFECTIVE_PRICE,
    NW_CORRECTION_FACTOR,
    NW_MAX_DEPLETION_PENALTY,
    NW_INITIAL_BALANCE,
    QUOTA_WEEK_DAYS,
    OFF_PEAK_FACTOR,
    _compute_time_decay,
    _compute_quota_health,
    _compute_depletion_penalty,
    _get_off_peak_factor,
)


# ── T1: Quota-Based Providers (ours, friend) — SUNK COST MODEL ─────────────

class TestTier1QuotaSunkCost:
    """Test T1 quota-based pricing with the CORRECTED sunk-cost formula.

    effective = MIN_EFFECTIVE_PRICE × max(0.0001, time_decay) × peak × health
    quota_health GATES availability (inf) but does NOT multiply price.
    """

    def test_start_of_week_full_quota(self):
        """7 days to reset, 0% used → effective = $0.001 (same as T3/T4).

        At the start of the week, z.ai quota is fresh and available.
        The price is $0.001/M — same as flat-rate providers.
        This is CORRECT: z.ai is NOT expensive (sunk cost), but not preferential
        over other free providers either.
        """
        result = compute_effective_price("ours", 0.068, context={
            "time_decay": 1.0,      # 7 days / 7
            "quota_health": 1.0,    # 0% used (gates only, not multiplier)
            "peak_factor": 1.0,
            "health_factor": 1.0,
        })
        assert result == pytest.approx(MIN_EFFECTIVE_PRICE, rel=1e-6)

    def test_3_days_to_reset(self):
        """3 days to reset → effective = $0.001 × 0.43 = $0.00043.

        As reset approaches, z.ai becomes CHEAPER than T3/T4 ($0.001).
        This is the use-it-or-lose-it urgency: prefer z.ai over paying providers.
        """
        time_decay = 3.0 / 7.0  # ~0.4286
        result = compute_effective_price("ours", 0.068, context={
            "time_decay": time_decay,
            "quota_health": 1.0,
            "peak_factor": 1.0,
            "health_factor": 1.0,
        })
        expected = MIN_EFFECTIVE_PRICE * time_decay
        assert result == pytest.approx(expected, rel=1e-6)
        assert result < MIN_EFFECTIVE_PRICE, "3 days to reset should be cheaper than T3/T4"

    def test_1_day_to_reset(self):
        """1 day to reset → effective ≈ $0.00014 (strongly prefer z.ai)."""
        time_decay = 1.0 / 7.0  # ~0.1429
        result = compute_effective_price("ours", 0.068, context={
            "time_decay": time_decay,
            "quota_health": 1.0,
            "peak_factor": 1.0,
            "health_factor": 1.0,
        })
        expected = MIN_EFFECTIVE_PRICE * time_decay
        assert result == pytest.approx(expected, rel=1e-6)
        assert result < MIN_EFFECTIVE_PRICE * 0.2, "1 day to reset should be < 20% of floor"

    def test_1_hour_to_reset(self):
        """1 hour to reset → effective ≈ $0.000006 (aggressively burn remaining quota)."""
        time_decay = (1.0 / 24.0) / 7.0  # ~0.00595
        result = compute_effective_price("ours", 0.068, context={
            "time_decay": time_decay,
            "quota_health": 1.0,
            "peak_factor": 1.0,
            "health_factor": 1.0,
        })
        expected = MIN_EFFECTIVE_PRICE * time_decay
        assert result == pytest.approx(expected, rel=1e-4)
        assert result < 0.00001, "1 hour to reset should be near-zero"

    def test_after_reset_back_to_floor(self):
        """After reset (7 fresh days) → back to $0.001 (fresh quota)."""
        result = compute_effective_price("ours", 0.068, context={
            "time_decay": 1.0,      # 7 days to reset (fresh)
            "quota_health": 1.0,
            "peak_factor": 1.0,
            "health_factor": 1.0,
        })
        assert result == pytest.approx(MIN_EFFECTIVE_PRICE, rel=1e-6)

    def test_quota_exhausted_returns_inf(self):
        """100% used → quota_health = 0 → inf (unavailable)."""
        result = compute_effective_price("ours", 0.068, context={
            "time_decay": 0.5,
            "quota_health": 0.0,
            "peak_factor": 1.0,
            "health_factor": 1.0,
        })
        assert math.isinf(result), "Exhausted quota should return inf"

    def test_health_zero_returns_inf(self):
        """health_factor = 0 → inf (unavailable)."""
        result = compute_effective_price("ours", 0.068, context={
            "time_decay": 1.0,
            "quota_health": 1.0,
            "peak_factor": 1.0,
            "health_factor": 0.0,
        })
        assert math.isinf(result), "Zero health should return inf"

    def test_off_peak_reduces_price(self):
        """Off-peak factor 0.5 should halve the effective price."""
        result = compute_effective_price("ours", 0.068, context={
            "time_decay": 1.0,
            "quota_health": 1.0,
            "peak_factor": 0.5,
            "health_factor": 1.0,
        })
        assert result == pytest.approx(MIN_EFFECTIVE_PRICE * 0.5, rel=1e-6)

    def test_friend_same_formula(self):
        """friend should use same formula as ours (same floor, same decay)."""
        result = compute_effective_price("friend", 0.082, context={
            "time_decay": 0.5,
            "quota_health": 1.0,
            "peak_factor": 1.0,
            "health_factor": 1.0,
        })
        expected = MIN_EFFECTIVE_PRICE * 0.5
        assert result == pytest.approx(expected, rel=1e-6)

    def test_base_rate_irrelevant(self):
        """The base_rate should NOT affect T1 pricing (sunk cost = $0)."""
        r1 = compute_effective_price("ours", 0.001, context={
            "time_decay": 1.0, "quota_health": 1.0,
            "peak_factor": 1.0, "health_factor": 1.0,
        })
        r2 = compute_effective_price("ours", 999.0, context={
            "time_decay": 1.0, "quota_health": 1.0,
            "peak_factor": 1.0, "health_factor": 1.0,
        })
        assert r1 == pytest.approx(r2, rel=1e-6), "Base rate must not affect T1 price"

    def test_quota_health_does_not_penalize(self):
        """Using quota should NOT increase price (no conservation penalty).

        50% used and 0% used at same time_decay should have the SAME price.
        quota_health only gates availability (0 → inf), not price.
        """
        r_fresh = compute_effective_price("ours", 0.068, context={
            "time_decay": 0.5, "quota_health": 1.0,  # 0% used
            "peak_factor": 1.0, "health_factor": 1.0,
        })
        r_half = compute_effective_price("ours", 0.068, context={
            "time_decay": 0.5, "quota_health": 0.5,  # 50% used
            "peak_factor": 1.0, "health_factor": 1.0,
        })
        assert r_fresh == pytest.approx(r_half, rel=1e-6), \
            "Using quota must NOT penalize price (no conservation)"

    def test_cheaper_than_per_token_providers(self):
        """T1 at any time_decay < 1.0 should be cheaper than T5 per-token ($0.80+)."""
        result = compute_effective_price("ours", 0.068, context={
            "time_decay": 0.5, "quota_health": 1.0,
            "peak_factor": 1.0, "health_factor": 1.0,
        })
        ppq_cost = compute_effective_price("ppq", 0.80)
        assert result < ppq_cost, "z.ai should be cheaper than PPQ ($0.80/M)"

    def test_at_floor_same_as_t3_t4(self):
        """At start of week, T1 should cost the same as T3/T4 ($0.001)."""
        t1 = compute_effective_price("ours", 0.068, context={
            "time_decay": 1.0, "quota_health": 1.0,
            "peak_factor": 1.0, "health_factor": 1.0,
        })
        t3 = compute_effective_price("opencode_go", 0.43)
        t4 = compute_effective_price("ollama_cloud", 0.40)
        assert t1 == pytest.approx(t3, rel=1e-6)
        assert t1 == pytest.approx(t4, rel=1e-6)

    def test_decay_floor_prevents_zero(self):
        """Time-decay floor of 0.0001 should prevent true $0."""
        result = compute_effective_price("ours", 0.068, context={
            "time_decay": 0.0,  # 0 days to reset
            "quota_health": 1.0,
            "peak_factor": 1.0,
            "health_factor": 1.0,
        })
        assert result > 0, "Effective price must never be exactly $0"
        assert result == pytest.approx(MIN_EFFECTIVE_PRICE * 0.0001, rel=1e-6)

    def test_kill_switch_returns_floor(self):
        """Kill switch (time-decay disabled) should return $0.001 floor."""
        with patch("flat_router._is_time_decay_disabled", return_value=True):
            result = compute_effective_price("ours", 0.068, context={
                "peak_factor": 1.0,
                "health_factor": 1.0,
            })
            assert result == pytest.approx(MIN_EFFECTIVE_PRICE, rel=1e-6)

    def test_kill_switch_health_zero(self):
        """Kill switch with health=0 should return inf."""
        with patch("flat_router._is_time_decay_disabled", return_value=True):
            result = compute_effective_price("ours", 0.068, context={
                "peak_factor": 1.0,
                "health_factor": 0.0,
            })
            assert math.isinf(result)


# ── T2: Balance-Based Provider (neuralwatt) ────────────────────────────────

class TestTier2Balance:
    """Test T2 balance-based pricing: neuralwatt."""

    def test_full_balance(self):
        """100% balance → depletion_penalty = 0 → base × 1.0 × correction."""
        base = 2.21
        result = compute_effective_price("neuralwatt", base, context={
            "depletion_penalty": 0.0,
        })
        expected = base * 1.0 * NW_CORRECTION_FACTOR
        assert result == pytest.approx(expected, rel=1e-6)

    def test_half_balance(self):
        """50% balance → depletion_penalty = 1.0 → base × 2.0 × correction."""
        base = 2.21
        result = compute_effective_price("neuralwatt", base, context={
            "depletion_penalty": 1.0,
        })
        expected = base * 2.0 * NW_CORRECTION_FACTOR
        assert result == pytest.approx(expected, rel=1e-6)

    def test_zero_balance(self):
        """0% balance → depletion_penalty = 2.0 → base × 3.0 × correction."""
        base = 2.21
        result = compute_effective_price("neuralwatt", base, context={
            "depletion_penalty": 2.0,
        })
        expected = base * 3.0 * NW_CORRECTION_FACTOR
        assert result == pytest.approx(expected, rel=1e-6)

    def test_correction_factor_applied(self):
        """The 0.2762 correction factor should always be applied."""
        base = 1.0
        result = compute_effective_price("neuralwatt", base, context={
            "depletion_penalty": 0.0,
        })
        assert result == pytest.approx(NW_CORRECTION_FACTOR, rel=1e-6)

    def test_corrected_rate_cheaper_than_uncorrected(self):
        """With correction, NeuralWatt at full balance should be cheaper than base."""
        base = 2.21
        result = compute_effective_price("neuralwatt", base, context={
            "depletion_penalty": 0.0,
        })
        assert result < base, "Correction factor should make it cheaper than uncorrected rate"

    def test_zero_balance_more_expensive_than_full(self):
        """At 0 balance, price should be 3× the full-balance price."""
        base = 2.21
        full = compute_effective_price("neuralwatt", base, context={
            "depletion_penalty": 0.0,
        })
        empty = compute_effective_price("neuralwatt", base, context={
            "depletion_penalty": 2.0,
        })
        assert empty == pytest.approx(3.0 * full, rel=1e-6)


# ── T3: Flat-Rate Provider (opencode_go) ───────────────────────────────────

class TestTier3Flat:
    """Test T3 flat-rate pricing: opencode_go."""

    def test_returns_min_effective_price(self):
        """opencode_go should always return MIN_EFFECTIVE_PRICE."""
        result = compute_effective_price("opencode_go", 0.43)
        assert result == MIN_EFFECTIVE_PRICE

    def test_ignores_base_rate(self):
        """Even with a high base rate, flat-rate returns floor."""
        result = compute_effective_price("opencode_go", 999.0)
        assert result == MIN_EFFECTIVE_PRICE

    def test_ignores_context(self):
        """Context should have no effect on flat-rate pricing."""
        result = compute_effective_price("opencode_go", 0.43, context={
            "time_decay": 0.5,
            "quota_health": 0.5,
            "depletion_penalty": 2.0,
        })
        assert result == MIN_EFFECTIVE_PRICE

    def test_not_zero(self):
        """Price should not be $0 (avoid always-wins edge case)."""
        assert compute_effective_price("opencode_go", 0.0) > 0


# ── T4: Included Providers (ollama_cloud, ollama_cloud_2) ──────────────────

class TestTier4Included:
    """Test T4 included pricing: ollama_cloud, ollama_cloud_2."""

    def test_ollama_cloud_returns_floor(self):
        """ollama_cloud should return MIN_EFFECTIVE_PRICE."""
        result = compute_effective_price("ollama_cloud", 0.40)
        assert result == MIN_EFFECTIVE_PRICE

    def test_ollama_cloud_2_returns_floor(self):
        """ollama_cloud_2 should return MIN_EFFECTIVE_PRICE."""
        result = compute_effective_price("ollama_cloud_2", 0.40)
        assert result == MIN_EFFECTIVE_PRICE

    def test_ignores_base_rate(self):
        """Even with high base rate, included returns floor."""
        result = compute_effective_price("ollama_cloud", 999.0)
        assert result == MIN_EFFECTIVE_PRICE


# ── T5: Per-Token Providers ────────────────────────────────────────────────

class TestTier5PerToken:
    """Test T5 per-token pricing: deepinfra, ppq, telnyx, openrouter, routstr, routstrd."""

    @pytest.mark.parametrize("provider", ["deepinfra", "ppq", "telnyx", "openrouter", "routstr", "routstrd"])
    def test_returns_base_rate(self, provider):
        """Per-token providers should return the base rate (Kalman-measured)."""
        base = 1.50
        result = compute_effective_price(provider, base)
        assert result == pytest.approx(base, rel=1e-6)

    def test_no_time_decay(self):
        """Per-token providers should NOT have time decay applied."""
        base = 1.50
        result = compute_effective_price("ppq", base, context={
            "time_decay": 0.1,  # should be ignored for T5
            "quota_health": 0.1,  # should be ignored
        })
        assert result == pytest.approx(base, rel=1e-6)

    def test_floor_applied(self):
        """Per-token providers should have MIN_EFFECTIVE_PRICE floor."""
        result = compute_effective_price("ppq", 0.0001)
        assert result >= MIN_EFFECTIVE_PRICE


# ── PROVIDER_TIER registry tests ───────────────────────────────────────────

class TestProviderTierRegistry:
    """Verify PROVIDER_TIER mapping is correct and complete."""

    def test_all_providers_classified(self):
        """Every provider in PROVIDER_MODELS should be in PROVIDER_TIER."""
        from flat_router import PROVIDER_MODELS
        for name in PROVIDER_MODELS:
            assert name in PROVIDER_TIER, f"Provider '{name}' missing from PROVIDER_TIER"

    def test_ours_is_quota(self):
        assert PROVIDER_TIER["ours"] == "quota"

    def test_friend_is_quota(self):
        assert PROVIDER_TIER["friend"] == "quota"

    def test_neuralwatt_is_balance(self):
        assert PROVIDER_TIER["neuralwatt"] == "balance"

    def test_opencode_go_is_flat(self):
        assert PROVIDER_TIER["opencode_go"] == "flat"

    def test_ollama_cloud_is_included(self):
        assert PROVIDER_TIER["ollama_cloud"] == "included"

    def test_ollama_cloud_2_is_included(self):
        assert PROVIDER_TIER["ollama_cloud_2"] == "included"

    @pytest.mark.parametrize("provider", ["deepinfra", "ppq", "telnyx", "openrouter", "routstr", "routstrd"])
    def test_per_token_providers(self, provider):
        assert PROVIDER_TIER[provider] == "per_token"


# ── Helper function tests ──────────────────────────────────────────────────

class TestComputeTimeDecay:
    """Test _compute_time_decay() with mocked quota windows."""

    def test_7_days_to_reset(self):
        """7 days to reset → time_decay = 1.0."""
        resets_at = time.time() + 7 * 86400
        with patch("flat_router._get_quota_windows", return_value=[
            {"name": "weekly", "used_pct": 0, "resets_at": resets_at, "window_hours": 168}
        ]):
            assert _compute_time_decay("ours") == pytest.approx(1.0, rel=0.01)

    def test_3_days_to_reset(self):
        """3 days to reset → time_decay ≈ 0.43."""
        resets_at = time.time() + 3 * 86400
        with patch("flat_router._get_quota_windows", return_value=[
            {"name": "weekly", "used_pct": 0, "resets_at": resets_at, "window_hours": 168}
        ]):
            assert _compute_time_decay("ours") == pytest.approx(3.0/7.0, rel=0.01)

    def test_1_day_to_reset(self):
        """1 day to reset → time_decay ≈ 0.14."""
        resets_at = time.time() + 1 * 86400
        with patch("flat_router._get_quota_windows", return_value=[
            {"name": "weekly", "used_pct": 0, "resets_at": resets_at, "window_hours": 168}
        ]):
            assert _compute_time_decay("ours") == pytest.approx(1.0/7.0, rel=0.01)

    def test_no_reset_time(self):
        """No resets_at → time_decay = 1.0 (safe default)."""
        with patch("flat_router._get_quota_windows", return_value=[
            {"name": "weekly", "used_pct": 0, "resets_at": 0, "window_hours": 168}
        ]):
            assert _compute_time_decay("ours") == 1.0

    def test_empty_windows(self):
        """No windows → time_decay = 1.0."""
        with patch("flat_router._get_quota_windows", return_value=[]):
            assert _compute_time_decay("ours") == 1.0

    def test_minimum_floor(self):
        """time_decay should never go below 0.01."""
        resets_at = time.time() + 1  # 1 second to reset
        with patch("flat_router._get_quota_windows", return_value=[
            {"name": "weekly", "used_pct": 0, "resets_at": resets_at, "window_hours": 168}
        ]):
            assert _compute_time_decay("ours") >= 0.01


class TestComputeQuotaHealth:
    """Test _compute_quota_health() with mocked quota windows."""

    def test_zero_used(self):
        """0% used → quota_health = 1.0."""
        with patch("flat_router._get_quota_windows", return_value=[
            {"name": "weekly", "used_pct": 0, "resets_at": 0, "window_hours": 168}
        ]):
            assert _compute_quota_health("ours") == 1.0

    def test_50_used(self):
        """50% used → quota_health = 0.5."""
        with patch("flat_router._get_quota_windows", return_value=[
            {"name": "weekly", "used_pct": 50, "resets_at": 0, "window_hours": 168}
        ]):
            assert _compute_quota_health("ours") == 0.5

    def test_100_used(self):
        """100% used → quota_health = 0.0."""
        with patch("flat_router._get_quota_windows", return_value=[
            {"name": "weekly", "used_pct": 100, "resets_at": 0, "window_hours": 168}
        ]):
            assert _compute_quota_health("ours") == 0.0

    def test_empty_windows(self):
        """No windows → quota_health = 1.0 (optimistic)."""
        with patch("flat_router._get_quota_windows", return_value=[]):
            assert _compute_quota_health("ours") == 1.0


class TestComputeDepletionPenalty:
    """Test _compute_depletion_penalty() with mocked NeuralWatt balance."""

    def test_full_balance(self):
        """100% balance → penalty = 0.0."""
        mock_snap = MagicMock(return_value={
            "remaining_usd": 100.0,
            "total_credits_usd": 100.0,
        })
        with patch("flat_router._resolve", return_value=mock_snap), \
             patch("flat_router._is_depletion_disabled", return_value=False):
            assert _compute_depletion_penalty("neuralwatt") == 0.0

    def test_half_balance(self):
        """50% balance → penalty = 1.0."""
        mock_snap = MagicMock(return_value={
            "remaining_usd": 50.0,
            "total_credits_usd": 100.0,
        })
        with patch("flat_router._resolve", return_value=mock_snap), \
             patch("flat_router._is_depletion_disabled", return_value=False):
            assert _compute_depletion_penalty("neuralwatt") == pytest.approx(1.0, rel=1e-6)

    def test_zero_balance(self):
        """0% balance → penalty = 2.0 (max)."""
        mock_snap = MagicMock(return_value={
            "remaining_usd": 0.0,
            "total_credits_usd": 100.0,
        })
        with patch("flat_router._resolve", return_value=mock_snap), \
             patch("flat_router._is_depletion_disabled", return_value=False):
            assert _compute_depletion_penalty("neuralwatt") == pytest.approx(2.0, rel=1e-6)

    def test_bridge_disabled(self):
        """Bridge disabled → max penalty (conservative)."""
        with patch("flat_router._resolve", return_value=None), \
             patch("flat_router._is_depletion_disabled", return_value=False):
            assert _compute_depletion_penalty("neuralwatt") == NW_MAX_DEPLETION_PENALTY

    def test_kill_switch(self):
        """Kill switch active → penalty = 0.0."""
        with patch("flat_router._is_depletion_disabled", return_value=True):
            assert _compute_depletion_penalty("neuralwatt") == 0.0


# ── Integration: _get_effective_cost uses tier formula ─────────────────────

class TestGetEffectiveCostWithTiers:
    """Verify _get_effective_cost() applies tier formulas."""

    def test_flat_rate_provider_returns_floor(self):
        """opencode_go should return MIN_EFFECTIVE_PRICE from _get_effective_cost."""
        from flat_router import _get_effective_cost
        with patch("flat_router._is_manually_disabled", return_value=False), \
             patch("flat_router._is_key_healthy", return_value=True), \
             patch("flat_router._is_provider_funded", return_value=True):
            cost = _get_effective_cost("opencode_go", "glm-5.2")
            # Should be MIN_EFFECTIVE_PRICE (via seed rate fallback or shadow opt)
            assert cost == MIN_EFFECTIVE_PRICE

    def test_included_provider_returns_floor(self):
        """ollama_cloud should return MIN_EFFECTIVE_PRICE."""
        from flat_router import _get_effective_cost
        with patch("flat_router._is_manually_disabled", return_value=False), \
             patch("flat_router._is_key_healthy", return_value=True), \
             patch("flat_router._is_provider_funded", return_value=True):
            cost = _get_effective_cost("ollama_cloud", "glm-5.2")
            assert cost == MIN_EFFECTIVE_PRICE

    def test_unknown_provider_defaults_to_per_token(self):
        """Unknown provider should default to per-token tier."""
        result = compute_effective_price("unknown_provider", 1.5)
        assert result == pytest.approx(1.5, rel=1e-6)


# ── T1 Sunk-Cost Model: Before/After comparison tests ──────────────────────

class TestTier1SunkCostComparison:
    """Verify the corrected T1 formula matches Felix's intent.

    OLD (WRONG): base_rate × time_decay × quota_health × peak × health
        At start of week: 0.068 × 1.0 × 1.0 = $0.068/M (EXPENSIVE — router avoids z.ai!)

    NEW (CORRECT): MIN_EFFECTIVE_PRICE × max(0.0001, time_decay) × peak × health
        At start of week: 0.001 × 1.0 = $0.001/M (CHEAP — same as T3/T4, sunk cost)
    """

    def test_old_formula_was_expensive_at_start_of_week(self):
        """OLD formula: base × 1.0 × 1.0 = $0.068/M — MORE expensive than PPQ ($0.80).

        This test documents WHY the old formula was wrong:
        z.ai at $0.068/M would lose to opencode_go ($0.001) AND be close to PPQ ($0.80).
        The router would avoid z.ai at the start of the week — backwards!
        """
        old_formula = 0.068 * 1.0 * 1.0 * 1.0 * 1.0  # base × decay × health × peak × health
        new_formula = MIN_EFFECTIVE_PRICE * 1.0 * 1.0 * 1.0  # floor × decay × peak × health
        assert old_formula > new_formula, "New formula should be cheaper at start of week"
        assert old_formula > MIN_EFFECTIVE_PRICE, "Old formula was more expensive than T3/T4 floor"

    def test_new_formula_cheaper_than_per_token(self):
        """NEW formula: z.ai is ALWAYS cheaper than per-token providers when available."""
        # Even at start of week (time_decay = 1.0)
        zai_cost = compute_effective_price("ours", 0.068, context={
            "time_decay": 1.0, "quota_health": 1.0,
            "peak_factor": 1.0, "health_factor": 1.0,
        })
        ppq_cost = compute_effective_price("ppq", 0.80)
        assert zai_cost < ppq_cost, "z.ai should always be cheaper than PPQ"

    def test_new_formula_uses_001_floor_not_base_rate(self):
        """The corrected formula uses $0.001 as the base, NOT the Kalman base_rate."""
        # With time_decay=1.0, effective should be exactly $0.001 regardless of base_rate
        for base in [0.001, 0.068, 1.0, 100.0]:
            result = compute_effective_price("ours", base, context={
                "time_decay": 1.0, "quota_health": 1.0,
                "peak_factor": 1.0, "health_factor": 1.0,
            })
            assert result == pytest.approx(MIN_EFFECTIVE_PRICE, rel=1e-6), \
                f"base_rate={base} should not affect T1 price"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])