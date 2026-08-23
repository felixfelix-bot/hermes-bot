#!/usr/bin/env python3
"""Tests for the NeuralWatt self-tracking balance collector.

NeuralWatt has NO balance API — every path returns 404. The collector computes
remaining = starting_balance - SUM(cost_usd FROM api_calls WHERE tier='neuralwatt').

Tests are fixture-based — no live calls, no network, no writes to production DBs.
Two temp sqlite DBs are created per test:
  * usage.db   — mimics zai_usage.db schema (api_calls + tier column)
  * balances.db — mimics api_burn.db (provider_balances, auto-created by _ensure_table)

Run:  python3 -m pytest test_neuralwatt_balance.py -v   (from ~/.hermes/bot)
  or: python3 test_neuralwatt_balance.py               (plain unittest runner)
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import src.balance_collectors as bc  # noqa: E402


# ── Schema mirroring zai_usage.db.api_calls ──────────────────────────────────
_API_CALLS_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_calls (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               REAL NOT NULL,
    key_name         TEXT,
    key_suffix       TEXT,
    model            TEXT,
    prompt_tokens    INTEGER,
    completion_tokens INTEGER,
    total_tokens     INTEGER,
    tier             TEXT,
    cache_hit        INTEGER DEFAULT 0,
    ollama_hit       INTEGER DEFAULT 0,
    ppq_hit          INTEGER DEFAULT 0,
    status_code      INTEGER,
    error            TEXT,
    duration_ms      INTEGER,
    cost_usd         REAL DEFAULT NULL,
    cost_source      TEXT DEFAULT NULL,
    session_id       TEXT,
    task_type        TEXT
)
"""


def _epoch_for(days_ago: float = 0.0) -> float:
    """epoch seconds for a row inserted `days_ago` days before today (UTC)."""
    return time.time() - days_ago * 86400.0


def _seed_call(conn, cost_usd, tier="neuralwatt", ts=None, key_name="neuralwatt"):
    """Insert one api_calls row with the given cost/tier/ts."""
    conn.execute(
        "INSERT INTO api_calls (ts, key_name, tier, cost_usd, total_tokens, model)"
        " VALUES (?, ?, ?, ?, 0, 'glm-5.2')",
        (ts if ts is not None else time.time(), key_name, tier, cost_usd),
    )


class NeuralWattFixture(unittest.TestCase):
    """Two temp DBs (usage.db + balances.db) + clean env per test."""

    def setUp(self):
        # usage.db — the zai_usage.db analogue (api_calls table)
        self._u_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._u_tmp.close()
        self.usage_db = self._u_tmp.name
        self.uconn = sqlite3.connect(self.usage_db, isolation_level=None,
                                     check_same_thread=False)
        self.uconn.execute("PRAGMA journal_mode=WAL")
        self.uconn.execute(_API_CALLS_SCHEMA)
        # balances.db — the api_burn.db analogue (provider_balances table)
        self._b_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._b_tmp.close()
        self.balances_db = self._b_tmp.name
        # Snapshot env vars we touch so we can restore on teardown.
        self._env_keys = (
            bc.NEURALWATT_STARTING_ENV,
            bc.NEURALWATT_DAILY_CAP_ENV,
            "ZAI_USAGE_DB",
            "API_BURN_DB",
        )
        self._orig_env = {k: os.environ.get(k) for k in self._env_keys}
        # Clear them by default so tests are deterministic.
        for k in self._env_keys:
            os.environ.pop(k, None)

    def tearDown(self):
        self.uconn.close()
        for k, v in self._orig_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        try:
            os.unlink(self.usage_db)
        except OSError:
            pass
        try:
            os.unlink(self.balances_db)
        except OSError:
            pass

    # ── helpers ─────────────────────────────────────────────────────────────
    def collect(self, **kwargs):
        kwargs.setdefault("usage_db_path", self.usage_db)
        kwargs.setdefault("balances_db_path", self.balances_db)
        return bc.collect_neuralwatt_balance(**kwargs)

    def store(self, balance, **kwargs):
        kwargs.setdefault("db_path", self.balances_db)
        return bc.store_neuralwatt_balance(kwargs["db_path"], balance)

    def latest(self):
        return bc.get_latest_neuralwatt_balance(self.balances_db)

    def quota_entry(self, **kwargs):
        kwargs.setdefault("db_path", self.balances_db)
        return bc.neuralwatt_quota_entry(**kwargs)


