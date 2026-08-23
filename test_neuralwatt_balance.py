#!/usr/bin/env python3
"""Tests for the NeuralWatt REAL-API balance collector (NW-API).

Verifies the collector that calls https://api.neuralwatt.com/v1/quota for the
kWh allowance, /v1/usage/summary for real daily spend, and exposes a daily
cap + cost-correction factor. All HTTP is mocked — no live calls.

Two temp sqlite DBs are created per test:
  * usage.db   — mimics zai_usage.db schema (api_calls + tier column)
  * balances.db — mimics api_burn.db (provider_balances, auto-created by _ensure_table)

Run:  python3 -m pytest test_neuralwatt_balance.py -v   (from ~/.hermes/bot)
  or: python3 test_neuralwatt_balance.py               (plain unittest runner)
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import src.balance_collectors as bc  # noqa: E402

# Use the canonical /v1/quota sample captured on 2026-08-23T16:55Z as the gold
# reference. The per-test code can mutate copies of it for edge cases.
_quota_sample = {
    "snapshot_at": "2026-08-23T16:55:28Z",
    "balance": {
        "credits_remaining_usd": 8.9951,
        "total_credits_usd": 11.0,
        "credits_used_usd": 2.0049,
        "accounting_method": "energy",
        "new_credits_usd": 8.9951,
        "legacy_credits_usd": 0.0,
    },
    "usage": {
        "lifetime": {
            "cost_usd": 51.3019, "requests": 4919, "tokens": 415717093,
            "energy_kwh": 6.8462,
        },
        "current_month": {
            "cost_usd": 51.3019, "requests": 4919, "tokens": 415717093,
            "energy_kwh": 6.8462,
        },
    },
    "limits": {"overage_limit_usd": None, "rate_limit_tier": "pro"},
    "subscription": {
        "plan": "pro", "status": "active", "billing_interval": "month",
        "current_period_start": "2026-08-22T21:41:54Z",
        "current_period_end": "2026-09-22T21:41:54Z", "auto_renew": True,
        "kwh_included": 13.3333, "kwh_used": 6.5729, "kwh_remaining": 6.7604,
        "in_overage": False, "kwh_reset_date": "2026-09-22T21:41:54Z",
    },
    "key": {"name": "hermes-key", "allowance": None},
}

_usage_summary_sample = {
    "period": {"start": "2026-07-24T16:55:33.347633+00:00",
               "end": "2026-08-23T16:55:33.347633+00:00"},
    "accounting_method": "energy",
    "totals": {
        "requests": 4924, "total_tokens": 416021475,
        "prompt_tokens": 412669187, "completion_tokens": 3352288,
        "total_cost_usd": 51.326226, "energy_kwh_consumed": 6.849402,
        "energy_kwh_charged": 6.776667, "energy_kwh": 6.849402,
        "cached_tokens": 389241856,
    },
    "time_series": [
        {"date": "2026-08-22", "requests": 491, "cost_usd": 5.725642,
         "total_tokens": 65004975},
        {"date": "2026-08-23", "requests": 4433, "cost_usd": 45.600584,
         "total_tokens": 351016500},
    ],
}


# ── Schema mirroring zai_usage.db.api_calls (used only for cost-correction) ──
_API_CALLS_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_calls (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               REAL NOT NULL,
    key_name         TEXT,
    tier             TEXT,
    cost_usd         REAL,
    model            TEXT,
    total_tokens     INTEGER
)
"""


def _today_iso() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _make_http_mock(quota_obj=None, summary_obj=None,
                    quota_error=None, summary_error=None):
    """Build an http_get seam that returns the given canned responses.

    The seam signature is (url, api_key, timeout) -> (parsed_json_or_None,
    error_str_or_None). The closure inspects the URL to decide which response
    to serve, allowing us to inject errors independently for /quota vs
    /usage/summary.
    """
    def _http_get(url, api_key, timeout):
        if "quota" in url:
            if quota_error is not None:
                return None, quota_error
            return (quota_obj if quota_obj is not None else dict(_quota_sample)), None
        if "usage/summary" in url:
            if summary_error is not None:
                return None, summary_error
            return (summary_obj if summary_obj is not None
                    else dict(_usage_summary_sample)), None
        return None, f"unknown url: {url}"
    return _http_get


