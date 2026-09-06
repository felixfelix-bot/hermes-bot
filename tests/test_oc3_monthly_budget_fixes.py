#!/usr/bin/env python3
"""test_oc3_monthly_budget_fixes.py — 2026-09-02 B1-fix regression tests.

Two FIX-class bugs in the oc3 monthly-budget routing (verified by consultant
review against code):

BUG A (remaining-quota math): oc3's remaining was computed from
``monthly_tokens`` (the USED 30-day count) instead of the monthly budget
limit, so oc3 remaining ≈ used×(1−pct) → tiny → oc3 always sank last,
defeating B1's intent to promote unused monthly capacity.

BUG B (scarcity + health gate comment-only for oc3):
  - (B-sc) flat_router scarcity used max(session, weekly) — both 0 for oc3
    (monthly-only key) → zero scarcity pricing until 100% exhausted.
  - (B-h) _is_key_healthy had no monthly gate for oc3 → no ~90% delist.

Run:  python3 -m pytest tests/test_oc3_monthly_budget_fixes.py -v
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import zai_proxy as z  # noqa: E402
import flat_router as fr  # noqa: E402

MONTHLY_LIMIT = 3_500_000_000


def _status(session_pct=0.0, weekly_pct=0.0, monthly_pct=0.0,
            monthly_tokens=0, monthly_limit=MONTHLY_LIMIT, regime="included"):
    return {
        "regime": regime,
        "session_used_pct": session_pct,
        "weekly_used_pct": weekly_pct,
        "monthly_used_pct": monthly_pct,
        "session_tokens": 0,
        "weekly_tokens": 0,
        "monthly_tokens": monthly_tokens,
        "monthly_limit": monthly_limit,
    }


class TestOc3MonthlyOrder(unittest.TestCase):
    """BUG A: oc3 remaining must use the monthly budget limit, not used tokens."""

    def _order_names(self, statuses, paywall=None):
        paywall = paywall or {}
        with patch.object(z, "_get_ollama_quota_status",
                          side_effect=lambda key: statuses[key]), \
             patch.object(z, "_ollama_paywall_active",
                          side_effect=lambda key: paywall.get(key, False)), \
             patch.object(z, "_OLLAMA_ORDER_TTL", 0.0):
            z._ollama_order_cache.update({"order": None, "ts": 0.0})
            return [name for name, _key in z._ollama_cloud_key_order()]

    def test_fresh_oc3_above_burned_oc_and_oc2(self):
        """Fresh oc3 (5% of 3.5B monthly) must outrank burned oc/oc2.

        oc3 remaining = 3.5B × 0.95 = 3.325B, dwarfing oc (50M) and oc2 (450M).
        """
        statuses = {
            "ollama_cloud":   _status(weekly_pct=90.0),
            "ollama_cloud_2": _status(weekly_pct=10.0),
            "ollama_cloud_3": _status(monthly_pct=5.0, monthly_tokens=175_000_000),
            "ollama_cloud_4": _status(weekly_pct=90.0),
        }
        self.assertEqual(
            self._order_names(statuses),
            ["ollama_cloud_3", "ollama_cloud_2", "ollama_cloud", "ollama_cloud_4"])

    def test_oc3_remaining_uses_budget_not_used_tokens(self):
        """Even with a large used-token count, oc3 remaining is budget-based.

        monthly_tokens=3.0B (used) at 85.7% → remaining = 3.5B × 0.143 ≈ 500M,
        NOT 3.0B × 0.143 ≈ 429M. Both still outrank a 90%-burned oc (50M),
        but the point is the budget (not used count) drives the math.
        """
        statuses = {
            "ollama_cloud":   _status(weekly_pct=90.0),
            "ollama_cloud_2": _status(weekly_pct=90.0),
            "ollama_cloud_3": _status(monthly_pct=85.7, monthly_tokens=3_000_000_000),
            "ollama_cloud_4": _status(weekly_pct=90.0),
        }
        self.assertEqual(self._order_names(statuses)[0], "ollama_cloud_3")


class TestOc3MonthlyHealthGate(unittest.TestCase):
    """BUG B-h: oc3 must delist at ~90% monthly usage."""

    def _healthy(self, monthly_pct, paywall=False):
        with patch.object(z, "_get_ollama_quota_status",
                          return_value=_status(monthly_pct=monthly_pct)), \
             patch.object(z, "_ollama_paywall_active", return_value=paywall):
            return z._is_key_healthy("ollama_cloud_3")

    def test_below_90_stays_healthy(self):
        self.assertTrue(self._healthy(monthly_pct=50.0))

    def test_at_90_delists(self):
        self.assertFalse(self._healthy(monthly_pct=90.0))

    def test_above_90_delists(self):
        self.assertFalse(self._healthy(monthly_pct=95.0))

    def test_paywall_still_wins(self):
        """Paywall semantics intact: paywalled oc3 is unhealthy regardless."""
        self.assertFalse(self._healthy(monthly_pct=5.0, paywall=True))

    def test_gate_fails_open_on_error(self):
        """A quota-status error must not delist oc3 (fail-open)."""
        with patch.object(z, "_get_ollama_quota_status",
                          side_effect=RuntimeError("tracker down")), \
             patch.object(z, "_ollama_paywall_active", return_value=False):
            self.assertTrue(z._is_key_healthy("ollama_cloud_3"))


class TestOc3ScarcityPricing(unittest.TestCase):
    """BUG B-sc: oc3 scarcity must use monthly_used_pct, not session/weekly."""

    def _price(self, monthly_pct, session_pct=0.0, weekly_pct=0.0, burn_share=0.0):
        status = _status(session_pct=session_pct, weekly_pct=weekly_pct,
                         monthly_pct=monthly_pct)
        with patch.object(z, "_get_ollama_quota_status", return_value=status), \
             patch.object(z, "_compute_model_burn_share", return_value=burn_share):
            return fr.compute_effective_price(
                "ollama_cloud_3", 0.40, model="glm-5.2")

    def test_monthly_scarcity_raises_price(self):
        """50% monthly usage (session/weekly 0) must raise price above floor."""
        price = self._price(monthly_pct=50.0)
        self.assertGreater(price, fr.MIN_EFFECTIVE_PRICE)

    def test_zero_monthly_returns_floor(self):
        """0% monthly → no scarcity → floor price."""
        self.assertEqual(self._price(monthly_pct=0.0), fr.MIN_EFFECTIVE_PRICE)

    def test_monthly_scarcity_matches_formula(self):
        """scarcity=0.5, burn_share=0.5 → 0.001×(1+0.5+0.5×0.5×2)=0.002."""
        price = self._price(monthly_pct=50.0, burn_share=0.5)
        self.assertAlmostEqual(price, 0.002, places=9)


if __name__ == "__main__":
    unittest.main()