# ═══════════════════════════════════════════════════════════════════════════
# collect_neuralwatt_balance — core spend computation
# ═══════════════════════════════════════════════════════════════════════════

class TestCollectNeuralWattBalance(NeuralWattFixture):

    def test_known_spend_computed_correctly(self):
        """$30 spent on $100 starting → remaining $70, usage_fraction 0.30."""
        for cost in (10.0, 5.0, 15.0):  # 30 total
            _seed_call(self.uconn, cost)
        bal = self.collect(starting=100.0)
        self.assertTrue(bal.ok, msg=f"expected ok, got error={bal.error!r}")
        self.assertAlmostEqual(bal.total_spent_usd, 30.0, places=4)
        self.assertAlmostEqual(bal.starting_usd, 100.0)
        self.assertAlmostEqual(bal.remaining_usd, 70.0, places=4)
        self.assertAlmostEqual(bal.usage_fraction, 0.30, places=4)
        self.assertFalse(bal.is_exhausted)
        self.assertIsNone(bal.error)

    def test_empty_db_yields_zero_spend(self):
        """Fresh usage DB with no rows → spent $0, full remaining."""
        bal = self.collect(starting=100.0)
        self.assertTrue(bal.ok)
        self.assertEqual(bal.total_spent_usd, 0.0)
        self.assertEqual(bal.remaining_usd, 100.0)
        self.assertEqual(bal.usage_fraction, 0.0)
        self.assertFalse(bal.is_exhausted)

    def test_usage_fraction_clamped_to_1_when_overspent(self):
        """Spent $150 on $100 starting → fraction 1.0, exhausted True."""
        _seed_call(self.uconn, 150.0)
        bal = self.collect(starting=100.0)
        self.assertAlmostEqual(bal.total_spent_usd, 150.0)
        self.assertAlmostEqual(bal.remaining_usd, -50.0, places=4)
        self.assertEqual(bal.usage_fraction, 1.0)  # clamped
        self.assertTrue(bal.is_exhausted)

    def test_usage_fraction_clamped_to_0_on_negative_spend(self):
        """Bizarre: negative cost_usd (refunds) → fraction clamps to 0."""
        _seed_call(self.uconn, -10.0)
        bal = self.collect(starting=100.0)
        self.assertEqual(bal.usage_fraction, 0.0)  # clamped
        self.assertFalse(bal.is_exhausted)

    def test_only_neuralwatt_tier_is_summed(self):
        """Rows with tier != 'neuralwatt' must not be counted."""
        _seed_call(self.uconn, 30.0, tier="neuralwatt")
        _seed_call(self.uconn, 999.0, tier="telnyx")  # noise
        _seed_call(self.uconn, 1.0, tier="ppq")        # noise
        bal = self.collect(starting=100.0)
        self.assertAlmostEqual(bal.total_spent_usd, 30.0, places=4)

    def test_default_starting_balance_is_100_when_env_unset(self):
        """No NEURALWATT_STARTING_BALANCE env → default $100."""
        self.assertNotIn(bc.NEURALWATT_STARTING_ENV, os.environ)
        _seed_call(self.uconn, 50.0)
        bal = self.collect()  # starting omitted, env removed in setUp
        self.assertAlmostEqual(bal.starting_usd,
                               bc.NEURALWATT_DEFAULT_STARTING_BALANCE)
        # Also store/recover — get_latest should use the stored starting.
        self.store(bal)
        latest = self.latest()
        self.assertAlmostEqual(latest.starting_usd, 100.0)

    def test_env_starting_balance_overrides_default(self):
        """NEURALWATT_STARTING_BALANCE=50 in env → starting=50."""
        os.environ[bc.NEURALWATT_STARTING_ENV] = "50"
        _seed_call(self.uconn, 25.0)
        bal = self.collect()
        self.assertAlmostEqual(bal.starting_usd, 50.0)
        self.assertAlmostEqual(bal.remaining_usd, 25.0, places=4)
        self.assertAlmostEqual(bal.usage_fraction, 0.5, places=4)

    def test_explicit_arg_overrides_env_starting(self):
        """Explicit starting=80 wins over env=50."""
        os.environ[bc.NEURALWATT_STARTING_ENV] = "50"
        _seed_call(self.uconn, 20.0)
        bal = self.collect(starting=80.0)
        self.assertAlmostEqual(bal.starting_usd, 80.0)
        self.assertAlmostEqual(bal.remaining_usd, 60.0, places=4)

    def test_bad_env_starting_balance_falls_back_to_default(self):
        """Bad env value (non-numeric) → default, never raises."""
        os.environ[bc.NEURALWATT_STARTING_ENV] = "not-a-number"
        bal = self.collect()
        self.assertAlmostEqual(bal.starting_usd, 100.0)

    def test_zero_starting_balance_avoids_division_by_zero(self):
        """starting=0 → usage_fraction=0 (defensive), no ZeroDivisionError."""
        _seed_call(self.uconn, 5.0)
        bal = self.collect(starting=0.0)
        self.assertEqual(bal.usage_fraction, 0.0)
        self.assertTrue(bal.is_exhausted)  # remaining_usd = -5 <= 0

    def test_collected_at_set_to_now_on_success(self):
        """collected_at should be ~= now (within 5s window)."""
        before = time.time()
        bal = self.collect(starting=100.0)
        after = time.time()
        self.assertGreaterEqual(bal.collected_at, before - 0.1)
        self.assertLessEqual(bal.collected_at, after + 0.1)

    def test_db_path_failure_fallback(self):
        """Bad usage_db_path → error set, numeric fields None, never raises."""
        bal = bc.collect_neuralwatt_balance(
            starting=100.0,
            usage_db_path="/nonexistent/path/to/nowhere.db",
            balances_db_path=self.balances_db,
        )
        self.assertFalse(bal.ok)
        self.assertIsNotNone(bal.error)
        self.assertIsNone(bal.total_spent_usd)
        self.assertIsNone(bal.remaining_usd)
        # usage_fraction should be the safe 0.0
        self.assertEqual(bal.usage_fraction, 0.0)