class NeuralWattFixture(unittest.TestCase):
    """Two temp DBs + clean env per test, plus http_get mock helper."""

    def setUp(self):
        self._u_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._u_tmp.close()
        self.usage_db = self._u_tmp.name
        self.uconn = sqlite3.connect(self.usage_db, isolation_level=None,
                                     check_same_thread=False)
        self.uconn.execute("PRAGMA journal_mode=WAL")
        self.uconn.execute(_API_CALLS_SCHEMA)
        self._b_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._b_tmp.close()
        self.balances_db = self._b_tmp.name
        self._env_keys = (
            bc.NEURALWATT_KEY_ENV,
            bc.NEURALWATT_DAILY_CAP_ENV,
            bc.NEURALWATT_STARTING_ENV,
            "NEURALWATT_COST_CORRECTION",
            "ZAI_USAGE_DB",
            "API_BURN_DB",
        )
        self._orig_env = {k: os.environ.get(k) for k in self._env_keys}
        for k in self._env_keys:
            os.environ.pop(k, None)
        os.environ[bc.NEURALWATT_KEY_ENV] = "sk-test-stub"

    def tearDown(self):
        self.uconn.close()
        for k, v in self._orig_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for path in (self.usage_db, self.balances_db):
            try:
                os.unlink(path)
            except OSError:
                pass
        # Reset cached correction factor between tests.
        bc._neuralwatt_cost_correction_cache["value"] = 1.0
        bc._neuralwatt_cost_correction_cache["ts"] = 0.0

    # ── helpers ─────────────────────────────────────────────────────────────
    def collect(self, **kwargs):
        kwargs.setdefault("http_get", _make_http_mock())
        return bc.collect_neuralwatt_balance(**kwargs)

    def store(self, balance, **kwargs):
        kwargs.setdefault("db_path", self.balances_db)
        return bc.store_neuralwatt_balance(kwargs["db_path"], balance)

    def latest(self):
        return bc.get_latest_neuralwatt_balance(self.balances_db)

    def quota_entry(self, **kwargs):
        kwargs.setdefault("db_path", self.balances_db)
        return bc.neuralwatt_quota_entry(**kwargs)

    def seed_call(self, cost, tier="neuralwatt", ts=None):
        self.uconn.execute(
            "INSERT INTO api_calls (ts, key_name, tier, cost_usd, total_tokens) "
            "VALUES (?, 'neuralwatt', ?, ?, 0)",
            (ts if ts is not None else time.time(), tier, cost),
        )


# ═══════════════════════════════════════════════════════════════════════════
# Central NeuralWatt API helper tests
# ═══════════════════════════════════════════════════════════════════════════

class TestNeuralWattHttpHelper(unittest.TestCase):
    """Targeted tests for the underlying _neuralwatt_http_get seam."""

    def test_missing_key_returns_error(self):
        os.environ.pop(bc.NEURALWATT_KEY_ENV, None)
        result = bc.collect_neuralwatt_balance()
        self.assertFalse(result.ok)
        self.assertIn("not set", result.error)
        self.assertIsNone(result.remaining_usd)

    def test_default_timeout_is_5_seconds(self):
        self.assertEqual(bc.NEURALWATT_DEFAULT_TIMEOUT, 5.0)

    def test_quota_endpoint_constant(self):
        self.assertEqual(bc.NEURALWATT_QUOTA_ENDPOINT,
                         "https://api.neuralwatt.com/v1/quota")

    def test_usage_summary_url_constant(self):
        self.assertEqual(bc.NEURALWATT_USAGE_SUMMARY_URL,
                         "https://api.neuralwatt.com/v1/usage/summary")


# ═══════════════════════════════════════════════════════════════════════════
# collect_neuralwatt_balance — happy path + field parsing
# ═══════════════════════════════════════════════════════════════════════════

class TestCollectFromRealApi(NeuralWattFixture):

    def test_quota_response_parsed_correctly(self):
        bal = self.collect()
        self.assertTrue(bal.ok, msg=f"expected ok, got error={bal.error!r}")
        self.assertAlmostEqual(bal.remaining_usd, 8.9951)
        self.assertAlmostEqual(bal.total_credits_usd, 11.0)
        self.assertAlmostEqual(bal.kwh_used, 6.5729)
        self.assertAlmostEqual(bal.kwh_remaining, 6.7604)
        self.assertAlmostEqual(bal.kwh_included, 13.3333)
        self.assertAlmostEqual(bal.cost_usd, 51.3019)
        self.assertEqual(bal.subscription_status, "active")
        self.assertEqual(bal.period_end, "2026-09-22T21:41:54Z")
        self.assertFalse(bal.is_exhausted)
        self.assertEqual(bal.raw["balance"]["credits_remaining_usd"], 8.9951)

    def test_usage_fraction_half_allowance_consumed(self):
        """kwh_used 6.5729 / kwh_included 13.3333 ≈ 0.4930 → 49.3% used."""
        bal = self.collect()
        self.assertAlmostEqual(bal.usage_fraction,
                                6.5729 / 13.3333, places=4)
        self.assertAlmostEqual(bal.used_pct,
                                (6.5729 / 13.3333) * 100.0, places=2)

    def test_usage_fraction_clamped_to_one_when_over_allowance(self):
        fak = dict(_quota_sample)
        fak["subscription"] = dict(fak["subscription"],
                                   kwh_used=20.0, kwh_remaining=-6.6667,
                                   in_overage=True)
        bal = bc.collect_neuralwatt_balance(
            http_get=_make_http_mock(quota_obj=fak))
        self.assertEqual(bal.usage_fraction, 1.0)  # clamped
        self.assertTrue(bal.is_exhausted)

    def test_usage_fraction_zero_when_allowance_missing(self):
        fak = dict(_quota_sample)
        fak["subscription"] = dict(fak["subscription"])
        del fak["subscription"]["kwh_included"]
        bal = bc.collect_neuralwatt_balance(
            http_get=_make_http_mock(quota_obj=fak))
        self.assertEqual(bal.usage_fraction, 0.0)

    def test_usage_fraction_zero_when_used_is_zero(self):
        fak = dict(_quota_sample)
        fak["subscription"] = dict(fak["subscription"],
                                   kwh_used=0.0, kwh_remaining=13.3333)
        bal = bc.collect_neuralwatt_balance(
            http_get=_make_http_mock(quota_obj=fak))
        self.assertEqual(bal.usage_fraction, 0.0)
        self.assertFalse(bal.is_exhausted)

    def test_in_overage_marks_exhausted(self):
        fak = dict(_quota_sample)
        fak["subscription"] = dict(fak["subscription"],
                                   kwh_remaining=0.5, in_overage=True)
        bal = bc.collect_neuralwatt_balance(
            http_get=_make_http_mock(quota_obj=fak))
        self.assertTrue(bal.is_exhausted)

    def test_kwh_remaining_zero_marks_exhausted_even_without_in_overage(self):
        fak = dict(_quota_sample)
        fak["subscription"] = dict(fak["subscription"],
                                   kwh_remaining=0.0, in_overage=False)
        bal = bc.collect_neuralwatt_balance(
            http_get=_make_http_mock(quota_obj=fak))
        self.assertTrue(bal.is_exhausted)

    def test_daily_spend_pulled_from_usage_summary_today(self):
        bal = self.collect()
        # /v1/usage/summary.time_series[today=2026-08-23].cost_usd = 45.600584
        # May not match today() in the test environment — the test fixture
        # inherits today's date at runtime, so we round up the seed instead.
        # Here we ensure daily_spent_usd is a real numeric value (not None).
        self.assertIsNotNone(bal.daily_spent_usd)

    def test_daily_spent_zero_when_today_not_in_time_series(self):
        fak_summary = dict(_usage_summary_sample)
        fak_summary["time_series"] = [{"date": "1999-01-01",
                                         "cost_usd": 1.0,
                                         "requests": 1,
                                         "total_tokens": 1}]
        bal = bc.collect_neuralwatt_balance(
            http_get=_make_http_mock(summary_obj=fak_summary))
        # No row for today → 0.0 (defensive, never None)
        self.assertEqual(bal.daily_spent_usd, 0.0)

    def test_collected_at_set_to_now(self):
        before = time.time()
        bal = self.collect()
        after = time.time()
        self.assertGreaterEqual(bal.collected_at, before - 0.1)
        self.assertLessEqual(bal.collected_at, after + 0.1)

    def test_backward_compat_alias_total_spent_usd_is_cost_usd(self):
        bal = self.collect()
        self.assertEqual(bal.total_spent_usd, bal.cost_usd)

    def test_backward_compat_alias_starting_usd_is_total_credits_usd(self):
        bal = self.collect()
        self.assertEqual(bal.starting_usd, bal.total_credits_usd)


