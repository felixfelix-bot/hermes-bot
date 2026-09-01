#!/usr/bin/env python3
"""test_ollama_pool_and_routstrd_cap.py — 2026-09-02 plan B1/B3 regression tests.

B1: _ollama_cloud_key_order() must order ollama keys by remaining quota
    (most remaining first) instead of the static registration order that
    drained key #1 at 2.3× its weekly pool while oc2/oc3 sat idle.

B3: _routstrd_daily_cap_tripped() must self-demote routstrd once its real
    metered spend for the UTC day exceeds ROUTSTRD_DAILY_CAP — runaway
    overflow catch-basin guard ($47.67 burned in 7 days during ollama flaps).

Run:  python3 -m pytest tests/test_ollama_pool_and_routstrd_cap.py -v
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import zai_proxy as z  # noqa: E402


def _status(session_pct=0.0, weekly_pct=0.0, monthly_pct=0.0,
            monthly_tokens=0, regime="included"):
    return {
        "regime": regime,
        "session_used_pct": session_pct,
        "weekly_used_pct": weekly_pct,
        "monthly_used_pct": monthly_pct,
        "session_tokens": 0,
        "weekly_tokens": 0,
        "monthly_tokens": monthly_tokens,
    }


class TestOllamaPoolOrder(unittest.TestCase):
    """Pool-weighted key ordering (plan B1)."""

    def _order_names(self, statuses, paywall=None):
        paywall = paywall or {}
        with patch.object(z, "_get_ollama_quota_status",
                          side_effect=lambda key: statuses[key]), \
             patch.object(z, "_ollama_paywall_active",
                          side_effect=lambda key: paywall.get(key, False)), \
             patch.object(z, "_OLLAMA_ORDER_TTL", 0.0):  # bypass cache
            z._ollama_order_cache.update({"order": None, "ts": 0.0})
            return [name for name, _key in z._ollama_cloud_key_order()]

    def test_most_remaining_first(self):
        """oc2 with the most remaining must be tried before oc."""
        statuses = {
            "ollama_cloud":   _status(weekly_pct=90.0),
            "ollama_cloud_2": _status(weekly_pct=10.0),
            "ollama_cloud_3": _status(monthly_pct=5.0, monthly_tokens=96_000_000),
        }
        self.assertEqual(
            self._order_names(statuses),
            ["ollama_cloud_2", "ollama_cloud_3", "ollama_cloud"])

    def test_exhausted_key_sinks_last(self):
        statuses = {
            "ollama_cloud":   _status(weekly_pct=100.0),
            "ollama_cloud_2": _status(weekly_pct=50.0),
            "ollama_cloud_3": _status(monthly_pct=95.0, monthly_tokens=96_000_000),
        }
        self.assertEqual(
            self._order_names(statuses)[0], "ollama_cloud_2")
        self.assertEqual(self._order_names(statuses)[-1], "ollama_cloud")

    def test_paywalled_key_sinks_last(self):
        """A paywalled key has zero effective remaining regardless of
        what the local token counting says (G2 paywall semantics)."""
        statuses = {
            "ollama_cloud":   _status(weekly_pct=0.0),
            "ollama_cloud_2": _status(weekly_pct=10.0),
            "ollama_cloud_3": _status(monthly_pct=5.0, monthly_tokens=96_000_000),
        }
        order = self._order_names(
            statuses, paywall={"ollama_cloud": True})
        self.assertEqual(order[-1], "ollama_cloud")

    def test_static_fallback_on_error(self):
        """Any quota-status error must fall back to registration order."""
        def boom(key):
            raise RuntimeError("tracker down")
        with patch.object(z, "_get_ollama_quota_status", side_effect=boom), \
             patch.object(z, "_OLLAMA_ORDER_TTL", 0.0):
            z._ollama_order_cache.update({"order": None, "ts": 0.0})
            order = z._ollama_cloud_key_order()
        self.assertEqual([n for n, _k in order],
                         [n for n, _k in z._OLLAMA_CLOUD_KEYS])

    def test_result_is_cached(self):
        """Consecutive calls inside the TTL must not re-query the tracker."""
        calls = []

        def counting_status(key):
            calls.append(key)
            return _status(weekly_pct=10.0)

        with patch.object(z, "_get_ollama_quota_status", side_effect=counting_status), \
             patch.object(z, "_ollama_paywall_active", return_value=False):
            z._ollama_order_cache.update({"order": None, "ts": 0.0})
            z._ollama_cloud_key_order()   # fresh — queries all keys
            first_count = len(calls)
            z._ollama_cloud_key_order()   # cached — no new queries
        self.assertEqual(len(calls), first_count)


class TestRoutstrdDailyCap(unittest.TestCase):
    """Metered-overflow sub-cap for routstrd (plan B3)."""

    class _FakeDB:
        def __init__(self, row):
            self._row = row

        def execute(self, *a, **k):
            return self

        def fetchone(self):
            return self._row

    def test_under_cap_not_tripped(self):
        with patch.object(z, "_usage_db", return_value=self._FakeDB((2.50,))):
            self.assertFalse(z._routstrd_daily_cap_tripped(cap=10.0))

    def test_over_cap_tripped(self):
        with patch.object(z, "_usage_db", return_value=self._FakeDB((12.30,))):
            self.assertTrue(z._routstrd_daily_cap_tripped(cap=10.0))

    def test_no_row_not_tripped(self):
        with patch.object(z, "_usage_db", return_value=self._FakeDB(None)):
            self.assertFalse(z._routstrd_daily_cap_tripped(cap=10.0))

    def test_db_error_not_tripped(self):
        with patch.object(z, "_usage_db", side_effect=RuntimeError("db")):
            self.assertFalse(z._routstrd_daily_cap_tripped(cap=10.0))


if __name__ == "__main__":
    unittest.main()
