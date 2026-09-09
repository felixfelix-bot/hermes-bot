#!/usr/bin/env python3
"""viz_coverage_survey + price_viz overlay merge tests.

Covers:
  * ignore-list filtering (test keys, above-quota pseudo-lanes, meta keys)
  * /quota tier inference (included / balance / unknown)
  * seed-rate derivation (seed table > real rates > twin > tier default)
  * metadata derivation (token lane capacity from /quota, per-token no lane)
  * live_universe composition + filtering
  * every-Nth-run gating (run_if_due)
  * overlay persist/load round-trip
  * price_viz _merge_overlay + deterministic _derive_color/_derive_linestyle
"""
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import viz_coverage_survey as vcs


# ── Ignore list ───────────────────────────────────────────────────────────────

class TestIgnore(unittest.TestCase):
    def test_ignores_test_suffix(self):
        self.assertTrue(vcs._ignored("telnyx_test"))

    def test_ignores_extra_prefix(self):
        self.assertTrue(vcs._ignored("ollama_cloud_extra"))

    def test_keeps_real_lane(self):
        self.assertFalse(vcs._ignored("ollama_cloud_4"))
        self.assertFalse(vcs._ignored("chutes"))


# ── Tier inference ────────────────────────────────────────────────────────────

class TestInferTier(unittest.TestCase):
    def test_included(self):
        q = {"x": {"regime": "included", "total": 500000000}}
        self.assertEqual(vcs._infer_tier("x", q), "included")

    def test_exhausted_subscription(self):
        q = {"x": {"regime": "exhausted", "total": 3500000000}}
        self.assertEqual(vcs._infer_tier("x", q), "included")

    def test_balance(self):
        q = {"x": {"is_exhausted": True, "remaining": 0.0, "total": 13.33}}
        self.assertEqual(vcs._infer_tier("x", q), "balance")

    def test_unknown(self):
        q = {"x": {"regime": "weird"}}
        self.assertIsNone(vcs._infer_tier("x", q))
        self.assertIsNone(vcs._infer_tier("missing", q))


# ── Seed derivation ───────────────────────────────────────────────────────────

class TestDeriveSeed(unittest.TestCase):
    def test_from_seed_table(self):
        self.assertEqual(vcs._derive_seed("chutes", "per_token", {"chutes": 0.096}), 0.096)

    def test_included_uses_twin_before_real_rate(self):
        with patch.object(vcs, "_twin_seed", return_value=0.40), \
             patch.object(vcs, "_real_price_rates", return_value={"x": 0.0155}):
            self.assertEqual(vcs._derive_seed("x", "included", {}), 0.40)

    def test_per_token_uses_real_rate(self):
        with patch.object(vcs, "_real_price_rates", return_value={"newp": 0.7}):
            self.assertEqual(vcs._derive_seed("newp", "per_token", {}), 0.7)

    def test_per_token_default(self):
        with patch.object(vcs, "_real_price_rates", return_value={}), \
             patch.object(vcs, "_twin_seed", return_value=None):
            self.assertEqual(vcs._derive_seed("newp", "per_token", {}), 1.0)


# ── Metadata derivation ───────────────────────────────────────────────────────

class TestDeriveMetadata(unittest.TestCase):
    def test_token_lane_from_quota(self):
        meta = vcs.derive_metadata(
            "ollama_cloud_3", {"ollama_cloud_3": "included"}, {"ollama_cloud_3": 0.4},
            {"ollama_cloud_3": {"regime": "included", "total": 3500000000}})
        self.assertEqual(meta["tier"], "included")
        self.assertEqual(meta["seed_rate"], 0.4)
        self.assertEqual(meta["lane"]["kind"], "token")
        self.assertEqual(meta["lane"]["capacity"], 3500000000)

    def test_per_token_no_lane(self):
        meta = vcs.derive_metadata("chutes", {"chutes": "per_token"}, {"chutes": 0.096}, {})
        self.assertEqual(meta["tier"], "per_token")
        self.assertNotIn("lane", meta)

    def test_infinite_total_not_a_token_cap(self):
        meta = vcs.derive_metadata(
            "opencode_go", {"opencode_go": "flat"}, {"opencode_go": 0.4},
            {"opencode_go": {"regime": "exhausted", "total": float("inf")}})
        self.assertIsNotNone(meta)
        self.assertNotIn("lane", meta)

    def test_no_tier_returns_none(self):
        self.assertIsNone(vcs.derive_metadata("unknown", {}, {}, {}))


# ── Universe composition ──────────────────────────────────────────────────────