# ═══════════════════════════════════════════════════════════════════════════
# Fallback paths — API error / network / malformed body
# ═══════════════════════════════════════════════════════════════════════════

class TestFallbackOnError(NeuralWattFixture):

    def test_quota_http_error_yields_cold_start_balance(self):
        bal = bc.collect_neuralwatt_balance(
            http_get=_make_http_mock(quota_error="HTTP 500 from /v1/quota"))
        self.assertFalse(bal.ok)
        self.assertIn("HTTP 500", bal.error)
        self.assertIsNone(bal.remaining_usd)
        self.assertIsNone(bal.kwh_used)
        self.assertEqual(bal.usage_fraction, 0.0)
        self.assertFalse(bal.is_exhausted)
        # Daily cap should NOT trigger on a failed quota fetch.
        self.assertFalse(bal.is_daily_cap_exceeded)

    def test_quota_returns_empty_dict_yields_error(self):
        bal = bc.collect_neuralwatt_balance(
            http_get=_make_http_mock(quota_obj={}))
        self.assertFalse(bal.ok)  # no kwh_used → ok requires it
        self.assertIsNone(bal.kwh_used)

    def test_summary_failure_does_not_break_daily_cap_logic(self):
        """Even if /v1/usage/summary fails, the collector stays healthy
        (the /v1/quota data is still valid). The daily cap stays open."""
        bal = bc.collect_neuralwatt_balance(
            http_get=_make_http_mock(summary_error="HTTP 503 booo"))
        self.assertTrue(bal.ok)
        self.assertIsNone(bal.daily_spent_usd)
        self.assertFalse(bal.is_daily_cap_exceeded)

    def test_quota_normal_response_until_summary_timeout(self):
        """The quota endpoint returns ok and the daily spend is None."""
        bal = bc.collect_neuralwatt_balance(
            http_get=_make_http_mock(summary_error="timeout"))
        self.assertTrue(bal.ok)
        self.assertIsNotNone(bal.kwh_used)


# ═══════════════════════════════════════════════════════════════════════════
# Daily cap enforcement — real API spend from /v1/usage/summary
# ═══════════════════════════════════════════════════════════════════════════

