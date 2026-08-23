#!/usr/bin/env python3
"""Tests for the D6 PPQ good-use policy in zai_proxy.py.

Fixture-based — no live calls, no network, no writes to the production
zai_usage.db: a temp sqlite connection is injected into zai_proxy's
`_usage_db` singleton (`_usage_db()` returns the pre-set connection).

Run:  python3 -m pytest test_ppq_policy.py -v   (from ~/.hermes/bot)
  or: python3 test_ppq_policy.py               (plain unittest runner)
"""

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import zai_proxy  # noqa: E402  (import is safe: server/threads start only under __main__)


class PPQPolicyFixture(unittest.TestCase):
    """Temp DB + clean in-memory storm state + tight caps per test."""

    CAP = 2.0          # $/day (same as prod default; overridden per-test as needed)
    MAX_HOURLY = 3     # tight so tests don't need 20 iterations
    STORM_HITS = 3
    STORM_WINDOW = 600

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        conn = sqlite3.connect(self._tmp.name, isolation_level=None,
                               check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        # Inject BEFORE any _ppq_* call: _usage_db() returns the singleton.
        self.db: sqlite3.Connection = conn
        self._orig_conn = zai_proxy._usage_db_conn
        zai_proxy._usage_db_conn = conn
        # Prime both schemas so seed/inspect helpers can run before any
        # _ppq_* call creates them lazily.
        zai_proxy._ppq_usage_row()
        conn.execute(zai_proxy._PPQ_ANOMALY_SCHEMA)
        # Clean in-memory storm tracker + policy cache.
        self._orig_attempts = dict(zai_proxy._ppq_prompt_attempts)
        zai_proxy._ppq_prompt_attempts.clear()
        self._env = {
            "PPQ_DAILY_CAP_USD": str(self.CAP),
            "PPQ_MAX_REQ_PER_HOUR": str(self.MAX_HOURLY),
            "PPQ_STORM_MIN_HITS": str(self.STORM_HITS),
            "PPQ_STORM_WINDOW_S": str(self.STORM_WINDOW),
            "PPQ_POLICY_ENABLED": "1",
        }
        self._orig_env = {k: os.environ.get(k) for k in self._env}
        os.environ.update(self._env)
        self._reset_policy_cache()

    def tearDown(self):
        zai_proxy._usage_db_conn.close()
        zai_proxy._usage_db_conn = self._orig_conn
        zai_proxy._ppq_prompt_attempts.clear()
        zai_proxy._ppq_prompt_attempts.update(self._orig_attempts)
        for k, v in self._orig_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._reset_policy_cache()
        os.unlink(self._tmp.name)

    # ── helpers ──────────────────────────────────────────────────────────────
    def _reset_policy_cache(self):
        zai_proxy._ppq_policy_cache = (0.0, {})

    def seed_usage(self, spend=0.0, requests=0, hour_requests=None, storm=0):
        self.db.execute(
            "INSERT OR REPLACE INTO ppq_daily_used "
            "(date, spend_usd, requests, tokens, storm_blocked, hour_requests, last_ts) "
            "VALUES (?,?,?,?,?,?,?)",
            (time.strftime("%Y-%m-%d"), spend, requests, 0, storm,
             json.dumps(hour_requests or {}), time.time()))

    def unresolved_alerts(self):
        return self.db.execute(
            "SELECT severity, title, detail FROM anomaly_events "
            "WHERE category='ppq_budget' AND resolved=0").fetchall()


class TestGateFreshState(PPQPolicyFixture):
    def test_fresh_state_allows(self):
        ok, why = zai_proxy._ppq_gate_ok("a" * 64)
        self.assertTrue(ok)
        self.assertEqual(why, "ok")

    def test_policy_disabled_allows_everything(self):
        os.environ["PPQ_POLICY_ENABLED"] = "0"
        self._reset_policy_cache()
        self.seed_usage(spend=99.0)
        ok, why = zai_proxy._ppq_gate_ok("a" * 64)
        self.assertTrue(ok)
        self.assertEqual(why, "policy_disabled")


class TestDailyCap(PPQPolicyFixture):
    def test_at_cap_blocks_and_alerts_critical(self):
        self.seed_usage(spend=self.CAP)
        ok, why = zai_proxy._ppq_gate_ok("b" * 64)
        self.assertFalse(ok)
        self.assertIn("daily_cap", why)
        alerts = self.unresolved_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0][0], "critical")
        self.assertIn("daily cap reached", alerts[0][1])

    def test_alert_deduped_across_gate_calls(self):
        self.seed_usage(spend=self.CAP)
        zai_proxy._ppq_gate_ok("b" * 64)
        zai_proxy._ppq_gate_ok("b" * 64)
        zai_proxy._ppq_gate_ok("c" * 64)
        self.assertEqual(len(self.unresolved_alerts()), 1)

    def test_below_cap_allows(self):
        self.seed_usage(spend=self.CAP - 0.01)
        ok, _ = zai_proxy._ppq_gate_ok("b" * 64)
        self.assertTrue(ok)


