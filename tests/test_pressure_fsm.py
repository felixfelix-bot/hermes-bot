#!/usr/bin/env python3
"""test_pressure_fsm.py — S2b pressure FSM shadow mode tests (t_4dfaf0d5).

Covers, per task spec and DESIGN-two-layer-pressure-routing.md (D3-D8):
  1. GREEN/AMBER/RED band transitions from friend 5h used_pct + kalman
     exhausts_in_hours (predictive, uncertainty-adjusted) + monthly
     floor-raiser.
  2. Dwell / hysteresis: 10-min dwell gates de-escalation only; escalation
     is immediate; no flapping around thresholds.
  3. Interactive classifier: session-recency heuristic (api_calls rows for
     the X-Hermes-Session within 10 min => interactive).
  4. Decision matrix + invariants: interactive NEVER downgraded; a
     downgrade is NEVER routed to a paid provider.
  5. Kill switch: .pressure_routing_disabled flag file and
     pressure_policy.json mode=off make everything inert.
  6. Shadow logging: pressure_decisions rows with stable reason codes.
  7. State persistence across restarts (pressure_state.json).
  8. zai_proxy wiring: X-Served-Model/X-Downgrade-Reason headers on the
     existing silent glm-5.3->5.2 ollama rewrite (mock handler), and the
     GET /pressure observability endpoint.

TDD: this file was written RED-first — it must fail before pressure_fsm.py
exists and pass after.

Run:  python3 -m pytest tests/test_pressure_fsm.py -v   (from ~/.hermes/bot)
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
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.expanduser("~/.hermes/bot"))

import pressure_fsm as pf  # noqa: E402


def _inputs(**kw):
    """PressureInputs with green-safe defaults; override per test."""
    base = dict(
        used_pct_5h=10.0,
        exhausts_in_hours=None,
        uncertainty=None,
        will_exhaust=False,
        used_pct_monthly=20.0,
        ollama_regime="included",
        friend_locked=False,
    )
    base.update(kw)
    return pf.PressureInputs(**base)


class TrackerEnv:
    """Temp-dir tracker factory with an injectable clock."""

    def __init__(self, policy: dict | None = None):
        self.dir = tempfile.TemporaryDirectory(prefix="pressure_fsm_")
        self.root = Path(self.dir.name)
        self.db_path = self.root / "usage.db"
        self.state_path = self.root / "pressure_state.json"
        self.policy_path = self.root / "pressure_policy.json"
        self.flag_path = self.root / ".pressure_routing_disabled"
        if policy is not None:
            self.policy_path.write_text(json.dumps(policy))
        self.now = [1_000_000.0]

    def tracker(self) -> pf.PressureTracker:
        return pf.PressureTracker(
            db_path=self.db_path,
            state_path=self.state_path,
            policy_path=self.policy_path,
            flag_path=self.flag_path,
            now=lambda: self.now[0],
        )

    def seed_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS api_calls ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,"
            " key_name TEXT, key_suffix TEXT, model TEXT,"
            " prompt_tokens INTEGER, completion_tokens INTEGER,"
            " total_tokens INTEGER, tier TEXT, cache_hit INTEGER DEFAULT 0,"
            " ollama_hit INTEGER DEFAULT 0, ppq_hit INTEGER DEFAULT 0,"
            " status_code INTEGER, error TEXT, duration_ms INTEGER,"
            " cost_usd REAL DEFAULT NULL, cost_source TEXT DEFAULT NULL,"
            " session_id TEXT DEFAULT NULL)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS kalman_samples ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,"
            " key TEXT NOT NULL, window TEXT, used_pct_observed REAL,"
            " projected_additional_pct REAL, projected_total_pct REAL,"
            " burn_rate_tph REAL, velocity_tph2 REAL, uncertainty REAL,"
            " exhausts_in_hours REAL, will_exhaust INTEGER, note TEXT)")
        conn.commit()
        return conn

    def cleanup(self):
        self.dir.cleanup()


# ── 1. Band transitions ─────────────────────────────────────────────────────

class TestBandTransitions(unittest.TestCase):
    def setUp(self):
        self.env = TrackerEnv()
        self.fsm = self.env.tracker()

    def tearDown(self):
        self.env.cleanup()

    def test_green_stays_green_low_usage(self):
        self.assertEqual(self.fsm.update(_inputs(used_pct_5h=30.0))["state"], "GREEN")

    def test_green_to_amber_at_60(self):
        self.assertEqual(self.fsm.update(_inputs(used_pct_5h=60.0))["state"], "AMBER")

    def test_green_to_amber_above_60(self):
        self.assertEqual(self.fsm.update(_inputs(used_pct_5h=61.0))["state"], "AMBER")

    def test_amber_to_red_at_75(self):
        self.fsm.update(_inputs(used_pct_5h=61.0))
        self.assertEqual(self.fsm.update(_inputs(used_pct_5h=75.0))["state"], "RED")

    def test_amber_holds_between_60_and_75(self):
        self.fsm.update(_inputs(used_pct_5h=61.0))
        self.assertEqual(self.fsm.update(_inputs(used_pct_5h=70.0))["state"], "AMBER")

    def test_deep_spike_escalates_to_red_in_one_update(self):
        """Safety direction: GREEN at 85% must not sit in AMBER."""
        self.assertEqual(self.fsm.update(_inputs(used_pct_5h=85.0))["state"], "RED")

    def test_predictive_green_to_amber_at_3h(self):
        inp = _inputs(used_pct_5h=30.0, exhausts_in_hours=3.5,
                      uncertainty=0.5, will_exhaust=True)   # 3.5-0.5=3.0 <= 3.0
        self.assertEqual(self.fsm.update(inp)["state"], "AMBER")

    def test_predictive_not_triggered_without_will_exhaust(self):
        inp = _inputs(used_pct_5h=30.0, exhausts_in_hours=0.2,
                      uncertainty=0.0, will_exhaust=False)
        self.assertEqual(self.fsm.update(inp)["state"], "GREEN")

    def test_predictive_amber_to_red_at_1h(self):
        self.fsm.update(_inputs(used_pct_5h=61.0))
        inp = _inputs(used_pct_5h=70.0, exhausts_in_hours=1.2,
                      uncertainty=0.3, will_exhaust=True)   # 1.2-0.3=0.9 <= 1.0
        self.assertEqual(self.fsm.update(inp)["state"], "RED")

    def test_no_kalman_data_is_green(self):
        self.assertEqual(self.fsm.update(_inputs(used_pct_5h=None))["state"], "GREEN")


# ── 2. Dwell / hysteresis ───────────────────────────────────────────────────

class TestDwellHysteresis(unittest.TestCase):
    def setUp(self):
        self.env = TrackerEnv()
        self.fsm = self.env.tracker()

    def tearDown(self):
        self.env.cleanup()

    def test_deescalation_blocked_inside_dwell(self):
        self.fsm.update(_inputs(used_pct_5h=61.0))            # -> AMBER @ t0
        self.env.now[0] += 300                                 # 5 min < 10 min
        self.assertEqual(self.fsm.update(_inputs(used_pct_5h=40.0))["state"], "AMBER")

    def test_amber_to_green_after_dwell_at_45(self):
        self.fsm.update(_inputs(used_pct_5h=61.0))
        self.env.now[0] += 601
        self.assertEqual(self.fsm.update(_inputs(used_pct_5h=45.0))["state"], "GREEN")

    def test_amber_stays_above_deescalate_threshold_after_dwell(self):
        self.fsm.update(_inputs(used_pct_5h=61.0))
        self.env.now[0] += 601
        # 50 > 45 deescalate-green but <= 60: AMBER holds
        self.assertEqual(self.fsm.update(_inputs(used_pct_5h=50.0))["state"], "AMBER")

    def test_red_to_amber_after_dwell_at_60(self):
        self.fsm.update(_inputs(used_pct_5h=80.0))             # -> RED
        self.env.now[0] += 601
        self.assertEqual(self.fsm.update(_inputs(used_pct_5h=60.0))["state"], "AMBER")

    def test_red_to_amber_blocked_inside_dwell(self):
        self.fsm.update(_inputs(used_pct_5h=80.0))
        self.env.now[0] += 60
        self.assertEqual(self.fsm.update(_inputs(used_pct_5h=10.0))["state"], "RED")

    def test_red_deescalates_one_step_per_dwell(self):
        """RED -> AMBER -> GREEN needs two dwell windows (anti-flap)."""
        self.fsm.update(_inputs(used_pct_5h=80.0))             # RED  @ t0
        self.env.now[0] += 601
        self.assertEqual(self.fsm.update(_inputs(used_pct_5h=10.0))["state"], "AMBER")
        self.env.now[0] += 601
        self.assertEqual(self.fsm.update(_inputs(used_pct_5h=10.0))["state"], "GREEN")

    def test_escalation_ignores_dwell(self):
        self.fsm.update(_inputs(used_pct_5h=61.0))             # AMBER @ t0
        self.env.now[0] += 1                                   # 1 s later
        self.assertEqual(self.fsm.update(_inputs(used_pct_5h=80.0))["state"], "RED")

    def test_oscillation_near_threshold_does_not_flap(self):
        """61 -> 59 -> 61 within dwell: state must stay AMBER throughout."""
        self.assertEqual(self.fsm.update(_inputs(used_pct_5h=61.0))["state"], "AMBER")
        self.env.now[0] += 60
        self.assertEqual(self.fsm.update(_inputs(used_pct_5h=59.0))["state"], "AMBER")
        self.env.now[0] += 60
        self.assertEqual(self.fsm.update(_inputs(used_pct_5h=61.0))["state"], "AMBER")


# ── 3. Monthly floor-raiser ─────────────────────────────────────────────────

class TestMonthlyFloorRaiser(unittest.TestCase):
    def setUp(self):
        self.env = TrackerEnv()
        self.fsm = self.env.tracker()

    def tearDown(self):
        self.env.cleanup()

    def test_monthly_85_amber_at_50(self):
        inp = _inputs(used_pct_5h=50.0, used_pct_monthly=85.0)
        self.assertEqual(self.fsm.update(inp)["state"], "AMBER")

    def test_monthly_85_red_at_65(self):
        self.fsm.update(_inputs(used_pct_5h=50.0, used_pct_monthly=85.0))
        self.assertEqual(
            self.fsm.update(_inputs(used_pct_5h=65.0, used_pct_monthly=85.0))["state"],
            "RED")

    def test_monthly_84_no_floor_raiser(self):
        inp = _inputs(used_pct_5h=55.0, used_pct_monthly=84.0)
        self.assertEqual(self.fsm.update(inp)["state"], "GREEN")


# ── 4. State persistence ────────────────────────────────────────────────────

class TestStatePersistence(unittest.TestCase):
    def setUp(self):
        self.env = TrackerEnv()

    def tearDown(self):
        self.env.cleanup()

    def test_band_and_dwell_survive_restart(self):
        fsm1 = self.env.tracker()
        fsm1.update(_inputs(used_pct_5h=80.0))                 # RED @ t0
        self.env.now[0] += 601
        # New instance loads persisted state: dwell already served.
        fsm2 = self.env.tracker()
        self.assertEqual(fsm2.update(_inputs(used_pct_5h=60.0))["state"], "AMBER")

    def test_state_file_written(self):
        self.env.tracker().update(_inputs(used_pct_5h=61.0))
        data = json.loads(self.env.state_path.read_text())
        self.assertEqual(data["state"], "AMBER")
        self.assertIn("since", data)
        self.assertIn("updated_at", data)


# ── 5. flat_rate_capacity composite ─────────────────────────────────────────

class TestFlatRateCapacity(unittest.TestCase):
    def setUp(self):
        self.env = TrackerEnv()
        self.fsm = self.env.tracker()

    def tearDown(self):
        self.env.cleanup()

    def test_included_is_ok(self):
        self.assertEqual(self.fsm.flat_rate_capacity(_inputs(ollama_regime="included")), "ok")

    def test_extra_is_ollama_extra(self):
        self.assertEqual(self.fsm.flat_rate_capacity(_inputs(ollama_regime="extra")), "ollama_extra")

    def test_exhausted_is_friend_only(self):
        self.assertEqual(
            self.fsm.flat_rate_capacity(_inputs(ollama_regime="exhausted")), "friend_only")

    def test_paywalled_is_friend_only(self):
        self.assertEqual(
            self.fsm.flat_rate_capacity(_inputs(ollama_regime="paywalled")), "friend_only")

    def test_unknown_regime_conservative_friend_only(self):
        self.assertEqual(self.fsm.flat_rate_capacity(_inputs(ollama_regime=None)), "friend_only")

    def test_friend_locked_and_ollama_dead_is_none(self):
        self.assertEqual(
            self.fsm.flat_rate_capacity(
                _inputs(ollama_regime="exhausted", friend_locked=True)), "none")


# ── 6. Interactive classifier ───────────────────────────────────────────────

class TestInteractiveClassifier(unittest.TestCase):
    def setUp(self):
        self.env = TrackerEnv()
        self.conn = self.env.seed_db()
        self.fsm = self.env.tracker()

    def tearDown(self):
        self.conn.close()
        self.env.cleanup()

    def _add_call(self, session_id, age_s):
        self.conn.execute(
            "INSERT INTO api_calls (ts, key_name, model, status_code, session_id)"
            " VALUES (?, 'friend', 'glm-5.3', 200, ?)",
            (self.env.now[0] - age_s, session_id))
        self.conn.commit()

    def test_recent_session_is_interactive(self):
        self._add_call("sess-A", 300)                            # 5 min ago
        self.assertTrue(self.fsm.classify_interactive("sess-A"))

    def test_session_older_than_10min_is_background(self):
        self._add_call("sess-B", 601)
        self.assertFalse(self.fsm.classify_interactive("sess-B"))

    def test_boundary_exactly_10min_is_interactive(self):
        self._add_call("sess-C", 600)
        self.assertTrue(self.fsm.classify_interactive("sess-C"))

    def test_no_session_is_background(self):
        self.assertFalse(self.fsm.classify_interactive(None))

    def test_unknown_session_is_background(self):
        self._add_call("sess-D", 60)
        self.assertFalse(self.fsm.classify_interactive("never-seen"))

    def test_classifier_never_raises_on_missing_table(self):
        """DB error -> protected (interactive=True), NOT background.

        (Cold review pass 1: the old default was background=False, which
        would have downgraded interactive sessions in enforce mode —
        a D10 invariant breach.)
        """
        env2 = TrackerEnv()
        try:
            self.assertTrue(env2.tracker().classify_interactive("sess-E"))
        finally:
            env2.cleanup()


# ── 7. Decision matrix + invariants ─────────────────────────────────────────

class TestDecisionMatrix(unittest.TestCase):
    def setUp(self):
        self.env = TrackerEnv()
        self.conn = self.env.seed_db()
        self.fsm = self.env.tracker()

    def tearDown(self):
        self.conn.close()
        self.env.cleanup()

    def _decide(self, band_inputs, session_id=None, model="glm-5.3"):
        snap = self.fsm.update(band_inputs)
        return self.fsm.decide(model, session_id, snap)

    # background branch
    def test_bg_green_keeps_53(self):
        d = self._decide(_inputs(used_pct_5h=30.0))
        self.assertEqual((d.would_serve_model, d.would_provider, d.reason),
                         ("glm-5.3", "friend", "bg_kept"))

    def test_bg_amber_ollama_included_downgrades(self):
        d = self._decide(_inputs(used_pct_5h=61.0, ollama_regime="included"))
        self.assertEqual((d.would_serve_model, d.would_provider, d.reason),
                         ("glm-5.2", "ollama_cloud", "bg_downgraded_ollama"))

    def test_bg_red_ollama_included_downgrades(self):
        self.fsm.update(_inputs(used_pct_5h=80.0))
        d = self.fsm.decide("glm-5.3", None,
                            self.fsm.update(_inputs(used_pct_5h=80.0)))
        self.assertEqual((d.would_serve_model, d.would_provider, d.reason),
                         ("glm-5.2", "ollama_cloud", "bg_downgraded_ollama"))

    def test_bg_amber_ollama_extra_downgrades_with_extra_reason(self):
        d = self._decide(_inputs(used_pct_5h=61.0, ollama_regime="extra"))
        self.assertEqual((d.would_serve_model, d.would_provider, d.reason),
                         ("glm-5.2", "ollama_cloud", "bg_downgraded_ollama_extra"))

    def test_bg_amber_ollama_dead_quota_neutral_on_friend(self):
        d = self._decide(_inputs(used_pct_5h=61.0, ollama_regime="exhausted"))
        self.assertEqual((d.would_serve_model, d.would_provider, d.reason),
                         ("glm-5.2", "friend", "bg_quota_neutral"))

    def test_bg_no_flat_capacity_last_resort_53(self):
        d = self._decide(_inputs(used_pct_5h=61.0, ollama_regime="exhausted",
                                 friend_locked=True))
        self.assertEqual((d.would_serve_model, d.would_provider, d.reason),
                         ("glm-5.3", "friend", "bg_last_resort"))

    # interactive branch
    def test_interactive_green_protected(self):
        self.conn.execute(
            "INSERT INTO api_calls (ts, key_name, model, status_code, session_id)"
            " VALUES (?, 'friend', 'glm-5.3', 200, 'ix')",
            (self.env.now[0] - 60,))
        self.conn.commit()
        d = self._decide(_inputs(used_pct_5h=30.0), session_id="ix")
        self.assertEqual((d.would_serve_model, d.would_provider, d.reason),
                         ("glm-5.3", "friend", "interactive_protected"))

    def test_interactive_red_still_53_rationed(self):
        self.conn.execute(
            "INSERT INTO api_calls (ts, key_name, model, status_code, session_id)"
            " VALUES (?, 'friend', 'glm-5.3', 200, 'ix')",
            (self.env.now[0] - 60,))
        self.conn.commit()
        d = self._decide(_inputs(used_pct_5h=80.0), session_id="ix")
        self.assertEqual((d.would_serve_model, d.would_provider, d.reason),
                         ("glm-5.3", "friend", "interactive_rationed"))

    def test_interactive_red_ollama_included_still_53(self):
        """Interactive is NEVER downgraded — even with ollama included."""
        self.conn.execute(
            "INSERT INTO api_calls (ts, key_name, model, status_code, session_id)"
            " VALUES (?, 'friend', 'glm-5.3', 200, 'ix')",
            (self.env.now[0] - 60,))
        self.conn.commit()
        d = self._decide(_inputs(used_pct_5h=80.0, ollama_regime="included"),
                         session_id="ix")
        self.assertEqual(d.would_serve_model, "glm-5.3")
        self.assertEqual(d.reason, "interactive_rationed")

    # non-5.3 models
    def test_non_53_model_passthrough(self):
        d = self._decide(_inputs(used_pct_5h=80.0), model="glm-4.5-flash")
        self.assertEqual((d.would_serve_model, d.reason),
                         ("glm-4.5-flash", "not_glm_53_passthrough"))

    # invariants across the whole matrix
    def test_invariant_interactive_never_downgraded_any_band(self):
        self.conn.execute(
            "INSERT INTO api_calls (ts, key_name, model, status_code, session_id)"
            " VALUES (?, 'friend', 'glm-5.3', 200, 'ix')",
            (self.env.now[0] - 60,))
        self.conn.commit()
        for pct in (30.0, 61.0, 80.0):
            for regime in ("included", "extra", "exhausted", "paywalled", None):
                d = self._decide(_inputs(used_pct_5h=pct, ollama_regime=regime),
                                 session_id="ix")
                self.assertEqual(d.would_serve_model, "glm-5.3",
                                 f"interactive downgraded at pct={pct} regime={regime}")

    def test_invariant_downgrade_never_to_paid_provider(self):
        paid = {"ppq", "openrouter", "telnyx", "deepinfra", "routstr"}
        for pct in (30.0, 61.0, 80.0):
            for regime in ("included", "extra", "exhausted", "paywalled", None):
                d = self._decide(_inputs(used_pct_5h=pct, ollama_regime=regime))
                if d.would_serve_model != d.requested_model:
                    self.assertNotIn(d.would_provider, paid,
                                     f"downgrade routed to paid provider at "
                                     f"pct={pct} regime={regime}")

    def test_decision_carries_state_and_interactive_flag(self):
        d = self._decide(_inputs(used_pct_5h=61.0))
        self.assertEqual(d.state, "AMBER")
        self.assertIs(d.interactive, False)


# ── 8. Kill switch ──────────────────────────────────────────────────────────

class TestKillSwitch(unittest.TestCase):
    def setUp(self):
        self.env = TrackerEnv()
        self.conn = self.env.seed_db()

    def tearDown(self):
        self.conn.close()
        self.env.cleanup()

    def test_flag_file_disables_everything(self):
        fsm = self.env.tracker()
        self.assertTrue(fsm.enabled())
        self.env.flag_path.touch()
        self.assertFalse(fsm.enabled())
        d = fsm.shadow_decision("glm-5.3", session_id=None,
                                ollama_regime="included")
        self.assertIsNone(d)                                    # inert
        rows = self._decision_rows()
        self.assertEqual(len(rows), 0)                          # nothing logged

    def test_policy_mode_off_disables_everything(self):
        env = TrackerEnv(policy={"mode": "off"})
        try:
            fsm = env.tracker()
            self.assertFalse(fsm.enabled())
            self.assertIsNone(fsm.shadow_decision("glm-5.3", None))
            self.assertEqual(fsm.mode(), "off")
        finally:
            env.cleanup()

    def test_policy_missing_defaults_to_shadow(self):
        fsm = self.env.tracker()
        self.assertEqual(fsm.mode(), "shadow")
        self.assertTrue(fsm.enabled())

    def test_policy_mode_shadow_enabled(self):
        env = TrackerEnv(policy={"mode": "shadow"})
        try:
            self.assertTrue(env.tracker().enabled())
        finally:
            env.cleanup()

    def test_corrupt_policy_falls_back_to_defaults(self):
        self.env.policy_path.write_text("{not json")
        fsm = self.env.tracker()
        self.assertEqual(fsm.mode(), "shadow")                  # safe default
        self.assertTrue(fsm.enabled())

    def _decision_rows(self):
        try:
            conn = sqlite3.connect(self.env.db_path)
            rows = conn.execute("SELECT * FROM pressure_decisions").fetchall()
            conn.close()
            return rows
        except sqlite3.Error:
            return []


# ── 9. Shadow logging ───────────────────────────────────────────────────────

class TestShadowLogging(unittest.TestCase):
    def setUp(self):
        self.env = TrackerEnv()
        self.conn = self.env.seed_db()

    def tearDown(self):
        self.conn.close()
        self.env.cleanup()

    def test_shadow_decision_logs_row_with_spec_columns(self):
        fsm = self.env.tracker()
        d = fsm.shadow_decision("glm-5.3", session_id=None,
                                ollama_regime="included")
        self.assertIsNotNone(d)
        rows = self.conn.execute(
            "SELECT ts, state, requested_model, would_serve_model,"
            " would_provider, interactive, reason FROM pressure_decisions"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        ts, state, req, serve, prov, interactive, reason = rows[0]
        self.assertEqual(ts, self.env.now[0])
        self.assertEqual(state, "GREEN")
        self.assertEqual(req, "glm-5.3")
        self.assertEqual(serve, "glm-5.3")
        self.assertEqual(prov, "friend")
        self.assertEqual(interactive, 0)
        self.assertEqual(reason, "bg_kept")

    def test_shadow_decision_gathers_kalman_from_db(self):
        """shadow_decision must read latest friend kalman_samples itself."""
        self.conn.execute(
            "INSERT INTO kalman_samples (ts, key, window, used_pct_observed,"
            " uncertainty, exhausts_in_hours, will_exhaust)"
            " VALUES (?, 'friend', '5-hour', 65.0, 0.5, 9.0, 0)",
            (self.env.now[0] - 60,))
        self.conn.execute(
            "INSERT INTO kalman_samples (ts, key, window, used_pct_observed)"
            " VALUES (?, 'friend', 'monthly', 30.0)",
            (self.env.now[0] - 60,))
        self.conn.commit()
        d = self.env.tracker().shadow_decision("glm-5.3", session_id=None,
                                               ollama_regime="included")
        self.assertEqual(d.state, "AMBER")                      # 65 >= 60
        self.assertEqual(d.would_serve_model, "glm-5.2")        # bg downgrade
        self.assertEqual(d.reason, "bg_downgraded_ollama")

    def test_non_53_not_logged(self):
        fsm = self.env.tracker()
        d = fsm.shadow_decision("glm-5.2", session_id=None,
                                ollama_regime="included")
        self.assertEqual(d.reason, "not_glm_53_passthrough")
        try:
            n = self.conn.execute(
                "SELECT COUNT(*) FROM pressure_decisions").fetchone()[0]
        except sqlite3.OperationalError:
            n = 0  # table never created — nothing was ever logged
        self.assertEqual(n, 0)

    def test_interactive_session_logged_as_interactive(self):
        self.conn.execute(
            "INSERT INTO api_calls (ts, key_name, model, status_code, session_id)"
            " VALUES (?, 'friend', 'glm-5.3', 200, 'ix')",
            (self.env.now[0] - 60,))
        self.conn.commit()
        self.env.tracker().shadow_decision("glm-5.3", session_id="ix",
                                           ollama_regime="included")
        row = self.conn.execute(
            "SELECT interactive, reason FROM pressure_decisions").fetchone()
        self.assertEqual(row[0], 1)
        self.assertEqual(row[1], "interactive_protected")

    def test_logging_failure_never_raises(self):
        env = TrackerEnv()
        try:
            env.db_path.write_text("not a sqlite file")          # corrupt db
            fsm = env.tracker()
            d = fsm.shadow_decision("glm-5.3", session_id=None,
                                    ollama_regime="included")
            self.assertIsNotNone(d)                              # decision still returned
        finally:
            env.cleanup()

    def test_snapshot_returns_state_and_last_decisions(self):
        fsm = self.env.tracker()
        fsm.shadow_decision("glm-5.3", session_id=None, ollama_regime="included")
        snap = fsm.snapshot(limit=5)
        self.assertIn("state", snap)
        self.assertIn("mode", snap)
        self.assertIn("last_decisions", snap)
        self.assertEqual(len(snap["last_decisions"]), 1)
        self.assertEqual(snap["last_decisions"][0]["reason"], "bg_kept")


class ColdReviewFixTests(unittest.TestCase):
    """Hardening fixes from the cold review (GLM pass 1, t_4dfaf0d5).

    Each test maps to a review issue: policy type validation, same-tick
    predictive-escalation cancellation, classifier error direction,
    hot-path connection count/timeout, state-file caching, retention.
    """

    def setUp(self):
        self.env = TrackerEnv()
        self.conn = self.env.seed_db()

    def tearDown(self):
        self.conn.close()
        self.env.cleanup()

    # ── minor 2: policy type validation ─────────────────────────────
    def test_policy_wrong_types_fall_back_to_defaults(self):
        self.env.policy_path.write_text(json.dumps(
            {"dwell_seconds": "abc", "escalate_amber_pct": None,
             "mode": 123}))
        fsm = self.env.tracker()
        self.assertEqual(fsm.mode(), "shadow")  # invalid mode -> default
        # update() must not raise, and dwell must be the default 600s:
        # escalate to AMBER at t0, try to de-escalate at t0+599 (still
        # within default dwell) with used below the de-escalation bar.
        fsm.update(_inputs(used_pct_5h=70.0))
        self.env.now[0] += 599
        snap = fsm.update(_inputs(used_pct_5h=10.0))
        self.assertEqual(snap["state"], "AMBER")  # dwell (default) held it

    # ── minor 1: predictive escalation must not be cancelled ────────
    def test_predictive_red_survives_no_data_same_tick(self):
        # Persisted GREEN, dwell long elapsed, no observed used_pct but
        # Kalman says exhaust within 0.5h -> RED must STICK (the old code
        # immediately de-escalated it to AMBER because no-data == low).
        self.env.state_path.write_text(json.dumps(
            {"state": "GREEN", "since": self.env.now[0] - 99999}))
        fsm = self.env.tracker()
        snap = fsm.update(_inputs(used_pct_5h=None, will_exhaust=True,
                                  exhausts_in_hours=0.5, uncertainty=0.0))
        self.assertEqual(snap["state"], "RED")

    def test_no_data_without_prediction_still_deescalates(self):
        # G2 unchanged: no data + no prediction == low pressure after dwell.
        self.env.state_path.write_text(json.dumps(
            {"state": "RED", "since": self.env.now[0] - 99999}))
        fsm = self.env.tracker()
        snap = fsm.update(_inputs(used_pct_5h=None, will_exhaust=False))
        self.assertEqual(snap["state"], "AMBER")

    # ── minor 4: classifier error direction ─────────────────────────
    def test_classifier_db_error_defaults_interactive(self):
        """DB failure must protect the session (interactive), not expose it.

        Shadow mode only mislogs; in enforce mode (S2c) the old default
        would have downgraded a live interactive session (D10 breach).
        """
        env = TrackerEnv()
        try:
            env.db_path.write_text("not a sqlite file")
            fsm = env.tracker()
            self.assertTrue(fsm.classify_interactive("some-session"))
        finally:
            env.cleanup()

    def test_first_request_of_session_still_background(self):
        """Successful query, no prior row -> background (D4, unchanged)."""
        fsm = self.env.tracker()
        self.assertFalse(fsm.classify_interactive("never-seen"))

    # ── major: hot path budget ──────────────────────────────────────
    def test_shadow_decision_uses_one_connection(self):
        """gather + classify + log must share ONE sqlite connection."""
        fsm = self.env.tracker()
        with patch("pressure_fsm.sqlite3.connect",
                   wraps=sqlite3.connect) as conn_mock:
            fsm.shadow_decision("glm-5.3", session_id="sx",
                                ollama_regime="included")
        self.assertLessEqual(conn_mock.call_count, 1,
                             f"hot path opened {conn_mock.call_count} conns")

    def test_connect_timeout_is_request_safe(self):
        """sqlite busy timeout must be short (<= 1s), never 5s."""
        fsm = self.env.tracker()
        with patch("pressure_fsm.sqlite3.connect",
                   wraps=sqlite3.connect) as conn_mock:
            fsm.shadow_decision("glm-5.3", session_id="sx",
                                ollama_regime="included")
        for call in conn_mock.call_args_list:
            timeout = call.kwargs.get("timeout", 5)
            self.assertLessEqual(timeout, 1.0)

    # ── minor 3: state cached in memory, written atomically ─────────
    def test_state_not_reread_from_disk_per_request(self):
        fsm = self.env.tracker()
        fsm.update(_inputs(used_pct_5h=70.0))          # -> AMBER, persisted
        self.env.state_path.unlink()                    # simulate loss
        self.env.now[0] += 100                          # within dwell (<600s)
        snap = fsm.update(_inputs(used_pct_5h=10.0))    # low input
        # In-memory band must survive disk loss; a disk-reread impl
        # would reset to GREEN (fresh since) and lose the AMBER band.
        self.assertEqual(snap["state"], "AMBER")

    # ── retention: pressure_decisions must not grow unbounded ───────
    def test_old_pressure_decisions_are_pruned(self):
        fsm = self.env.tracker()
        # First call creates the table (log_decision) + a current row.
        fsm.shadow_decision("glm-5.3", session_id=None,
                            ollama_regime="included")
        old = self.env.now[0] - 40 * 86400
        self.conn.execute(
            "INSERT INTO pressure_decisions (ts, state, requested_model,"
            " would_serve_model, would_provider, interactive, reason)"
            " VALUES (?, 'RED', 'glm-5.3', 'glm-5.3', 'friend', 0, 'x')",
            (old,))
        self.conn.commit()
        self.env.now[0] += 3600 * 2  # past the prune interval
        fsm.shadow_decision("glm-5.3", session_id=None,
                            ollama_regime="included")
        ts_min = self.conn.execute(
            "SELECT MIN(ts) FROM pressure_decisions").fetchone()[0]
        self.assertGreater(ts_min, old)

    # ── review pass-2 minors ─────────────────────────────────────────
    def test_snapshot_survives_non_numeric_since(self):
        """Kimi review 2: half-corrupt state file must not break /pressure."""
        fsm = self.env.tracker()
        self.env.state_path.write_text('{"state": "GREEN", "since": "abc"}')
        fsm._state_cache = None  # force disk re-read
        snap = fsm.snapshot()
        self.assertIn(snap["state"], ("GREEN", "AMBER", "RED"))
        self.assertIsInstance(snap["state_age_s"], int)

    def test_policy_clamps_negative_dwell(self):
        """dwell_seconds=-5 must not disable anti-flap."""
        self.env.policy_path.write_text(json.dumps({
            "dwell_seconds": -5}))
        fsm = self.env.tracker()
        pol = fsm._policy()
        self.assertGreaterEqual(pol["dwell_seconds"], 60)

    def test_policy_rejects_inverted_thresholds(self):
        """escalate_amber below deescalate_green (flap machine) -> defaults restored."""
        self.env.policy_path.write_text(json.dumps({
            "escalate_amber_pct": 50, "deescalate_green_pct": 55}))
        fsm = self.env.tracker()
        pol = fsm._policy()
        self.assertGreater(pol["escalate_amber_pct"], pol["deescalate_green_pct"])
        self.assertGreater(pol["escalate_red_pct"], pol["escalate_amber_pct"])
        self.assertGreater(pol["deescalate_amber_pct"], pol["deescalate_green_pct"])


if __name__ == "__main__":
    unittest.main()
