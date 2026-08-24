#!/usr/bin/env python3
"""test_cost_attribution.py — TDD tests for cost_usd never-NULL guarantee.

Bug: _log_api_call() accepts cost_usd=None and inserts NULL into api_calls.
     When _extract_cost() returns (None, None) (e.g. rate unavailable, parse
     failure, or provider without a branch), the row is logged with NULL cost
     → invisible burn in daily_spend, broken EWMA baselines, misleading alerts.

     4 providers had NULL cost_usd:
       ollama_cloud_2: 3035 rows, 170M tokens invisible
       neuralwatt:     2338 rows, 170M tokens invisible
       ppq:             519 rows,  12M tokens invisible
       opencode_go:     160 rows,  15M tokens invisible

Fix: _log_api_call() now applies a safety net — if cost_usd is None and
     key_name is a known provider, it computes an estimated cost via
     _estimate_cost_usd() before inserting. This ensures cost_usd is NEVER
     NULL for any known provider, regardless of upstream _extract_cost() gaps.

Run:  python3 -m pytest tests/test_cost_attribution.py -v
  or: python3 tests/test_cost_attribution.py
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.expanduser("~/.hermes/bot"))

import zai_proxy as z  # noqa: E402


class TestCostAttributionNeverNull(unittest.TestCase):
    """Verify _log_api_call() NEVER inserts NULL cost_usd for known providers."""

    # All providers that should always get a cost_usd value.
    KNOWN_PROVIDERS = [
        "ours", "friend",
        "ollama_cloud", "ollama_cloud_2",
        "opencode_go", "neuralwatt",
        "ppq", "openrouter", "deepinfra",
        "telnyx", "routstr", "routstrd",
    ]

    def setUp(self):
        """Mock the DB so _log_api_call doesn't actually write."""
        # Patch _usage_db to return a mock that captures execute calls
        self._mock_db = MagicMock()
        self._mock_cursor = MagicMock()
        self._mock_db.execute.return_value = self._mock_cursor
        self._patch_db = patch.object(z, "_usage_db", return_value=self._mock_db)
        self._patch_db.start()

    def tearDown(self):
        self._patch_db.stop()

    def _get_inserted_cost(self):
        """Extract the cost_usd value from the INSERT call."""
        # _log_api_call calls _usage_db().execute(sql, params)
        # params is a tuple; cost_usd is at index 13 (0-indexed) in the full insert
        call_args = self._mock_db.execute.call_args
        if call_args is None:
            return None
        args, kwargs = call_args
        # args[0] is SQL, args[1] is the params tuple
        if len(args) < 2:
            return None
        params = args[1]
        # Find the INSERT statement and get cost_usd from params
        sql = args[0]
        if "INSERT" in sql and "cost_usd" in sql:
            # In the full insert: (ts, key_name, key_suffix, model, prompt_tokens,
            #   completion_tokens, total_tokens, tier, cache_hit, ollama_hit, ppq_hit,
            #   status_code, error, duration_ms, cost_usd, cost_source, session_id, task_type)
            # cost_usd is at index 13 (0-indexed)
            if len(params) > 14:
                return params[14]  # cost_usd
            # Fallback insert without task_type
            if len(params) > 13:
                return params[13]
        return None

    def test_known_providers_never_get_null_cost(self):
        """For each known provider, _log_api_call with cost_usd=None must
        compute an estimated cost and insert it instead of NULL."""
        for provider in self.KNOWN_PROVIDERS:
            with self.subTest(provider=provider):
                # Reset mock between iterations
                self._mock_db.reset_mock()

                # Call _log_api_call with cost_usd=None (simulating _extract_cost failure)
                z._log_api_call(
                    key_name=provider,
                    key_suffix="test",
                    model="glm-5.2",
                    prompt_tokens=10000,
                    completion_tokens=500,
                    total_tokens=10500,
                    tier=provider,
                    status_code=200,
                    cost_usd=None,  # This is the key: None should trigger fallback
                    cost_source=None,
                )

                cost = self._get_inserted_cost()
                self.assertIsNotNone(
                    cost,
                    f"{provider}: cost_usd is NULL — safety net failed to estimate"
                )
                self.assertGreaterEqual(
                    cost, 0.0,
                    f"{provider}: cost_usd is negative ({cost})"
                )

    def test_explicit_cost_is_preserved(self):
        """When cost_usd is explicitly provided, it must be used as-is."""
        self._mock_db.reset_mock()
        z._log_api_call(
            key_name="ollama_cloud",
            model="glm-5.2",
            total_tokens=1000,
            cost_usd=0.05,
            cost_source="measured",
        )
        cost = self._get_inserted_cost()
        self.assertEqual(cost, 0.05, "Explicit cost_usd was not preserved")

    def test_zero_cost_is_preserved(self):
        """Zero cost (flat-rate providers) must stay 0, not be overwritten."""
        self._mock_db.reset_mock()
        z._log_api_call(
            key_name="ours",
            model="glm-5.2",
            total_tokens=1000,
            cost_usd=0.0,
            cost_source="flat_rate",
        )
        cost = self._get_inserted_cost()
        self.assertEqual(cost, 0.0, "Zero cost was modified by safety net")

    def test_unknown_provider_stays_null(self):
        """Unknown/None provider with no cost should remain NULL (we can't
        estimate what we don't know)."""
        self._mock_db.reset_mock()
        z._log_api_call(
            key_name=None,
            model="unknown",
            total_tokens=1000,
            cost_usd=None,
        )
        cost = self._get_inserted_cost()
        self.assertIsNone(cost, "Unknown provider should stay NULL")

    def test_zero_tokens_get_zero_cost(self):
        """Zero tokens should result in 0.0 cost, not NULL."""
        self._mock_db.reset_mock()
        z._log_api_call(
            key_name="neuralwatt",
            model="glm-5.2",
            total_tokens=0,
            cost_usd=None,
        )
        cost = self._get_inserted_cost()
        self.assertIsNotNone(cost, "Zero-token call got NULL cost")
        self.assertEqual(cost, 0.0, "Zero-token call should have $0 cost")


if __name__ == "__main__":
    unittest.main(verbosity=2)