class TestHourlyCap(PPQPolicyFixture):
    def test_hourly_cap_blocks(self):
        bucket = zai_proxy._ppq_hour_bucket()
        self.seed_usage(hour_requests={bucket: self.MAX_HOURLY})
        ok, why = zai_proxy._ppq_gate_ok("d" * 64)
        self.assertFalse(ok)
        self.assertIn("hourly_cap", why)

    def test_previous_hours_do_not_count(self):
        bucket = zai_proxy._ppq_hour_bucket()
        prev = bucket[:-1] + str(int(bucket[-1]) - 1 if bucket[-1] != "0" else 23)
        self.seed_usage(hour_requests={prev: 99})
        ok, _ = zai_proxy._ppq_gate_ok("d" * 64)
        self.assertTrue(ok)


class TestRetryStorm(PPQPolicyFixture):
    BODY = json.dumps({"model": "glm-5.2", "messages": [{"role": "user",
                   "content": "reproduce crash"}]}).encode()

    def test_same_prompt_below_threshold_ok(self):
        h = zai_proxy._ppq_hash_body(self.BODY)
        zai_proxy._ppq_note_attempt(h)
        zai_proxy._ppq_note_attempt(h)
        ok, _ = zai_proxy._ppq_gate_ok(h)
        self.assertTrue(ok)  # 2 attempts < storm_min_hits(3)

    def test_same_prompt_storm_blocked(self):
        h = zai_proxy._ppq_hash_body(self.BODY)
        for _ in range(self.STORM_HITS):
            zai_proxy._ppq_note_attempt(h)
        ok, why = zai_proxy._ppq_gate_ok(h)
        self.assertFalse(ok)
        self.assertIn("retry_storm", why)
        # storm_blocked counter incremented
        row = zai_proxy._ppq_usage_row()
        self.assertEqual(row["storm_blocked"], 1)
        # warning raised through the anomaly chain
        alerts = self.unresolved_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0][0], "warning")
        self.assertIn("retry-storm blocked", alerts[0][1])

    def test_other_prompt_unaffected_by_storm(self):
        h = zai_proxy._ppq_hash_body(self.BODY)
        for _ in range(self.STORM_HITS):
            zai_proxy._ppq_note_attempt(h)
        ok, _ = zai_proxy._ppq_gate_ok("e" * 64)
        self.assertTrue(ok)

    def test_stale_attempts_expire_out_of_window(self):
        h = zai_proxy._ppq_hash_body(self.BODY)
        now = time.time()
        # Seed attempts older than the storm window directly in memory.
        zai_proxy._ppq_prompt_attempts[h] = [now - self.STORM_WINDOW - 5] * 10
        ok, _ = zai_proxy._ppq_gate_ok(h)
        self.assertTrue(ok)

    def test_hash_is_content_addressed(self):
        a = zai_proxy._ppq_hash_body(self.BODY)
        b = zai_proxy._ppq_hash_body(self.BODY + b"x")
        self.assertNotEqual(a, b)
        self.assertEqual(len(a), 64)  # sha256 hex


