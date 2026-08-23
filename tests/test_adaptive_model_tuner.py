#!/usr/bin/env python3
"""test_adaptive_model_tuner.py — S3a tuner revival tests (t_12f0a395).

Covers, per task spec:
  1. MODEL_MAP current generation: reasoning->glm-5.3, standard->glm-5.2,
     economy->glm-4.5-flash.
  2. FSM band calibration math: percentile analysis of friend 5h-window
     used_pct_observed history -> escalate/de-escalate band thresholds for
     pressure_policy.json (consumed by PressureTracker from S2b).
     - percentile helper (linear interpolation)
     - typical distribution -> P85 amber / P95 red bands + hysteresis
     - ordering invariant required by PressureTracker._policy() validation:
       deescalate_green < deescalate_amber <= escalate_amber < escalate_red
     - low-usage floors and high-usage ceilings (guardrails)
     - minimum-sample gate (< 30 samples -> no policy written)
  3. pressure_policy.json merge-write semantics:
     - foreign keys (e.g. mode=off kill switch, dwell) are PRESERVED
     - atomic write leaves a valid JSON file
     - corrupt pre-existing file is treated as empty, write still succeeds
  4. Integration: PressureTracker actually loads the tuner's bands (its
     range-safety validation does not reject them), and a pre-existing
     mode=off is still honored after a tuner rewrite (the weekly cron can
     never silently re-enable killed routing).
  5. Legacy compat: model_tier_thresholds.json output contract unchanged.
  6. End-to-end run() against a synthetic kalman_samples DB: both files
     written (legacy + policy), dry-run writes neither.

TDD: this file was written RED-first — it must fail before the tuner
revival exists and pass after.

Run:  python3 -m pytest tests/test_adaptive_model_tuner.py -v  (from ~/.hermes/bot)
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.expanduser("~/.hermes/bot"))

import adaptive_model_tuner as amt
import model_tier_router as mtr
from pressure_fsm import PressureTracker


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE kalman_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            key TEXT NOT NULL,
            window TEXT,
            used_pct_observed REAL,
            projected_additional_pct REAL,
            projected_total_pct REAL,
            burn_rate_tph REAL,
            velocity_tph2 REAL,
            uncertainty REAL,
            exhausts_in_hours REAL,
            will_exhaust INTEGER,
            note TEXT
        )""")
    conn.commit()
    conn.close()


def _insert(conn: sqlite3.Connection, ts: float, key: str, window: str,
            used_pct: float | None, exhausts: float | None = None) -> None:
    conn.execute(
        "INSERT INTO kalman_samples (ts, key, window, used_pct_observed,"
        " exhausts_in_hours) VALUES (?, ?, ?, ?, ?)",
        (ts, key, window, used_pct, exhausts))


def make_db(td: Path, used: list, exhaust: list) -> Path:
    """Convenience fixture: kalman_samples db with friend/5-hour rows."""
    db = td / "kalm.db"
    _make_db(db)
    conn = sqlite3.connect(str(db))
    base = 1_700_000_000.0
    for i, u in enumerate(used):
        _insert(conn, base + i, "friend", "5-hour", u, None)
    for i, e in enumerate(exhaust):
        _insert(conn, base + 10_000 + i, "friend", "5-hour", None, e)
    conn.commit()
    conn.close()
    return db