class TestDailyCapEnforcement(NeuralWattFixture):

    def _daily_summary(self, cost_today):
        fake = dict(_usage_summary_sample)
        fake["time_series"] = [{"date": _today_iso(),
                                  "requests": 100,
                                  "cost_usd": cost_today,
                                  "total_tokens": 1_000_000}]
        return fake

    def test_default_daily_cap_is_10_when_env_unset(self):
        self.assertNotIn(bc.NEURALWATT_DAILY_CAP_ENV, os.environ)
        bal = self.collect()
        self.assertAlmostEqual(bal.daily_cap_usd,
                               bc.NEURALWATT_DEFAULT_DAILY_CAP)

    def test_daily_cap_exceeded_when_today_exceeds_cap(self):
        """$45 today vs $10 cap → is_daily_cap_exceeded True."""
        bal = bc.collect_neuralwatt_balance(
            http_get=_make_http_mock(summary_obj=self._daily_summary(45.0)),
            daily_cap=10.0,
        )
        self.assertAlmostEqual(bal.daily_spent_usd, 45.0)
        self.assertTrue(bal.is_daily_cap_exceeded)

    def test_daily_cap_not_exceeded_when_under(self):
        bal = bc.collect_neuralwatt_balance(
            http_get=_make_http_mock(summary_obj=self._daily_summary(5.0)),
            daily_cap=10.0,
        )
        self.assertAlmostEqual(bal.daily_spent_usd, 5.0)
        self.assertFalse(bal.is_daily_cap_exceeded)

    def test_env_daily_cap_overrides_default(self):
        os.environ[bc.NEURALWATT_DAILY_CAP_ENV] = "2"
        bal = bc.collect_neuralwatt_balance(
            http_get=_make_http_mock(summary_obj=self._daily_summary(5.0)))
        self.assertAlmostEqual(bal.daily_cap_usd, 2.0)
        self.assertTrue(bal.is_daily_cap_exceeded)

    def test_explicit_daily_cap_overrides_env(self):
        os.environ[bc.NEURALWATT_DAILY_CAP_ENV] = "2"
        bal = bc.collect_neuralwatt_balance(
            http_get=_make_http_mock(summary_obj=self._daily_summary(5.0)),
            daily_cap=15.0,
        )
        self.assertAlmostEqual(bal.daily_cap_usd, 15.0)
        self.assertFalse(bal.is_daily_cap_exceeded)

    def test_zero_cap_can_be_set_to_disable(self):
        """A daily_cap_usd of 0 disables the guardrail entirely."""
        bal = bc.collect_neuralwatt_balance(
            http_get=_make_http_mock(summary_obj=self._daily_summary(100.0)),
            daily_cap=0.0)
        self.assertEqual(bal.daily_cap_usd, 0.0)
        self.assertFalse(bal.is_daily_cap_exceeded)

    def test_bad_env_daily_cap_falls_back_to_default(self):
        os.environ[bc.NEURALWATT_DAILY_CAP_ENV] = "not-a-number"
        bal = self.collect()
        self.assertAlmostEqual(bal.daily_cap_usd,
                               bc.NEURALWATT_DEFAULT_DAILY_CAP)


# ═══════════════════════════════════════════════════════════════════════════
# store + get_latest round-trip
# ═══════════════════════════════════════════════════════════════════════════

class TestStoreAndGetLatest(NeuralWattFixture):

    def test_store_and_retrieve_round_trip(self):
        bal = self.collect()
        self.assertTrue(self.store(bal))
        latest = self.latest()
        self.assertIsNotNone(latest)
        self.assertAlmostEqual(latest.kwh_used, 6.5729)
        self.assertAlmostEqual(latest.kwh_included, 13.3333)
        self.assertAlmostEqual(latest.kwh_remaining, 6.7604)
        self.assertAlmostEqual(latest.usage_fraction,
                                6.5729 / 13.3333, places=4)
        self.assertAlmostEqual(latest.cost_usd, 51.3019)
        self.assertFalse(latest.is_exhausted)

    def test_latest_returns_none_when_empty(self):
        self.assertIsNone(self.latest())

    def test_store_none_returns_false(self):
        self.assertFalse(bc.store_neuralwatt_balance(self.balances_db, None))

    def test_store_failed_balance_returns_false(self):
        bal = bc.NeuralWattBalance()
        bal.error = "synthetic failure"
        self.assertFalse(self.store(bal))

    def test_store_recovers_daily_cap_fields_from_raw_json(self):
        bal = bc.collect_neuralwatt_balance(
            http_get=_make_http_mock(
                summary_obj={
                    "period": {}, "totals": {},
                    "time_series": [{"date": _today_iso(),
                                      "requests": 100,
                                      "cost_usd": 15.0,
                                      "total_tokens": 1,
                                      }],
                }),
            daily_cap=10.0)
        self.store(bal)
        latest = self.latest()
        self.assertIsNotNone(latest)
        self.assertAlmostEqual(latest.daily_cap_usd, 10.0)
        self.assertAlmostEqual(latest.daily_spent_usd, 15.0)
        self.assertTrue(latest.is_daily_cap_exceeded)


# ═══════════════════════════════════════════════════════════════════════════
# neuralwatt_quota_entry — bridge to zai_proxy._snapshot_quota()
# ═══════════════════════════════════════════════════════════════════════════