class TestRecordSuccess(PPQPolicyFixture):
    def test_records_spend_requests_tokens_and_hour(self):
        zai_proxy._ppq_record_success(100_000, cost_usd=0.30)
        row = zai_proxy._ppq_usage_row()
        self.assertAlmostEqual(row["spend_usd"], 0.30, places=4)
        self.assertEqual(row["requests"], 1)
        self.assertEqual(row["tokens"], 100_000)
        hours = json.loads(row["hour_requests"])
        self.assertEqual(hours.get(zai_proxy._ppq_hour_bucket()), 1)

    def test_accumulates_and_counts_hour(self):
        zai_proxy._ppq_record_success(10, cost_usd=0.9)
        zai_proxy._ppq_record_success(10, cost_usd=0.9)
        row = zai_proxy._ppq_usage_row()
        self.assertAlmostEqual(row["spend_usd"], 1.8, places=4)
        self.assertEqual(row["requests"], 2)
        hours = json.loads(row["hour_requests"])
        self.assertEqual(hours.get(zai_proxy._ppq_hour_bucket()), 2)

    def test_eighty_pct_alert_fires_once_on_crossing(self):
        # cap 2.0 -> alert at 1.6. 0.8+0.8=1.6 crosses; another 0.1 stays quiet.
        zai_proxy._ppq_record_success(10, cost_usd=0.8)
        self.assertEqual(len(self.unresolved_alerts()), 0)
        zai_proxy._ppq_record_success(10, cost_usd=0.8)   # crosses 1.6
        alerts = self.unresolved_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0][0], "warning")
        self.assertIn("80% of cap", alerts[0][1])
        zai_proxy._ppq_record_success(10, cost_usd=0.1)   # no re-crossing
        self.assertEqual(len(self.unresolved_alerts()), 1)

    def test_cost_fallback_estimates_when_none(self):
        # No actual cost -> falls back to _estimate_cost_usd("ppq", tokens).
        with mock.patch.object(zai_proxy, "_estimate_cost_usd",
                               return_value=0.42) as est:
            zai_proxy._ppq_record_success(123, cost_usd=None)
            est.assert_called_once_with("ppq", 123)
        row = zai_proxy._ppq_usage_row()
        self.assertAlmostEqual(row["spend_usd"], 0.42, places=4)

    def test_inf_cost_does_not_poison_row(self):
        zai_proxy._ppq_record_success(10, cost_usd=float("inf"))
        row = zai_proxy._ppq_usage_row()
        self.assertEqual(row["spend_usd"], 0.0)
        self.assertEqual(row["requests"], 1)

    def test_recorded_spend_then_gate_blocks_at_cap(self):
        for _ in range(4):
            zai_proxy._ppq_record_success(10, cost_usd=0.75)
        ok, why = zai_proxy._ppq_gate_ok("f" * 64)
        self.assertFalse(ok)
        self.assertIn("daily_cap", why)


class TestPolicyConfig(PPQPolicyFixture):
    def test_env_overrides_defaults(self):
        os.environ["PPQ_DAILY_CAP_USD"] = "0.5"
        self._reset_policy_cache()
        pol = zai_proxy._ppq_policy()
        self.assertEqual(pol["daily_cap_usd"], 0.5)
        self.assertEqual(pol["max_requests_per_hour"], self.MAX_HOURLY)

    def test_json_file_overrides_defaults_env_wins(self):
        import zai_proxy as zp
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as tf:
            json.dump({"daily_cap_usd": 7.0, "alert_pct": 0.5}, tf)
        orig_file = zp.PPQ_POLICY_FILE
        zp.PPQ_POLICY_FILE = Path(tf.name)
        try:
            os.environ["PPQ_DAILY_CAP_USD"] = "9.0"   # env beats file
            self._reset_policy_cache()
            pol = zp._ppq_policy()
            self.assertEqual(pol["daily_cap_usd"], 9.0)   # env wins over file's 7.0
            self.assertEqual(pol["alert_pct"], 0.5)       # file applied (no env var)
            self.assertEqual(pol["max_requests_per_hour"], self.MAX_HOURLY)  # env
        finally:
            zp.PPQ_POLICY_FILE = orig_file
            os.unlink(tf.name)

    def test_garbage_env_ignored(self):
        os.environ["PPQ_DAILY_CAP_USD"] = "not-a-number"
        self._reset_policy_cache()
        pol = zai_proxy._ppq_policy()
        self.assertEqual(pol["daily_cap_usd"], self.CAP)  # env var skipped


class TestAnomalyRollover(PPQPolicyFixture):
    def test_stale_alerts_resolved_next_day(self):
        conn = self.db
        conn.execute(zai_proxy._PPQ_ANOMALY_SCHEMA)
        yesterday = time.time() - 86400
        conn.execute(
            "INSERT INTO anomaly_events (ts, severity, category, title, detail, "
            "alerted, resolved) VALUES (?, 'warning', 'ppq_budget', 'old', '', 1, 0)",
            (yesterday,))
        zai_proxy._ppq_usage_row()   # rollover hygiene runs on read
        n = conn.execute(
            "SELECT COUNT(*) FROM anomaly_events "
            "WHERE category='ppq_budget' AND resolved=0").fetchone()[0]
        self.assertEqual(n, 0)


class TestFailoverIntegration(PPQPolicyFixture):
    """The patched _try_external_failover honours the gate (static check)."""

    def test_gate_called_in_failover_candidate_loop(self):
        src = Path(zai_proxy.__file__).read_text()
        self.assertIn('_ppq_gate_ok(_ppq_hash_body(body))', src)
        self.assertIn('_ppq_note_attempt(_ppq_hash_body(body))', src)
        self.assertIn('_ppq_record_success(ext_tokens, ext_cost_usd)', src)

    def test_usage_row_shape_stable(self):
        row = zai_proxy._ppq_usage_row()
        for key in ("date", "spend_usd", "requests", "tokens",
                    "storm_blocked", "hour_requests", "last_ts"):
            self.assertIn(key, row)
        self.assertEqual(row["requests"], 0)
        self.assertEqual(json.loads(row["hour_requests"]), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
