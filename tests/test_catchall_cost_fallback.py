#!/usr/bin/env python3
"""test_catchall_cost_fallback.py — TDD tests for the catch-all cost fallback.

Bug: _extract_cost() has specific branches for ollama_cloud, opencode_go,
     telnyx, neuralwatt, ours, friend — but NOT for routstr, routstrd,
     deepinfra, ppq, openrouter. Those providers fall through to the final
     `return (None, None)` → api_calls.cost_usd stays NULL → invisible burn.

Fix: add a catch-all fallback before the final return that derives cost from
     the Kalman-measured rate (_rpt_rate) × total_tokens for ANY provider
     without a specific branch. Source = 'rate_derived_fallback'.

Run:  python3 -m pytest tests/test_catchall_cost_fallback.py -v
  or: python3 tests/test_catchall_cost_fallback.py
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.expanduser("~/.hermes/bot"))

import zai_proxy as z  # noqa: E402


class TestCatchallCostFallback(unittest.TestCase):
    """Verify _extract_cost() returns non-None cost for providers without
    a specific branch, using the Kalman-measured rate fallback."""

    # Providers that currently have NO specific branch in _extract_cost()
    # and previously fell through to (None, None) → invisible burn.
    FALLBACK_PROVIDERS = ["routstr", "routstrd", "deepinfra", "ppq", "openrouter"]

    def test_fallback_providers_return_non_none(self):
        """Each no-branch provider must yield a non-None cost_usd and the
        'rate_derived_fallback' source (given a sane measured rate)."""
        body = b'{"id":"x","object":"chat.completion","model":"glm-5.2",'
        body += b'"choices":[{"finish_reason":"stop"}],'
        body += b'"usage":{"prompt_tokens":800000,"completion_tokens":200000,'
        body += b'"total_tokens":1000000}}'

        for provider in self.FALLBACK_PROVIDERS:
            with self.subTest(provider=provider):
                # Make the Kalman-measured rate deterministic (e.g. $1.50/M)
                with patch.object(z, "_rpt_rate", return_value=1.50):
                    cost, source = z._extract_cost(provider, body, 1_000_000)

                self.assertIsNotNone(
                    cost, f"{provider}: cost_usd should NOT be None after fix"
                )
                self.assertEqual(
                    source, "rate_derived_fallback",
                    f"{provider}: expected rate_derived_fallback source",
                )
                # tokens / 1e6 * rate = 1.0M/1e6 * $1.50 = $1.50
                self.assertAlmostEqual(cost, 1.50, places=6,
                                       msg=f"{provider}: cost math is wrong")

    def test_fallback_zero_rate_returns_none(self):
        """If the measured rate is 0 / unknown, the fallback must bail to
        (None, None) rather than record a bogus $0 cost."""
        body = b'{"id":"x","usage":{"total_tokens":500000}}'
        for provider in self.FALLBACK_PROVIDERS:
            with self.subTest(provider=provider):
                with patch.object(z, "_rpt_rate", return_value=0.0):
                    cost, source = z._extract_cost(provider, body, 500_000)
                self.assertIsNone(cost, f"{provider}: zero rate must → None")
                self.assertIsNone(source)

    def test_fallback_inf_rate_returns_none(self):
        """An exhausted/infinite measured rate must also bail to None."""
        body = b'{"id":"x","usage":{"total_tokens":500000}}'
        with patch.object(z, "_rpt_rate", return_value=float("inf")):
            cost, source = z._extract_cost("routstr", body, 500_000)
        self.assertIsNone(cost, "infinite rate must → None")
        self.assertIsNone(source)

    def test_specific_branches_still_win(self):
        """Providers with dedicated branches must NOT be shadowed by the
        fallback (i.e. the fallback only fires for no-branch providers)."""
        # ours/friend → flat_rate $0
        for provider in ("ours", "friend"):
            with self.subTest(provider=provider):
                cost, source = z._extract_cost(provider, b"{}", 100)
                self.assertEqual(source, "flat_rate",
                                 f"{provider}: flat_rate branch must win")
        # ollama_cloud → 'estimated' regime-derivative branch
        with patch.object(z, "_rpt_rate", return_value=0.0155):
            cost, source = z._extract_cost(
                "ollama_cloud", b"{}", 1_000_000)
            # ollama branch uses _get_ollama_cloud_cost_per_1m, which for the
            # 'included' regime returns _rpt_rate("ollama_cloud") = 0.0155
            self.assertEqual(source, "estimated",
                             "ollama_cloud branch must win")


if __name__ == "__main__":
    unittest.main(verbosity=2)
