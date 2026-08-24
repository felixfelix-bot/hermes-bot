#!/usr/bin/env python3
"""Tests for the 5-tier time-aware pricing model in flat_router.py.

Tests verify compute_effective_price() for each tier:
  T1 (quota):   time-decay + quota-health + peak + health
  T2 (balance): depletion penalty + correction factor
  T3 (flat):    MIN_EFFECTIVE_PRICE floor
  T4 (included): MIN_EFFECTIVE_PRICE floor
  T5 (per_token): base rate, no time decay
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


# ── T1: Quota-Based Providers (ours, friend) ───────────────────────────────

class TestTier1Quota:
    """Test T1 quota-based pricing: ours, friend."""

    def test_full_quota_full_week(self):
        """7 days to reset, 0% used → full price × peak × health."""
        base = 0.068
        result = compute_effective_price("ours", base, context={
            "time_decay": 1.0,
            "quota_health": 1.0,
            "peak_factor": 1.0,
            "health_factor": 1.0,
        })
        assert result == pytest.approx(base, rel=1e-6)

    def test_half_week_half_used(self):
        """3.5 days to reset, 50% used → base × 0.5 × 0.5 = 0.25 × base."""
        base = 0.068
        result = compute_effective_price("ours", base, context={
            "time_decay": 0.5,
            "quota_health": 0.5,
            "peak_factor": 1.0,
            "health_factor": 1.0,
        })
        assert result == pytest.approx(base * 0.25, rel=1e-6)

    def test_near_reset_low_usage(self):
        """1 day to reset, 20% used → aggressive discount."""
        base = 0.068
        time_decay = 1.0 / 7.0  # ~0.143
        quota_health = 0.8
        result = compute_effective_price("ours", base, context={
            "time_decay": time_decay,
            "quota_health": quota_health,
            "peak_factor": 1.0,
            "health_factor": 1.0,
        })
        expected = base * time_decay * quota_health
        assert result == pytest.approx(expected, rel=1e-6)

    def test_quota_exhausted_returns_inf(self):
        """100% used → quota_health = 0 → inf (unavailable)."""
        base = 0.068
        result = compute_effective_price("ours", base, context={
            "time_decay": 0.5,
            "quota_health": 0.0,
            "peak_factor": 1.0,
            "health_factor": 1.0,
        })
        assert math.isinf(result), "Exhausted quota should return inf"

    def test_health_zero_returns_inf(self):
        """health_factor = 0 → inf (unavailable)."""
        base = 0.068
        result = compute_effective_price("ours", base, context={
            "time_decay": 1.0,
            "quota_health": 1.0,
            "peak_factor": 1.0,
            "health_factor": 0.0,
        })
        assert math.isinf(result), "Zero health should return inf"

    def test_off_peak_reduces_price(self):
        """Off-peak factor 0.5 should halve the effective price."""
        base = 0.068
        result = compute_effective_price("ours", base, context={
            "time_decay": 1.0,
            "quota_health": 1.0,
            "peak_factor": 0.5,  # off-peak
            "health_factor": 1.0,
        })
        assert result == pytest.approx(base * 0.5, rel=1e-6)

    def test_friend_same_formula(self):
        """friend should use same formula as ours (different base rate)."""
        base = 0.082
        result = compute_effective_price("friend", base, context={
            "time_decay": 0.5,
            "quota_health": 0.5,
            "peak_factor": 1.0,
            "health_factor": 1.0,
        })
        assert result == pytest.approx(base * 0.25, rel=1e-6)

    def test_floor_applied(self):
        """Result should never go below MIN_EFFECTIVE_PRICE."""
        base = 0.068
        result = compute_effective_price("ours", base, context={
            "time_decay": 0.01,
            "quota_health": 0.01,
            "peak_factor": 0.5,
            "health_factor": 1.0,
        })
        assert result >= MIN_EFFECTIVE_PRICE


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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])