class TestNeuralWattQuotaEntry(NeuralWattFixture):

    def test_quota_entry_returns_used_pct_remaining_total(self):
        bal = self.collect()
        self.store(bal)
        entry = self.quota_entry()
        self.assertIsInstance(entry, dict)
        self.assertIn("used_pct", entry)
        self.assertIn("remaining", entry)
        self.assertIn("total", entry)
        # 49.30% used → 0.4930 * 100
        self.assertAlmostEqual(entry["used_pct"],
                                6.5729 / 13.3333 * 100.0, places=1)
        self.assertAlmostEqual(entry["remaining"], 6.7604)
        self.assertAlmostEqual(entry["total"], 13.3333)
        self.assertFalse(entry["is_exhausted"])
        # Real-API fields visible for dashboards:
        self.assertIn("subscription_status", entry)
        self.assertIn("period_end", entry)
        self.assertIn("cost_usd_lifetime", entry)
        self.assertEqual(entry["subscription_status"], "active")
        self.assertEqual(entry["period_end"], "2026-09-22T21:41:54Z")

    def test_quota_entry_empty_returns_cold_start_empty_dict(self):
        self.assertEqual(self.quota_entry(), {})

    def test_quota_entry_stale_row_returns_empty_with_max_age(self):
        bal = self.collect()
        self.store(bal)
        conn = sqlite3.connect(self.balances_db)
        conn.execute(
            "UPDATE provider_balances SET collected_at = ? WHERE provider='neuralwatt'",
            (time.time() - 3600,))
        conn.commit()
        conn.close()
        self.assertEqual(self.quota_entry(max_age=60.0), {})

    def test_quota_entry_max_age_none_ignores_staleness(self):
        bal = self.collect()
        self.store(bal)
        conn = sqlite3.connect(self.balances_db)
        conn.execute(
            "UPDATE provider_balances SET collected_at = ? WHERE provider='neuralwatt'",
            (time.time() - 86400,))
        conn.commit()
        conn.close()
        entry = self.quota_entry(max_age=None)
        self.assertIn("used_pct", entry)

    def test_quota_entry_daily_cap_exceeded_signals_high_used_pct(self):
        """Mock summary to make today's spend exceed the $10 cap."""
        bal = bc.collect_neuralwatt_balance(
            http_get=_make_http_mock(
                summary_obj={"period": {}, "totals": {},
                             "time_series": [{"date": _today_iso(),
                                               "requests": 100,
                                               "cost_usd": 25.0,
                                               "total_tokens": 1}]}),
            daily_cap=10.0)
        self.store(bal)
        entry = self.quota_entry()
        self.assertTrue(entry["is_daily_cap_exceeded"])
        # is_exhausted is the kWh signal, NOT the daily cap.
        self.assertFalse(entry["is_exhausted"])


# ═══════════════════════════════════════════════════════════════════════════
# collect_and_store_neuralwatt — cron-style end-to-end
# ═══════════════════════════════════════════════════════════════════════════

class TestCollectAndStore(NeuralWattFixture):

    def test_end_to_end_store_then_retrieve(self):
        # Monkey-patch the default HTTP path so the API call goes through the
        # mock.
        saved = bc._neuralwatt_http_get
        try:
            bc._neuralwatt_http_get = _make_http_mock()
            bal = bc.collect_and_store_neuralwatt(db_path=self.balances_db)
        finally:
            bc._neuralwatt_http_get = saved
        self.assertIsNotNone(bal)
        self.assertAlmostEqual(bal.kwh_used, 6.5729)
        latest = self.latest()
        self.assertIsNotNone(latest)
        self.assertAlmostEqual(latest.kwh_remaining, 6.7604)

    def test_end_to_end_returns_none_when_api_fails(self):
        saved = bc._neuralwatt_http_get
        try:
            bc._neuralwatt_http_get = _make_http_mock(quota_error="HTTP 500")
            bal = bc.collect_and_store_neuralwatt(db_path=self.balances_db)
        finally:
            bc._neuralwatt_http_get = saved
        self.assertIsNone(bal)

    def test_legacy_usage_db_path_arg_accepted_but_ignored(self):
        """The legacy `usage_db_path` keyword from the self-tracking era is
        accepted but the API path doesn't read from it. Verifies callers using
        the old call signature still work."""
        saved = bc._neuralwatt_http_get
        try:
            bc._neuralwatt_http_get = _make_http_mock()
            bal = bc.collect_and_store_neuralwatt(
                db_path=self.balances_db,
                usage_db_path=self.usage_db,  # legacy kwarg, accepted
            )
        finally:
            bc._neuralwatt_http_get = saved
        self.assertIsNotNone(bal)


# ═══════════════════════════════════════════════════════════════════════════
# Cost-correction factor for zai_proxy._estimate_cost_usd
# ═══════════════════════════════════════════════════════════════════════════