class TestUniverse(unittest.TestCase):
    def test_universe_filters_meta_and_ignored(self):
        fake_fr = types.SimpleNamespace(PROVIDER_MODELS={"a": set()})
        with patch.object(vcs, "_flat_router_tables", return_value=({"a": "quota"}, {"a": 0.1})), \
             patch.object(vcs, "_fetch_quota", return_value={
                 "active": "friend", "proactive_cooldown": {},
                 "b": {"regime": "included"}, "telnyx_test": {}}), \
             patch.object(vcs, "_real_price_rates", return_value={"c": 0.5}), \
             patch.dict("sys.modules", {"flat_router": fake_fr}):
            uni = vcs.live_universe()
        self.assertEqual(uni, {"a", "b", "c"})


# ── Every-Nth-run gating ──────────────────────────────────────────────────────

class TestRunIfDue(unittest.TestCase):
    def test_skips_until_nth(self):
        with tempfile.TemporaryDirectory() as d:
            counter = Path(d) / "counter.json"
            with patch.object(vcs, "COUNTER_PATH", counter), \
                 patch.object(vcs, "run", return_value={"added": []}) as run_mock:
                for i in range(1, 5):
                    self.assertIsNone(vcs.run_if_due(), f"run {i} should skip")
                # 5th run fires
                result = vcs.run_if_due()
                self.assertIsNotNone(result)
                run_mock.assert_called_once()

    def test_force_runs_immediately(self):
        with tempfile.TemporaryDirectory() as d:
            counter = Path(d) / "counter.json"
            with patch.object(vcs, "COUNTER_PATH", counter), \
                 patch.object(vcs, "run", return_value={"added": ["x"]}):
                self.assertIsNotNone(vcs.run_if_due(force=True))


# ── Overlay round-trip ────────────────────────────────────────────────────────

class TestOverlay(unittest.TestCase):
    def test_load_persist_roundtrip(self):
        overlay = {"providers": {"chutes": {"tier": "per_token", "seed_rate": 0.096}},
                   "lanes": {"ollama_cloud_3": {"kind": "token", "capacity": 3500000000}}}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "overlay.json"
            with patch.object(vcs, "OVERLAY_PATH", path):
                vcs._persist_overlay(overlay)
                self.assertEqual(vcs._load_overlay()["providers"]["chutes"]["tier"], "per_token")
                self.assertEqual(vcs._load_overlay()["lanes"]["ollama_cloud_3"]["capacity"], 3500000000)

    def test_load_missing_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "nope.json"
            with patch.object(vcs, "OVERLAY_PATH", path):
                self.assertEqual(vcs._load_overlay(), {"providers": {}, "lanes": {}})


# ── price_viz merge + display derivation ──────────────────────────────────────

class TestPriceVizOverlay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("price_viz", REPO_ROOT / "price_viz.py")
        cls.pv = importlib.util.module_from_spec(spec)
        sys.modules["price_viz"] = cls.pv
        spec.loader.exec_module(cls.pv)

    def test_derive_color_stable_and_distinct(self):
        c1 = self.pv._derive_color("chutes")
        c2 = self.pv._derive_color("chutes")
        self.assertEqual(c1, c2)
        self.assertNotIn(c1, set(self.pv.PROVIDER_COLORS.values()))

    def test_derive_linestyle_stable(self):
        self.assertEqual(self.pv._derive_linestyle("deepseek"), self.pv._derive_linestyle("deepseek"))

    def test_merge_overlay_adds_provider_and_lane(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "overlay.json"
            path.write_text(json.dumps({
                "providers": {"survey_test": {"tier": "per_token", "seed_rate": 0.123}},
                "lanes": {"survey_lane": {"kind": "token", "capacity": 111, "session_capacity": 22}},
            }))
            with patch.object(self.pv, "VIZ_OVERLAY_PATH", path):
                self.pv._merge_overlay()
            self.assertEqual(self.pv.PROVIDER_TIER["survey_test"], "per_token")
            self.assertEqual(self.pv.SEED_RATES["survey_test"], 0.123)
            self.assertIn("survey_test", self.pv.PROVIDER_COLORS)
            self.assertIn("survey_test", self.pv.PROVIDER_LINESTYLES)
            self.assertEqual(self.pv.LANE_REGISTRY_STATIC["survey_lane"]["capacity"], 111)

    def test_merge_overlay_idempotent_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(self.pv, "VIZ_OVERLAY_PATH", Path(d) / "nope.json"):
                self.pv._merge_overlay()  # must not raise


if __name__ == "__main__":
    unittest.main()