# ═══════════════════════════════════════════════════════════════════════════
# Daily spend cap
# ═══════════════════════════════════════════════════════════════════════════

class TestNeuralWattDailyCap(NeuralWattFixture):

    def test_default_daily_cap_is_10_when_env_unset(self):
        """No NEURALWATT_DAILY_CAP env → default $10/day."""
        self.assertNotIn(bc.NEURALWATT_DAILY_CAP_ENV, os.environ)
        bal = self.collect(starting=100.0)
        self.assertAlmostEqual(bal.daily_cap_usd,
                               bc.NEURALWATT_DEFAULT_DAILY_CAP)

    def test_daily_cap_exceeded(self):
        """$15 spent today, $10 cap → is_daily_cap_exceeded True."""
        for cost in (5.0, 5.0, 5.0):  # 15 total today
            _seed_call(self.uconn, cost)
        bal = self.collect(starting=100.0, daily_cap=10.0)
        self.assertAlmostEqual(bal.daily_spent_usd, 15.0, places=4)
        self.assertTrue(bal.is_daily_cap_exceeded)

    def test_daily_cap_not_exceeded(self):
        """$5 spent today, $10 cap → is_daily_cap_exceeded False."""
        for cost in (2.0, 3.0):  # 5 total today
            _seed_call(self.uconn, cost)
        bal = self.collect(starting=100.0, daily_cap=10.0)
        self.assertAlmostEqual(bal.daily_spent_usd, 5.0, places=4)
        self.assertFalse(bal.is_daily_cap_exceeded)

    def test_yesterday_spend_does_not_count_toward_today(self):
        """Older-than-today rows are excluded from daily_spent_usd."""
        _seed_call(self.uconn, 50.0, ts=_epoch_for(days_ago=2))  # 2 days ago
        _seed_call(self.uconn, 5.0)  # today
        bal = self.collect(starting=100.0, daily_cap=10.0)
        self.assertAlmostEqual(bal.daily_spent_usd, 5.0, places=4)
        self.assertFalse(bal.is_daily_cap_exceeded)
        # Total still includes the old spend:
        self.assertAlmostEqual(bal.total_spent_usd, 55.0, places=4)

    def test_daily_cap_via_env(self):
        """NEURALWATT_DAILY_CAP=2 in env → cap=2."""
        os.environ[bc.NEURALWATT_DAILY_CAP_ENV] = "2"
        _seed_call(self.uconn, 5.0)  # exceeds $2 cap
        bal = self.collect(starting=100.0)
        self.assertAlmostEqual(bal.daily_cap_usd, 2.0)
        self.assertTrue(bal.is_daily_cap_exceeded)

    def test_explicit_daily_cap_overrides_env(self):
        """Explicit daily_cap=15 wins over env=2."""
        os.environ[bc.NEURALWATT_DAILY_CAP_ENV] = "2"
        _seed_call(self.uconn, 5.0)  # under 15 cap, over 2 env
        bal = self.collect(starting=100.0, daily_cap=15.0)
        self.assertAlmostEqual(bal.daily_cap_usd, 15.0)
        self.assertFalse(bal.is_daily_cap_exceeded)