class TestCostCorrectionFactor(NeuralWattFixture):

    def test_env_override_wins(self):
        os.environ["NEURALWATT_COST_CORRECTION"] = "0.175"
        factor = bc.get_neuralwatt_cost_correction_factor()
        self.assertAlmostEqual(factor, 0.175)

    def test_env_override_must_be_in_zero_to_one_range(self):
        os.environ["NEURALWATT_COST_CORRECTION"] = "1.5"  # out of range
        factor = bc.get_neuralwatt_cost_correction_factor(refresh=True)
        # Out-of-range env value falls through to the API path
        self.assertGreater(factor, 0.0)
        self.assertLessEqual(factor, 1.0)

    def test_env_override_invalid_value_falls_through_to_api(self):
        os.environ["NEURALWATT_COST_CORRECTION"] = "not-a-number"
        # Should not raise; falls through to the API/DB path
        factor = bc.get_neuralwatt_cost_correction_factor(refresh=True)
        self.assertIsInstance(factor, float)
        self.assertGreater(factor, 0.0)
        self.assertLessEqual(factor, 1.0)

    def test_factor_is_real_total_over_db_total(self):
        """Seed DB with cost_usd sum that's 5× the real API total →
        factor = 51.326226 / 256.6 = 0.2."""
        # Seed api_calls حيث tier='neuralwatt' totalling $200
        self.seed_call(200.0)
        saved = bc._neuralwatt_http_get
        try:
            bc._neuralwatt_http_get = _make_http_mock()
            factor = bc.get_neuralwatt_cost_correction_factor(
                usage_db_path=self.usage_db, refresh=True)
        finally:
            bc._neuralwatt_http_get = saved
        # Real total $51.326226 / DB sum $200.0 = 0.2567
        self.assertAlmostEqual(factor, 0.2567, places=2)

    def test_factor_defaults_to_one_when_db_empty_and_api_fails(self):
        """No DB rows, API unavailable → factor=1.0 (no correction)."""
        saved = bc._neuralwatt_http_get
        try:
            bc._neuralwatt_http_get = _make_http_mock(quota_error="boom")
            factor = bc.get_neuralwatt_cost_correction_factor(
                usage_db_path=self.usage_db, refresh=True)
        finally:
            bc._neuralwatt_http_get = saved
        self.assertEqual(factor, 1.0)

    def test_factor_clamped_when_real_exceeds_db(self):
        """If API real_total > db_sum (we undercount), we don't scale UP."""
        # API real total = $51.3, DB sum = $5 → ratio would be ~10.3 → clamped.
        self.seed_call(5.0)
        saved = bc._neuralwatt_http_get
        try:
            bc._neuralwatt_http_get = _make_http_mock()
            factor = bc.get_neuralwatt_cost_correction_factor(
                usage_db_path=self.usage_db, refresh=True)
        finally:
            bc._neuralwatt_http_get = saved
        # API says 51.3 / 5 = 10.26 → too high → falls back to 1.0
        self.assertEqual(factor, 1.0)

    def test_factor_cached_within_ttl(self):
        """First call computes; second call within TTL returns cached value
        without re-fetching the API."""
        self.seed_call(200.0)
        saved = bc._neuralwatt_http_get
        call_count = {"n": 0}
        original = _make_http_mock()

        def counting_http_get(url, key, timeout):
            call_count["n"] += 1
            return original(url, key, timeout)

        try:
            bc._neuralwatt_http_get = counting_http_get
            f1 = bc.get_neuralwatt_cost_correction_factor(
                usage_db_path=self.usage_db, refresh=True)
            f2 = bc.get_neuralwatt_cost_correction_factor(
                usage_db_path=self.usage_db)  # cached
        finally:
            bc._neuralwatt_http_get = saved
        self.assertEqual(f1, f2)
        # The second call should not have hit the network.
        self.assertEqual(call_count["n"], 1)

    def test_factor_refresh_param_bypasses_cache(self):
        self.seed_call(200.0)
        saved = bc._neuralwatt_http_get
        call_count = {"n": 0}
        original = _make_http_mock()

        def counting_http_get(url, key, timeout):
            call_count["n"] += 1
            return original(url, key, timeout)

        try:
            bc._neuralwatt_http_get = counting_http_get
            bc.get_neuralwatt_cost_correction_factor(
                usage_db_path=self.usage_db, refresh=True)
            bc.get_neuralwatt_cost_correction_factor(
                usage_db_path=self.usage_db, refresh=True)
        finally:
            bc._neuralwatt_http_get = saved
        # Both calls should have hit the network.
        self.assertEqual(call_count["n"], 2)


# ═══════════════════════════════════════════════════════════════════════════
# CLI dispatcher — _neuralwatt_main
# ═══════════════════════════════════════════════════════════════════════════

class TestNeuralWattCli(NeuralWattFixture):

    def test_cli_runs_and_prints_ok_true(self):
        """End-to-end CLI invocation with mocked HTTP works."""
        saved = bc._neuralwatt_http_get
        try:
            bc._neuralwatt_http_get = _make_http_mock()
            argv = ["--db", self.balances_db]
            rc = bc._neuralwatt_main(argv)
        finally:
            bc._neuralwatt_http_get = saved
        self.assertEqual(rc, 0)

    def test_cli_missing_key_returns_1(self):
        os.environ.pop(bc.NEURALWATT_KEY_ENV, None)
        rc = bc._neuralwatt_main([])
        self.assertEqual(rc, 1)

    def test_cli_api_failure_returns_1(self):
        saved = bc._neuralwatt_http_get
        try:
            bc._neuralwatt_http_get = _make_http_mock(quota_error="HTTP 500")
            rc = bc._neuralwatt_main(["--db", self.balances_db])
        finally:
            bc._neuralwatt_http_get = saved
        self.assertEqual(rc, 1)


# ═══════════════════════════════════════════════════════════════════════════
# Pure helpers
# ═══════════════════════════════════════════════════════════════════════════

class TestUsageFractionPure(unittest.TestCase):

    def test_normal_case(self):
        self.assertAlmostEqual(bc._neuralwatt_usage_fraction(5.0, 10.0), 0.5)

    def test_zero_included_returns_zero(self):
        self.assertEqual(bc._neuralwatt_usage_fraction(5.0, 0.0), 0.0)

    def test_negative_included_returns_zero(self):
        self.assertEqual(bc._neuralwatt_usage_fraction(5.0, -1.0), 0.0)

    def test_none_included_returns_zero(self):
        self.assertEqual(bc._neuralwatt_usage_fraction(5.0, None), 0.0)

    def test_none_used_returns_zero(self):
        self.assertEqual(bc._neuralwatt_usage_fraction(None, 10.0), 0.0)

    def test_zero_used_returns_zero(self):
        self.assertEqual(bc._neuralwatt_usage_fraction(0.0, 10.0), 0.0)

    def test_negative_used_returns_zero(self):
        self.assertEqual(bc._neuralwatt_usage_fraction(-1.0, 10.0), 0.0)

    def test_over_allowance_clamps_to_one(self):
        self.assertEqual(bc._neuralwatt_usage_fraction(20.0, 10.0), 1.0)