@contextmanager
def capture():
    """redirect_stdout helper; yields a zero-arg callable returning text."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        yield lambda: buf.getvalue()


# Deterministic "typical" distribution: 41-value ramp 10..50 plus a
# 10-value ascending tail. P85 -> 60.0, P95 -> 87.5 (no clamp binds).
TYPICAL = ([float(v) for v in range(10, 51)]
           + [55.0, 58.0, 62.0, 66.0, 70.0, 80.0, 85.0, 90.0, 95.0, 100.0])


class TestModelMap(unittest.TestCase):
    def test_model_map_current_generation(self):
        self.assertEqual(
            mtr.MODEL_MAP,
            {"reasoning": "glm-5.3",
             "standard": "glm-5.2",
             "economy": "glm-4.5-flash"})


class TestPercentileHelper(unittest.TestCase):
    def test_linear_interpolation_midpoint(self):
        self.assertEqual(amt._percentile(list(range(101)), 50.0), 50.0)

    def test_linear_interpolation_fractional(self):
        # idx = 0.3 * 4 = 1.2 -> s[1] + 0.2 * (s[2] - s[1]) = 1.2
        self.assertAlmostEqual(amt._percentile([0.0, 1.0, 2.0, 3.0, 4.0], 30.0), 1.2)

    def test_bounds(self):
        vals = [3.0, 1.0, 2.0, 5.0, 4.0]
        vals.sort()
        self.assertEqual(amt._percentile(vals, 0.0), 1.0)
        self.assertEqual(amt._percentile(vals, 100.0), 5.0)


class TestFsmBandCalibration(unittest.TestCase):
    def test_typical_distribution(self):
        # n=51, sorted: 10..50 (idx 0-40) + [55,58,62,66,70,80,85,90,95,100]
        # P85: idx=42.5 -> s[42]=58, s[43]=62 -> 60.0  (no clamp binds)
        # P95: idx=47.5 -> s[47]=85, s[48]=90 -> 87.5
        pol = amt.compute_fsm_band_policy(TYPICAL)
        self.assertAlmostEqual(pol["escalate_amber_pct"], 60.0)
        self.assertAlmostEqual(pol["escalate_red_pct"], 87.5)
        self.assertAlmostEqual(pol["deescalate_amber_pct"], 60.0)   # design symmetry
        self.assertAlmostEqual(pol["deescalate_green_pct"], 45.0)   # 15pp hysteresis
        self.assertEqual(pol["samples_used"], len(TYPICAL))
        self.assertEqual(pol["source"], "adaptive_percentile_fsm")

    def test_ordering_invariant(self):
        # Property required by PressureTracker._policy() range-safety:
        # desc_green < desc_amber <= esc_amber < esc_red
        import random
        rng = random.Random(42)
        for _ in range(25):
            samples = [round(rng.uniform(0, 100), 2) for _ in range(rng.randint(30, 500))]
            pol = amt.compute_fsm_band_policy(samples)
            self.assertLess(pol["deescalate_green_pct"], pol["deescalate_amber_pct"])
            self.assertLessEqual(pol["deescalate_amber_pct"], pol["escalate_amber_pct"])
            self.assertLess(pol["escalate_amber_pct"], pol["escalate_red_pct"])

    def test_low_usage_floors(self):
        # Mostly-idle system: percentiles near zero -> floors bind.
        samples = [1.0, 2.0, 3.0] * 15 + [5.0] * 5
        pol = amt.compute_fsm_band_policy(samples)
        self.assertEqual(pol["escalate_amber_pct"], 30.0)
        self.assertEqual(pol["escalate_red_pct"], 40.0)      # esc_amber + 10 gap floor
        self.assertEqual(pol["deescalate_amber_pct"], 30.0)
        self.assertEqual(pol["deescalate_green_pct"], 15.0)

    def test_high_usage_ceilings(self):
        # Chronically hot system: amber clamps at 75, red at 95.
        samples = [88.0 + (i % 13) for i in range(200)]
        pol = amt.compute_fsm_band_policy(samples)
        self.assertEqual(pol["escalate_amber_pct"], 75.0)
        self.assertEqual(pol["escalate_red_pct"], 95.0)
        self.assertEqual(pol["deescalate_green_pct"], 60.0)
        self.assertLess(pol["deescalate_green_pct"], pol["deescalate_amber_pct"])

    def test_minimum_sample_gate(self):
        self.assertEqual(amt.compute_fsm_band_policy([10.0] * 29), {})
        self.assertEqual(amt.compute_fsm_band_policy([]), {})
        self.assertNotEqual(amt.compute_fsm_band_policy(TYPICAL), {})


class TestPolicyWrite(unittest.TestCase):
    def test_preserves_foreign_keys(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "pressure_policy.json"
            p.write_text(json.dumps({"mode": "off", "dwell_seconds": 300,
                                     "custom_operator_note": "keep me"}))
            bands = amt.compute_fsm_band_policy(TYPICAL)
            merged = amt.write_pressure_policy(bands, p)
            data = json.loads(p.read_text())
            self.assertEqual(data["mode"], "off")
            self.assertEqual(data["dwell_seconds"], 300)
            self.assertEqual(data["custom_operator_note"], "keep me")
            self.assertEqual(data["escalate_amber_pct"], bands["escalate_amber_pct"])
            self.assertEqual(merged, data)

    def test_atomic_valid_json(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "pressure_policy.json"
            bands = amt.compute_fsm_band_policy(TYPICAL)
            amt.write_pressure_policy(bands, p)
            data = json.loads(p.read_text())
            for k in ("escalate_amber_pct", "escalate_red_pct",
                      "deescalate_amber_pct", "deescalate_green_pct"):
                self.assertIn(k, data)
            # no partial-write temp files left behind
            leftovers = [f for f in os.listdir(td) if f.startswith(".pressure")]
            self.assertEqual(leftovers, [])

    def test_corrupt_existing_treated_as_empty(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "pressure_policy.json"
            p.write_text("{not json!!")
            bands = amt.compute_fsm_band_policy(TYPICAL)
            merged = amt.write_pressure_policy(bands, p)
            self.assertEqual(merged["escalate_amber_pct"], bands["escalate_amber_pct"])


class TestPressureTrackerIntegration(unittest.TestCase):
    def _tracker(self, bot_dir: Path) -> PressureTracker:
        return PressureTracker(
            bot_dir=bot_dir,
            db_path=bot_dir / "zai_usage.db",
            state_path=bot_dir / "pressure_state.json",
            policy_path=bot_dir / "pressure_policy.json",
            flag_path=bot_dir / ".pressure_routing_disabled",
        )

    def test_tracker_loads_tuner_bands(self):
        # The FSM's range-safety validation must ACCEPT tuner output.
        with tempfile.TemporaryDirectory() as td:
            bot = Path(td)
            bands = amt.compute_fsm_band_policy(TYPICAL)
            amt.write_pressure_policy(bands, bot / "pressure_policy.json")
            pol = self._tracker(bot)._policy()
            self.assertAlmostEqual(pol["escalate_amber_pct"], bands["escalate_amber_pct"])
            self.assertAlmostEqual(pol["escalate_red_pct"], bands["escalate_red_pct"])
            self.assertAlmostEqual(pol["deescalate_amber_pct"], bands["deescalate_amber_pct"])
            self.assertAlmostEqual(pol["deescalate_green_pct"], bands["deescalate_green_pct"])
            # keys the tuner does NOT own keep FSM defaults
            self.assertEqual(pol["dwell_seconds"], 600)
            self.assertEqual(pol["mode"], "shadow")

    def test_tuner_rewrite_never_reenables_killed_routing(self):
        # Safety: weekly cron overwriting pressure_policy.json must not
        # resurrect routing an operator killed with mode=off.
        with tempfile.TemporaryDirectory() as td:
            bot = Path(td)
            (bot / "pressure_policy.json").write_text('{"mode": "off"}')
            bands = amt.compute_fsm_band_policy(TYPICAL)
            amt.write_pressure_policy(bands, bot / "pressure_policy.json")
            tracker = self._tracker(bot)
            self.assertEqual(tracker.mode(), "off")
            self.assertFalse(tracker.enabled())

    def test_hot_reload_on_mtime_change(self):
        # Proxy policy cache invalidates on (mtime, size) — confirm the
        # tracker picks up a rewritten policy without restart.
        with tempfile.TemporaryDirectory() as td:
            bot = Path(td)
            tracker = self._tracker(bot)
            bands = amt.compute_fsm_band_policy(TYPICAL)
            amt.write_pressure_policy(bands, bot / "pressure_policy.json")
            pol = tracker._policy()
            self.assertAlmostEqual(pol["escalate_amber_pct"], bands["escalate_amber_pct"])

    def test_set_mode_preserves_tuner_bands(self):
        # S2c enable path (t_4af977e4): flipping mode via the merge
        # writer must keep the tuner's calibrated bands — the enable
        # flip and the weekly tuner cron share this one file.
        with tempfile.TemporaryDirectory() as td:
            bot = Path(td)
            bands = amt.compute_fsm_band_policy(TYPICAL)
            amt.write_pressure_policy(bands, bot / "pressure_policy.json")
            amt.write_pressure_policy({"mode": "enforce"},
                                      bot / "pressure_policy.json")
            tracker = self._tracker(bot)
            self.assertEqual(tracker.mode(), "enforce")
            self.assertTrue(tracker.enabled())
            pol = tracker._policy()
            self.assertAlmostEqual(pol["escalate_amber_pct"], bands["escalate_amber_pct"])
            self.assertAlmostEqual(pol["escalate_red_pct"], bands["escalate_red_pct"])
            self.assertAlmostEqual(pol["deescalate_amber_pct"], bands["deescalate_amber_pct"])
            self.assertAlmostEqual(pol["deescalate_green_pct"], bands["deescalate_green_pct"])
            # hot flip back to shadow is equally non-destructive (exercises
            # the mtime cache invalidation on the production rollback path)
            amt.write_pressure_policy({"mode": "shadow"},
                                      bot / "pressure_policy.json")
            self.assertEqual(tracker.mode(), "shadow")
            self.assertAlmostEqual(tracker._policy()["escalate_red_pct"],
                                   bands["escalate_red_pct"])


class TestSamplesFromDb(unittest.TestCase):
    def test_get_used_pct_samples_filters(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "zai_usage.db"
            _make_db(db)
            conn = sqlite3.connect(str(db))
            base = 1_000_000.0
            for i in range(50):
                _insert(conn, base + i, "friend", "5-hour", float(i % 20))
            _insert(conn, base + 99, "friend", "monthly", 77.0)      # wrong window
            _insert(conn, base + 98, "ours", "5-hour", 88.0)         # wrong key
            _insert(conn, base + 97, "friend", "5-hour", None)       # NULL pct
            conn.commit()
            conn.close()
            got = amt.get_used_pct_samples(db, limit=1000)
            self.assertEqual(len(got), 50)
            self.assertNotIn(77.0, got)
            self.assertNotIn(88.0, got)

    def test_missing_db_returns_empty(self):
        self.assertEqual(amt.get_used_pct_samples(Path("/nonexistent/x.db")), [])


class TestLegacyCompat(unittest.TestCase):
    def test_legacy_threshold_contract(self):
        th = amt.compute_percentile_thresholds([10.0] * 90 + [200.0] * 10)
        self.assertIn("economy_max_hours", th)
        self.assertIn("premium_min_hours", th)
        self.assertEqual(th["source"], "adaptive_percentile")


class TestRunEndToEnd(unittest.TestCase):
    def test_run_writes_both_files(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            db = td / "zai_usage.db"
            _make_db(db)
            conn = sqlite3.connect(str(db))
            base = 1_000_000.0
            for i in range(60):
                _insert(conn, base + i, "friend", "5-hour",
                        used_pct=float(i % 25), exhausts=float(i))
            conn.commit()
            conn.close()
            summary = amt.run(db_path=db,
                              threshold_path=td / "model_tier_thresholds.json",
                              policy_path=td / "pressure_policy.json")
            # legacy file written with its contract
            legacy = json.loads((td / "model_tier_thresholds.json").read_text())
            self.assertIn("economy_max_hours", legacy)
            # policy file written with FSM band keys
            pol = json.loads((td / "pressure_policy.json").read_text())
            for k in ("escalate_amber_pct", "escalate_red_pct",
                      "deescalate_amber_pct", "deescalate_green_pct"):
                self.assertIn(k, pol)
            self.assertTrue(summary["policy_written"])

    def test_run_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            db = td / "zai_usage.db"
            _make_db(db)
            conn = sqlite3.connect(str(db))
            base = 1_000_000.0
            for i in range(60):
                _insert(conn, base + i, "friend", "5-hour",
                        used_pct=float(i % 25), exhausts=float(i))
            conn.commit()
            conn.close()
            amt.run(db_path=db,
                    threshold_path=td / "model_tier_thresholds.json",
                    policy_path=td / "pressure_policy.json",
                    dry_run=True)
            self.assertFalse((td / "model_tier_thresholds.json").exists())
            self.assertFalse((td / "pressure_policy.json").exists())

    def test_run_without_used_pct_history_skips_policy(self):
        # exhaust-only history: legacy still written, policy not.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            db = td / "zai_usage.db"
            _make_db(db)
            conn = sqlite3.connect(str(db))
            base = 1_000_000.0
            for i in range(60):
                _insert(conn, base + i, "friend", "5-hour",
                        used_pct=None, exhausts=float(i))
            conn.commit()
            conn.close()
            summary = amt.run(db_path=db,
                              threshold_path=td / "model_tier_thresholds.json",
                              policy_path=td / "pressure_policy.json")
            self.assertFalse(summary["policy_written"])
            self.assertTrue((td / "model_tier_thresholds.json").exists())
            self.assertFalse((td / "pressure_policy.json").exists())


class TestRouterBehavior(unittest.TestCase):
    """model_tier_router public API through patched paths (no live-file coupling)."""

    def test_base_tier_peak_forces_economy(self):
        with mock.patch.object(mtr, "is_peak_hour", return_value=True):
            self.assertEqual(mtr.compute_base_tier("friend"), mtr.TIER_ECONOMY)

    def test_base_tier_no_live_data_falls_back_standard(self):
        with mock.patch.object(mtr, "is_peak_hour", return_value=False), \
             mock.patch.object(mtr, "PROXY_STATE_PATH", Path("/nonexistent/state.json")):
            self.assertEqual(mtr.compute_base_tier("friend"), mtr.TIER_STANDARD)

    def test_base_tier_threshold_banding(self):
        # exhaust 1.5h <= p10 -> economy; 10h between -> standard; 40h > p90 -> reasoning
        for exhaust, expected in [(1.5, "economy"), (10.0, "standard"), (40.0, "reasoning")]:
            with tempfile.TemporaryDirectory() as td:
                td = Path(td)
                (td / "th.json").write_text(json.dumps(
                    {"friend": {"p10_exhaust": 2.0, "p90_exhaust": 30.0}}))
                (td / "state.json").write_text(json.dumps(
                    {"friend": {"predictions": [{"exhausts_in_hours": exhaust}]}}))
                with mock.patch.object(mtr, "is_peak_hour", return_value=False), \
                     mock.patch.object(mtr, "THRESHOLDS_PATH", td / "th.json"), \
                     mock.patch.object(mtr, "PROXY_STATE_PATH", td / "state.json"):
                    self.assertEqual(mtr.compute_base_tier("friend"), expected,
                                     f"exhaust={exhaust}")

    def test_effective_model_maps_current_generation(self):
        for tier, model in [("reasoning", "glm-5.3"), ("standard", "glm-5.2"),
                            ("economy", "glm-4.5-flash")]:
            with mock.patch.object(mtr, "compute_base_tier", return_value=tier):
                self.assertEqual(mtr.compute_effective_model("friend"), model)

    def test_effective_tier_urgency_and_hints(self):
        # background in economy stays economy (dispatcher defers)
        self.assertEqual(mtr.compute_effective_tier("economy", urgency="background"),
                         "economy")
        # cheap hint forces economy from any tier
        self.assertEqual(mtr.compute_effective_tier("reasoning", task_hint="cheap"),
                         "economy")
        # reasoning hint upgrades from standard but not from economy
        self.assertEqual(mtr.compute_effective_tier("standard", task_hint="reasoning"),
                         "reasoning")
        self.assertEqual(mtr.compute_effective_tier("economy", task_hint="reasoning"),
                         "economy")
        # unknown hint is a no-op
        self.assertEqual(mtr.compute_effective_tier("standard", task_hint="zzz"),
                         "standard")

    def test_compute_tier_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "th.json").write_text(json.dumps(
                {"friend": {"p10_exhaust": 2.0, "p90_exhaust": 30.0}}))
            (td / "state.json").write_text(json.dumps(
                {"friend": {"predictions": [{"exhausts_in_hours": 40.0}]}}))
            with mock.patch.object(mtr, "is_peak_hour", return_value=True), \
                 mock.patch.object(mtr, "THRESHOLDS_PATH", td / "th.json"), \
                 mock.patch.object(mtr, "PROXY_STATE_PATH", td / "state.json"):
                res = mtr.compute_tier("friend", urgency="background")
            self.assertEqual(res["tier"], "economy")
            self.assertEqual(res["model"], "glm-4.5-flash")
            self.assertFalse(res["dispatch_ok"])
            self.assertIn("peak hours", res["reason"])


class TestTunerCliCoverage(unittest.TestCase):
    """CLI/stats paths of adaptive_model_tuner against fixture DBs."""

    def test_show_stats_empty(self):
        with capture() as out:
            amt.show_stats([])
        self.assertIn("No historic data", out())

    def test_percentile_empty_raises(self):
        with self.assertRaises(ValueError):
            amt._percentile([], 50.0)

    def test_get_hours_left_samples_missing_db(self):
        self.assertEqual(amt.get_hours_left_samples(Path("/nonexistent/x.db")), [])

    def test_db_without_table_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "empty.db"
            db.write_text("")  # valid sqlite connect, no kalman_samples table
            self.assertEqual(amt.get_used_pct_samples(db), [])
            self.assertEqual(amt.get_hours_left_samples(db), [])

    def test_nonfinite_samples_excluded(self):
        # sqlite happily stores 9e999 as REAL inf; NaN-normalizing rows would
        # poison sorted() + json.dumps (NaN → invalid JSON). Cold-review minor.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            db = make_db(td, used=[10.0] * 40, exhaust=[10.0] * 40)
            conn = sqlite3.connect(str(db))
            conn.execute(
                "INSERT INTO kalman_samples (ts, key, window, used_pct_observed,"
                " exhausts_in_hours) VALUES (?, ?, ?, ?, ?)",
                (1_800_000_000.0, "friend", "5-hour", 9e999, 9e999))
            conn.commit()
            conn.close()
            used = amt.get_used_pct_samples(db)
            self.assertNotIn(float("inf"), used)
            self.assertEqual(len(used), 40)
            hours = amt.get_hours_left_samples(db)
            self.assertNotIn(float("inf"), hours)
            self.assertEqual(len(hours), 40)

    def test_stats_mode_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            db = make_db(td, used=[float(i) for i in range(50)],
                         exhaust=[10.0] * 50)
            with capture() as out:
                res = amt.run(db_path=db, threshold_path=td / "t.json",
                              policy_path=td / "p.json", stats=True)
            self.assertEqual(res, {"legacy_written": False, "policy_written": False})
            self.assertFalse((td / "t.json").exists())
            self.assertFalse((td / "p.json").exists())
            self.assertIn("FSM bands from 50 used_pct samples", out())

    def test_stats_mode_sparse_samples(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            db = make_db(td, used=[5.0] * 10, exhaust=[10.0] * 10)
            with capture() as out:
                amt.run(db_path=db, threshold_path=td / "t.json",
                        policy_path=td / "p.json", stats=True)
            self.assertIn("SKIPPED", out())

    def test_main_dry_run_flag(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            db = make_db(td, used=[float(i) for i in range(40)],
                         exhaust=[10.0] * 40)
            with mock.patch.object(amt, "DB_PATH", db), \
                 mock.patch.object(amt, "THRESHOLD_FILE", td / "t.json"), \
                 mock.patch.object(amt, "PRESSURE_POLICY_FILE", td / "p.json"), \
                 mock.patch.object(sys, "argv",
                                   ["adaptive_model_tuner.py", "--dry-run"]), \
                 capture() as out:
                amt.main()
            self.assertIn("DRY RUN", out())
            self.assertFalse((td / "t.json").exists())
            self.assertFalse((td / "p.json").exists())


if __name__ == "__main__":
    unittest.main()