# ═══════════════════════════════════════════════════════════════════════════
# store_neuralwatt_balance + get_latest_neuralwatt_balance round-trip
# ═══════════════════════════════════════════════════════════════════════════

class TestStoreAndGetLatest(NeuralWattFixture):

    def test_store_and_retrieve_round_trip(self):
        _seed_call(self.uconn, 30.0)
        bal = self.collect(starting=100.0, daily_cap=10.0)
        self.assertTrue(self.store(bal))
        latest = self.latest()
        self.assertIsNotNone(latest)
        self.assertAlmostEqual(latest.total_spent_usd, 30.0, places=4)
        self.assertAlmostEqual(latest.starting_usd, 100.0)
        self.assertAlmostEqual(latest.remaining_usd, 70.0, places=4)
        self.assertAlmostEqual(latest.usage_fraction, 0.30, places=4)
        self.assertFalse(latest.is_exhausted)

    def test_latest_returns_none_when_empty(self):
        """No rows yet → None, never raises."""
        self.assertIsNone(self.latest())

    def test_store_none_returns_false(self):
        self.assertFalse(bc.store_neuralwatt_balance(self.balances_db, None))

    def test_store_failed_balance_returns_false(self):
        """A failed collect (error set) should NOT be persisted."""
        bal = bc.NeuralWattBalance()
        bal.error = "synthetic failure"
        self.assertFalse(self.store(bal))


# ═══════════════════════════════════════════════════════════════════════════
# neuralwatt_quota_entry — bridge to zai_proxy._snapshot_quota()
# ═══════════════════════════════════════════════════════════════════════════