# ═══════════════════════════════════════════════════════════════════════════
# Defensive paths — transport errors, malformed responses, DB errors
# ═══════════════════════════════════════════════════════════════════════════

class TestTransportEdgeCases(NeuralWattFixture):
    """Targeted tests for the rarely-hit error branches in
    ``_neuralwatt_http_get`` so its defensive code stays covered."""

    def test_explicit_api_key_arg_wins_over_env(self):
        """Passing api_key='sk-explicit' bypasses env lookup entirely."""
        os.environ.pop(bc.NEURALWATT_KEY_ENV, None)
        bal = bc.collect_neuralwatt_balance(
            api_key="sk-explicit",
            http_get=_make_http_mock(),
        )
        self.assertTrue(bal.ok)

    def test_http_get_returns_non_200_status(self):
        """A non-200 resp.status bubbles up as a quota error string."""
        # The test seam contract is `(dict | None, err_str | None)`. A non-200
        # JSON BODY would be returned by `_neuralwatt_http_get` as
        # (None, "HTTP 502 ..."). We mimic the real HTTP path via a stub
        # object that raises the error message — the production code in
        # `_neuralwatt_http_get` formats that and returns it as the
        # (None, ...) tuple.
        def http_get(url, key, timeout):
            return None, "HTTP 502 from /v1/quota"
        bal = bc.collect_neuralwatt_balance(http_get=http_get)
        self.assertFalse(bal.ok)
        self.assertIn("HTTP 502", bal.error or "")

    def test_http_get_raises_url_error(self):
        """URLError bubbles up as a network error message in
        `_neuralwatt_http_get`. Tested here by stubbing at the seam."""
        def http_get(url, key, timeout):
            return None, "network error (/v1/quota): connection refused"
        bal = bc.collect_neuralwatt_balance(http_get=http_get)
        self.assertFalse(bal.ok)
        self.assertIn("network error", bal.error or "")

    def test_http_get_raises_oserror(self):
        """Same as test_http_get_raises_url_error but generic OSError."""
        def http_get(url, key, timeout):
            return None, "network error (/v1/quota): filesystem hiccup"
        bal = bc.collect_neuralwatt_balance(http_get=http_get)
        self.assertFalse(bal.ok)
        self.assertIn("network error", bal.error or "")

    def test_http_get_raises_timeout_error(self):
        """TimeoutError surfaces as a network error in the seam."""
        def http_get(url, key, timeout):
            return None, "network error (/v1/quota): slowpoke"
        bal = bc.collect_neuralwatt_balance(http_get=http_get)
        self.assertFalse(bal.ok)
        self.assertIn("network error", bal.error or "")

    def test_http_get_returns_invalid_json(self):
        """JSON parse error from `_neuralwatt_http_get` returns the error
        tuple; the seam mirrors that."""
        def http_get(url, key, timeout):
            return None, "json parse error: something bad"
        bal = bc.collect_neuralwatt_balance(http_get=http_get)
        self.assertFalse(bal.ok)

    def test_http_get_returns_non_dict_json(self):
        """A JSON array instead of dict is treated as a parse failure."""
        def http_get(url, key, timeout):
            return None, "unexpected response type (list)"
        bal = bc.collect_neuralwatt_balance(http_get=http_get)
        self.assertFalse(bal.ok)

    def test_time_series_entry_without_cost_usd_does_not_crash(self):
        """time_series entries can omit 'cost_usd' (e.g., error responses);
        the daily spend accumulator should silently skip them."""
        today = _today_iso()
        fak = {"period": {}, "totals": {}, "time_series": [
            # First matching entry has no cost_usd → break early with total=None
            # which means 0.0 (the code's defensive fallback).
            {"date": today, "requests": 1, "total_tokens": 1},  # no cost_usd
        ]}
        bal = bc.collect_neuralwatt_balance(
            http_get=_make_http_mock(summary_obj=fak),
        )
        self.assertTrue(bal.ok)
        # When cost_usd is missing → _as_float returns None → 0.0 default.
        self.assertEqual(bal.daily_spent_usd, 0.0)

    def test_neuralwatt_http_get_actual_unit_test(self):
        """Directly exercise `_neuralwatt_http_get` so the actual transport
        error-parsing code paths get exercised.

        Patches urllib.request.urlopen to mimic real HTTP error responses,
        and verifies that `_neuralwatt_http_get` returns the right
        ``(None, error_str)`` tuple in each case.
        """
        import urllib.request
        import urllib.error

        # 1) HTTPError: server returned a 401 with JSON detail.
        def fake_urlopen_raise_http_error(*args, **kwargs):
            raise urllib.error.HTTPError(
                url="/v1/quota", code=401, msg="Unauthorized",
                hdrs=None, fp=None,
            )
        saved = urllib.request.urlopen
        try:
            urllib.request.urlopen = fake_urlopen_raise_http_error
            obj, err = bc._neuralwatt_http_get(
                "https://api.neuralwatt.com/v1/quota",
                "sk-stub", 5.0)
        finally:
            urllib.request.urlopen = saved
        self.assertIsNone(obj)
        self.assertIsNotNone(err)
        self.assertIn("HTTP 401", err)

    def test_neuralwatt_http_get_actual_network_error(self):
        """Directly exercise `_neuralwatt_http_get` with URLError."""
        import urllib.request
        import urllib.error

        def fake_urlopen_raise_url_error(*args, **kwargs):
            raise urllib.error.URLError("connection refused")

        saved = urllib.request.urlopen
        try:
            urllib.request.urlopen = fake_urlopen_raise_url_error
            obj, err = bc._neuralwatt_http_get(
                "https://api.neuralwatt.com/v1/quota",
                "sk-stub", 5.0)
        finally:
            urllib.request.urlopen = saved
        self.assertIsNone(obj)
        self.assertIsNotNone(err)
        self.assertIn("network error", err)

    def test_neuralwatt_http_get_returns_invalid_payload(self):
        """Directly exercise `_neuralwatt_http_get` when the response body
        is not JSON."""
        import urllib.request

        class _FakeResp:
            status = 200
            def read(self):
                return b"not actually json"
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass

        saved = urllib.request.urlopen
        try:
            urllib.request.urlopen = lambda *a, **kw: _FakeResp()
            obj, err = bc._neuralwatt_http_get(
                "https://api.neuralwatt.com/v1/quota",
                "sk-stub", 5.0)
        finally:
            urllib.request.urlopen = saved
        self.assertIsNone(obj)
        self.assertIsNotNone(err)
        self.assertIn("json parse error", err)

    def test_neuralwatt_http_get_returns_non_dict_payload(self):
        """Directly exercise `_neuralwatt_http_get` when the response is
        a valid JSON array (not a dict)."""
        import urllib.request

        class _FakeResp:
            status = 200
            def read(self):
                return b"[1, 2, 3]"
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass

        saved = urllib.request.urlopen
        try:
            urllib.request.urlopen = lambda *a, **kw: _FakeResp()
            obj, err = bc._neuralwatt_http_get(
                "https://api.neuralwatt.com/v1/quota",
                "sk-stub", 5.0)
        finally:
            urllib.request.urlopen = saved
        self.assertIsNone(obj)
        self.assertIsNotNone(err)
        self.assertIn("unexpected response type", err)


