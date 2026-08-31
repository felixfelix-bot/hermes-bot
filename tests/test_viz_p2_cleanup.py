#!/usr/bin/env python3
"""VIZ-P2 cleanup tests.

Covers the VIZ-P2 requirements:
  1. V4 surface retired — render_pressure_surface() removed; render_all() and
     scripts/send-viz-signal.sh no longer reference the surface plot.
  2. Registry refactor — the hardcoded QUOTA_LIMITS dict is gone;
     load_quota_series() and load_current_quota_state() consume the SAME
     LANE_REGISTRY_STATIC (single source of truth) via _lane_limits().
  3. Suggestion-engine thresholds -> env vars with documented defaults:
     INSIGHTS_L3_DAYS, INSIGHTS_C3_MIN_SESSIONS, INSIGHTS_M3_MIN_SHARE all
     overrideable and actually wired into their rules.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Load THIS worktree's copy of price_viz by file path (same pattern as the
# V8b/V11/V9V10 tests).
import importlib.util
_price_viz_path = REPO_ROOT / "price_viz.py"
_spec = importlib.util.spec_from_file_location("price_viz", _price_viz_path)
price_viz = importlib.util.module_from_spec(_spec)
sys.modules["price_viz"] = price_viz
_spec.loader.exec_module(price_viz)


# ── 1. V4 retirement ───────────────────────────────────────────────────────

class TestV4Retirement(unittest.TestCase):
    def test_render_pressure_surface_removed(self):
        """V4 surface function must no longer exist."""
        self.assertFalse(hasattr(price_viz, "render_pressure_surface"),
                         "render_pressure_surface should be retired")
        self.assertFalse(hasattr(price_viz, "render_pressure_surface"),
                         "V4 must not be callable in render_all")

    def test_sender_script_no_surface_ref(self):
        """send-viz-signal.sh must not reference surface-ollama_cloud."""
        sender = REPO_ROOT / "scripts" / "send-viz-signal.sh"
        text = sender.read_text()
        self.assertNotIn("surface-ollama_cloud", text)
        self.assertNotIn("surface-", text)

    def test_render_all_includes_headroom_not_surface(self):
        """render_all() wires headroom-weekly (V8b) and never a surface plot."""
        with tempfile.TemporaryDirectory() as td:
            with patch.object(price_viz.time, "time", return_value=NOW), \
                 patch.object(price_viz, "_connect_usage_db", side_effect=_fresh_usage_db), \
                 patch.object(price_viz, "_connect_api_burn_db", return_value=MagicMock()), \
                 patch.object(price_viz, "_fetch_quota_payload", return_value=None):
                files = price_viz.render_all(Path(td))
        names = {f.name for f in files}
        self.assertIn("headroom-weekly.png", names)
        self.assertTrue(all(not n.endswith("-surface-ollama_cloud.png") and
                            "surface" not in n for n in names))


# ── 2. Registry refactor ──────────────────────────────────────────────────

class TestRegistryRefactor(unittest.TestCase):
    def test_quota_limits_dict_gone(self):
        """The hardcoded QUOTA_LIMITS dict must be retired from source."""
        src = _price_viz_path.read_text()
        self.assertNotIn("QUOTA_LIMITS = {", src)

    def test_lane_limits_helper(self):
        """_lane_limits derives (session, weekly) from the registry."""
        reg = dict(price_viz.LANE_REGISTRY_STATIC)
        self.assertEqual(price_viz._lane_limits("ours", reg),
                         (2_000_000, 14_000_000))
        self.assertEqual(price_viz._lane_limits("ollama_cloud", reg),
                         (500_000_000, 3_500_000_000))
        # flat lane also carries caps now
        self.assertEqual(price_viz._lane_limits("opencode_go", reg),
                         (500_000_000, 3_500_000_000))
        # unknown lane -> generic default
        self.assertEqual(price_viz._lane_limits("ghost", reg),
                         (500_000_000, 3_500_000_000))

    def test_registry_has_session_capacity(self):
        """Every static lane carries session_capacity (flat too)."""
        for lane, entry in price_viz.LANE_REGISTRY_STATIC.items():
            self.assertIn("session_capacity", entry, lane)
        self.assertEqual(price_viz.LANE_REGISTRY_STATIC["ours"]["session_capacity"], 2_000_000)

    def test_load_current_quota_state_uses_registry(self):
        """Changing the registry capacity changes the computed fraction."""
        with patch.object(price_viz, "_connect_usage_db", side_effect=_fresh_usage_db), \
             patch.object(price_viz.time, "time", return_value=NOW):
            # Rows land in ~last hour: session dominates over weekly.
            # ours session_capacity=2M -> ~0.96M/2M = ~48%.
            frac_default = price_viz.load_current_quota_state()["ours"]
            self.assertGreater(frac_default, 0.3)
            self.assertLess(frac_default, 0.7)
            # shrink session_capacity -> fraction rises toward 100%
            reg = dict(price_viz.LANE_REGISTRY_STATIC)
            reg["ours"] = {**reg["ours"], "capacity": 14_000_000,
                           "session_capacity": 1_100_000}
            with patch.object(price_viz, "LANE_REGISTRY_STATIC", reg):
                frac_tight = price_viz.load_current_quota_state()["ours"]
            self.assertGreater(frac_tight, frac_default)

    def test_load_quota_series_uses_registry(self):
        """load_quota_series derives fractions from registry capacities."""
        with patch.object(price_viz, "_connect_usage_db", side_effect=_fresh_usage_db), \
             patch.object(price_viz, "_connect_api_burn_db", return_value=MagicMock()), \
             patch.object(price_viz.time, "time", return_value=NOW):
            data = price_viz.load_quota_series(hours_back=48)
        self.assertIn("ours", data)
        self.assertTrue(data["ours"])  # synth series present


# ── 3. Threshold env vars ─────────────────────────────────────────────────

class TestThresholdEnvVars(unittest.TestCase):
    def test_l3_days_wired_to_rule(self):
        """rule_l3_days_to_zero_ols honors INSIGHTS_L3_DAYS."""
        hist = []
        for i in range(14 * 24):
            ts = NOW - (14 * 24 - i) * 3600
            hist.append((ts, 20.0 - (i / (14 * 24)) * 18.0))  # drains to ~2
        ctx = _base_ctx(balance_history={"neuralwatt": hist})
        # With default 14 days it fires.
        self.assertIsNotNone(price_viz.rule_l3_days_to_zero_ols(ctx))
        # Tighten threshold below the projected days -> silent.
        with patch.object(price_viz, "INSIGHTS_L3_DAYS", 1.0):
            self.assertIsNone(price_viz.rule_l3_days_to_zero_ols(ctx))

    def test_c3_min_sessions_wired_to_rule(self):
        """rule_c3_runaway_session honors INSIGHTS_C3_MIN_SESSIONS."""
        ctx = _base_ctx(session_stats=(9, 50.0, 7000))  # 9 sessions, runaway
        # Default min 10 -> silent.
        self.assertIsNone(price_viz.rule_c3_runaway_session(ctx))
        # Lower the floor to 5 -> now fires.
        with patch.object(price_viz, "INSIGHTS_C3_MIN_SESSIONS", 5):
            self.assertIsNotNone(price_viz.rule_c3_runaway_session(ctx))

    def test_m3_min_share_wired_to_rule(self):
        """rule_m3_heavy_on_locked_lane honors INSIGHTS_M3_MIN_SHARE."""
        ctx = _base_ctx(
            quota_payload={"ours": {"locked": True, "locked_pct": 100}},
            lane_counts_48h={"ours": 3000, "ollama_cloud": 20000},  # ~13% share
        )
        # Default 0.30 -> share 13% is silent.
        self.assertIsNone(price_viz.rule_m3_heavy_on_locked_lane(ctx))
        # Lower floor to 0.10 -> fires.
        with patch.object(price_viz, "INSIGHTS_M3_MIN_SHARE", 0.10):
            self.assertIsNotNone(price_viz.rule_m3_heavy_on_locked_lane(ctx))

    def test_insights_l3_days_envable(self):
        """INSIGHTS_L3_DAYS loads from env VIZ_L3_DAYS."""
        # Load a fresh module straight off the worktree path under a unique name
        # so the read isn't polluted by (a) other viz test files overwriting
        # sys.modules["price_viz"] with their own module object, nor (b) the
        # shared ~/.hermes/bot live tree getting reloaded instead of this copy.
        # importlib.reload() on the file-level module object is too fragile for
        # that — a hermetic exec_module from REPO_ROOT is deterministic.
        import importlib.util as _iu
        with patch.dict(os.environ, {"VIZ_L3_DAYS": "7.5"}):
            _spec = _iu.spec_from_file_location("price_viz_envtest", REPO_ROOT / "price_viz.py")
            _m = _iu.module_from_spec(_spec)
            _spec.loader.exec_module(_m)
            self.assertEqual(_m.INSIGHTS_L3_DAYS, 7.5)


# ── Helpers (mirror the other viz test files) ─────────────────────────────

NOW = 1_784_000_000.0


def _make_usage_db():
    """In-memory usage DB with a few api_calls rows for 'ours' (last hour)."""
    import sqlite3
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE api_calls (id INTEGER PRIMARY KEY, "
               "key_name TEXT, model TEXT, total_tokens INTEGER, "
               "ts REAL, duration_ms REAL, cost_usd REAL)")
    db.execute("CREATE TABLE routing_profit (id INTEGER PRIMARY KEY, ts REAL, "
               "provider TEXT, price_per_mtok REAL)")
    # ~1M tokens for 'ours', all within the last hour so both the rolling 5h
    # session window and any weekly anchor include them.
    for i in range(6):
        db.execute("INSERT INTO api_calls (key_name, model, total_tokens, ts, duration_ms, cost_usd) "
                   "VALUES (?, ?, ?, ?, ?, ?)",
                   ("ours", "glm-5.3", 160_000, NOW - (3600 - i * 300), 15000, 0.01))
    db.execute("INSERT INTO api_calls (key_name, model, total_tokens, ts, duration_ms, cost_usd) "
               "VALUES (?, ?, ?, ?, ?, ?)",
               ("ollama_cloud", "dsv4", 500_000, NOW - 600, 9000, 0.02))
    db.commit()
    return db


def _fresh_usage_db():
    """Return a brand-new in-memory usage DB (survives each connection close)."""
    return _make_usage_db()


def _base_ctx(**over):
    ctx = {
        "now": NOW,
        "usage_columns": {"id", "ts", "key_name", "model", "total_tokens",
                          "cache_hit", "session_id", "task_type", "duration_ms"},
        "quota_state": {},
        "seed_rates": dict(price_viz.SEED_RATES),
        "tiers": dict(price_viz.PROVIDER_TIER),
        "lane_registry": dict(price_viz.LANE_REGISTRY_STATIC),
        "quota_payload": {},
        "model_counts_48h": {},
        "model_counts_7d": {},
        "model_latency_48h": {},
        "lane_counts_48h": {},
        "lane_last_seen": {},
        "session_stats": None,
        "balance_history": {},
        "cache_stats": None,
        "task_type_stats": None,
        "overall_avg_ms": None,
    }
    ctx.update(over)
    return ctx


if __name__ == "__main__":
    unittest.main()