class TestNeuralWattQuotaEntry(NeuralWattFixture):

    def test_quota_entry_returns_used_pct_remaining_total(self):
        # Seed $5 today + $35 two days ago so:
        #   total_spent = $40 (used_pct=40%), daily_spent=$5 (<cap, not exceeded)
        _seed_call(self.uconn, 5.0)
        _seed_call(self.uconn, 35.0, ts=_epoch_for(days_ago=2))
        bal = self.collect(starting=100.0, daily_cap=10.0)
        self.store(bal)
        entry = self.quota_entry()
        self.assertIsInstance(entry, dict)
        self.assertIn("used_pct", entry)
        self.assertIn("remaining", entry)
        self.assertIn("total", entry)
        self.assertAlmostEqual(entry["used_pct"], 40.0, places=2)
        self.assertAlmostEqual(entry["remaining"], 60.0, places=4)
        self.assertAlmostEqual(entry["total"], 100.0)
        # Should also surface daily cap status:
        self.assertIn("is_daily_cap_exceeded", entry)
        self.assertFalse(entry["is_daily_cap_exceeded"])

    def test_quota_entry_empty_returns_cold_start_empty_dict(self):
        """No stored rows → {} (cold-start marker)."""
        entry = self.quota_entry()
        self.assertEqual(entry, {})

    def test_quota_entry_stale_row_returns_empty_with_max_age(self):
        """A row older than max_age → {} (cold-start)."""
        _seed_call(self.uconn, 20.0)
        bal = self.collect(starting=100.0)
        self.store(bal)
        # Force the stored row to be 1 hour old; max_age=60s → stale.
        conn = sqlite3.connect(self.balances_db)
        conn.execute("UPDATE provider_balances SET collected_at = ? WHERE provider='neuralwatt'",
                     (time.time() - 3600,))
        conn.commit()
        conn.close()
        entry = self.quota_entry(max_age=60.0)
        self.assertEqual(entry, {})

    def test_quota_entry_max_age_none_ignores_staleness(self):
        """max_age=None → return most recent row even if old."""
        _seed_call(self.uconn, 20.0)
        bal = self.collect(starting=100.0)
        self.store(bal)
        conn = sqlite3.connect(self.balances_db)
        conn.execute("UPDATE provider_balances SET collected_at = ? WHERE provider='neuralwatt'",
                     (time.time() - 86400,))
        conn.commit()
        conn.close()
        entry = self.quota_entry(max_age=None)
        self.assertIn("used_pct", entry)

    def test_quota_entry_daily_cap_exceeded_raises_used_pct(self):
        """When daily cap exceeded, quota entry signals exhaustion."""
        for _ in range(3):
            _seed_call(self.uconn, 5.0)  # 15 total today
        bal = self.collect(starting=1000.0, daily_cap=10.0)  # not yet exhausted on monthly
        self.store(bal)
        entry = self.quota_entry()
        self.assertTrue(entry["is_daily_cap_exceeded"])
        # With ample monthly starting, used_pct should still reflect actual usage,
        # but is_exhausted should be False (cap is the daily-cap signal).
        self.assertFalse(entry["is_exhausted"])


# ═══════════════════════════════════════════════════════════════════════════
# collect_and_store_neuralwatt — cron-style end-to-end
# ═══════════════════════════════════════════════════════════════════════════

class TestCollectAndStore(NeuralWattFixture):

    def test_end_to_end_store_then_retrieve(self):
        """collect_and_store writes a retrievable row, returns balance."""
        _seed_call(self.uconn, 25.0)
        bal = bc.collect_and_store_neuralwatt(
            usage_db_path=self.usage_db,
            balances_db_path=self.balances_db,
            starting=100.0,
            daily_cap=10.0,
        )
        self.assertIsNotNone(bal)
        self.assertAlmostEqual(bal.total_spent_usd, 25.0, places=4)
        latest = self.latest()
        self.assertIsNotNone(latest)
        self.assertAlmostEqual(latest.remaining_usd, 75.0, places=4)


# ═══════════════════════════════════════════════════════════════════════════
# default_usage_db_path
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