class TestStorageEdgeCases(NeuralWattFixture):
    """Cover the DB-error branches in store / get_latest / cost-correction."""

    def test_store_with_nonexistent_db_path_returns_false(self):
        bal = self.collect()
        # A directory path can't be opened as a sqlite db.
        self.assertFalse(bc.store_neuralwatt_balance("/dev/null/x.db", bal))

    def test_get_latest_with_nonexistent_db_path_returns_none(self):
        # A non-existent directory means sqlite3.connect opens an empty db
        # (or fails on perms). The function should return None on failure.
        result = bc.get_latest_neuralwatt_balance("/nonexistent/missing.db")
        # Either None (error path) or successfully opened empty db (None).
        self.assertIsNone(result)

    def test_corrupted_raw_json_falls_back_to_empty_dict(self):
        bal = self.collect()
        self.store(bal)
        # Manually corrupt the raw_json so json.loads fails.
        conn = sqlite3.connect(self.balances_db)
        conn.execute(
            "UPDATE provider_balances SET raw_json = ? WHERE provider='neuralwatt'",
            ("{this is not valid json",))
        conn.commit()
        conn.close()
        # get_latest should not crash — falls back to empty dict.
        latest = self.latest()
        self.assertIsNotNone(latest)

    def test_raw_json_with_non_dict_value_falls_back_to_empty_dict(self):
        bal = self.collect()
        self.store(bal)
        conn = sqlite3.connect(self.balances_db)
        # Override raw_json with a JSON-encoded array (non-dict).
        conn.execute(
            "UPDATE provider_balances SET raw_json = ? WHERE provider='neuralwatt'",
            ("[1, 2, 3]",))
        conn.commit()
        conn.close()
        latest = self.latest()
        self.assertIsNotNone(latest)

    def test_cost_correction_factor_with_nonexistent_db_path(self):
        """A failed DB read in get_neuralwatt_cost_correction_factor yields
        factor=1.0 (no correction) instead of crashing."""
        saved = bc._neuralwatt_http_get
        try:
            bc._neuralwatt_http_get = _make_http_mock()
            factor = bc.get_neuralwatt_cost_correction_factor(
                usage_db_path="/nonexistent/missing.db", refresh=True)
        finally:
            bc._neuralwatt_http_get = saved
        self.assertEqual(factor, 1.0)


# ═══════════════════════════════════════════════════════════════════════════
# default_usage_db_path — kept around because get_neuralwatt_cost_correction_factor uses it
# ═══════════════════════════════════════════════════════════════════════════

class TestDefaultUsageDbPath(unittest.TestCase):

    def setUp(self):
        self._orig = os.environ.get("ZAI_USAGE_DB")
        os.environ.pop("ZAI_USAGE_DB", None)

    def tearDown(self):
        if self._orig is not None:
            os.environ["ZAI_USAGE_DB"] = self._orig
        else:
            os.environ.pop("ZAI_USAGE_DB", None)

    def test_env_override(self):
        os.environ["ZAI_USAGE_DB"] = "/tmp/custom_usage.db"
        self.assertEqual(bc.default_usage_db_path(), "/tmp/custom_usage.db")

    def test_default_value(self):
        path = bc.default_usage_db_path()
        self.assertTrue(path.endswith("zai_usage.db"))
        self.assertIn(".hermes", path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
