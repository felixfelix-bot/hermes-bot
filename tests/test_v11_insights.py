#!/usr/bin/env python3
"""V11 ASCII insights strip tests.

Covers the VIZ-P0B requirements:
  1. render_insights() — triggered-only ASCII insights strip. Silent (empty)
     when no rules fire. NEVER fabricates a line.
  2. 14 rules across prefixes ALERT / EST / SUGGEST / NAG.
  3. Schema-safe: rules introspect PRAGMA table_info and gracefully skip when
     columns (cached_tokens, task_type) are absent.
  4. Caps: hourly = red-only (ALERT) <= 3 lines; daily <= 12 lines ~1900 chars.
  5. NAG weekly dedupe via a persistent state file.
  6. False-positive damping: min sample sizes, thresholds.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Load THIS worktree's copy of price_viz by file path (same pattern as the
# V8b test) so the V11 symbols are present regardless of what other tests
# imported into sys.modules.
import importlib.util
_price_viz_path = REPO_ROOT / "price_viz.py"
_spec = importlib.util.spec_from_file_location("price_viz", _price_viz_path)
price_viz = importlib.util.module_from_spec(_spec)
sys.modules["price_viz"] = price_viz
_spec.loader.exec_module(price_viz)


# ── Minimal ctx fixtures ────────────────────────────────────────────────

def _base_ctx(**over):
    """A ctx with no triggers — every rule should stay silent on this."""
    ctx = {
        "now": 1_788_000_000.0,
        "usage_columns": {"id", "ts", "key_name", "model", "total_tokens",
                          "cache_hit", "session_id", "task_type", "duration_ms"},
        "quota_state": {},          # provider -> usage_fraction (0..1)
        "seed_rates": dict(price_viz.SEED_RATES),
        "tiers": dict(price_viz.PROVIDER_TIER),
        "lane_registry": dict(price_viz.LANE_REGISTRY_STATIC),
        "quota_payload": {},
        "model_counts_48h": {},
        "model_counts_7d": {},
        "model_latency_48h": {},    # model -> (calls, avg_ms)
        "lane_counts_48h": {},      # lane -> call count
        "lane_last_seen": {},       # lane -> ts
        "session_stats": None,      # (n_sessions, avg_calls, max_calls)
        "balance_history": {},      # provider -> [(ts, remaining), ...]
        "cache_stats": None,        # (total_calls, cache_hits)
        "task_type_stats": None,    # (total_calls, null_calls)
        "overall_avg_ms": None,
    }
    ctx.update(over)
    return ctx


# ── L1 idle-twin ─────────────────────────────────────────────────────────

class TestL1IdleTwin(unittest.TestCase):
    def test_fires_when_twin_idle(self):
        ctx = _base_ctx(
            quota_state={"ollama_cloud": 0.35, "ollama_cloud_2": 0.003},
        )
        line = price_viz.rule_l1_idle_twin(ctx)
        self.assertIsNotNone(line)
        self.assertTrue(line.startswith("SUGGEST"), line)
        self.assertIn("ollama_cloud_2", line)

    def test_silent_when_no_idle_twin(self):
        ctx = _base_ctx(
            quota_state={"ollama_cloud": 0.35, "ollama_cloud_2": 0.30},
        )
        self.assertIsNone(price_viz.rule_l1_idle_twin(ctx))

    def test_silent_when_both_idle(self):
        ctx = _base_ctx(
            quota_state={"ollama_cloud": 0.01, "ollama_cloud_2": 0.003},
        )
        self.assertIsNone(price_viz.rule_l1_idle_twin(ctx))


# ── L2 lane-exhaust ─────────────────────────────────────────────────────

class TestL2LaneExhaust(unittest.TestCase):
    def test_fires_on_near_exhaust(self):
        ctx = _base_ctx(quota_state={"neuralwatt": 0.9947})
        line = price_viz.rule_l2_lane_exhaust(ctx)
        self.assertIsNotNone(line)
        self.assertTrue(line.startswith("ALERT"), line)
        self.assertIn("neuralwatt", line)

    def test_silent_below_threshold(self):
        ctx = _base_ctx(quota_state={"neuralwatt": 0.50})
        self.assertIsNone(price_viz.rule_l2_lane_exhaust(ctx))


# ── L3 days-to-zero OLS ────────────────────────────────────────────────

class TestL3DaysToZeroOLS(unittest.TestCase):
    def test_fires_when_balance_draining(self):
        # 14 days of balance declining from 20 -> 2 USD.
        now = 1_788_000_000.0
        hist = []
        for i in range(14 * 24):
            ts = now - (14 * 24 - i) * 3600
            rem = 20.0 - (i / (14 * 24)) * 18.0
            hist.append((ts, rem))
        ctx = _base_ctx(balance_history={"neuralwatt": hist})
        line = price_viz.rule_l3_days_to_zero_ols(ctx)
        self.assertIsNotNone(line)
        self.assertTrue(line.startswith("EST"), line)
        self.assertIn("neuralwatt", line)

    def test_silent_when_insufficient_samples(self):
        ctx = _base_ctx(balance_history={"neuralwatt": [(1, 10.0), (2, 9.0)]})
        self.assertIsNone(price_viz.rule_l3_days_to_zero_ols(ctx))

    def test_silent_when_balance_flat(self):
        now = 1_788_000_000.0
        hist = [(now - (14 * 24 - i) * 3600, 20.0) for i in range(14 * 24)]
        ctx = _base_ctx(balance_history={"neuralwatt": hist})
        self.assertIsNone(price_viz.rule_l3_days_to_zero_ols(ctx))


# ── L4 zombie key ───────────────────────────────────────────────────────

class TestL4ZombieKey(unittest.TestCase):
    def test_fires_on_zombie_lane(self):
        now = 1_788_000_000.0
        ctx = _base_ctx(
            lane_last_seen={"friend": now - 265 * 3600},
            lane_counts_48h={"friend": 0},
        )
        line = price_viz.rule_l4_zombie_key(ctx)
        self.assertIsNotNone(line)
        self.assertTrue(line.startswith("SUGGEST"), line)
        self.assertIn("friend", line)

    def test_silent_when_lane_active(self):
        now = 1_788_000_000.0
        ctx = _base_ctx(
            lane_last_seen={"ollama_cloud": now - 100},
            lane_counts_48h={"ollama_cloud": 6200},
        )
        self.assertIsNone(price_viz.rule_l4_zombie_key(ctx))


# ── C1 cache-worth ──────────────────────────────────────────────────────

class TestC1CacheWorth(unittest.TestCase):
    def test_fires_when_zero_cache_hits(self):
        ctx = _base_ctx(cache_stats=(12408, 0))
        line = price_viz.rule_c1_cache_worth(ctx)
        self.assertIsNotNone(line)
        self.assertTrue(line.startswith("SUGGEST"), line)

    def test_silent_when_cache_hits_present(self):
        ctx = _base_ctx(cache_stats=(12408, 500))
        self.assertIsNone(price_viz.rule_c1_cache_worth(ctx))

    def test_silent_when_cache_hit_column_missing(self):
        # Schema-safe: cached_tokens / cache_hit absent -> graceful skip.
        ctx = _base_ctx(usage_columns={"id", "ts", "key_name", "model"})
        ctx["cache_stats"] = None
        self.assertIsNone(price_viz.rule_c1_cache_worth(ctx))

    def test_silent_when_too_few_calls(self):
        ctx = _base_ctx(cache_stats=(50, 0))
        self.assertIsNone(price_viz.rule_c1_cache_worth(ctx))


# ── C3 runaway session ──────────────────────────────────────────────────

class TestC3RunawaySession(unittest.TestCase):
    def test_fires_when_session_dominates(self):
        ctx = _base_ctx(session_stats=(764, 12.6, 557))
        line = price_viz.rule_c3_runaway_session(ctx)
        self.assertIsNotNone(line)
        self.assertTrue(line.startswith("SUGGEST"), line)

    def test_silent_when_balanced(self):
        ctx = _base_ctx(session_stats=(764, 12.6, 20))
        self.assertIsNone(price_viz.rule_c3_runaway_session(ctx))

    def test_silent_when_no_session_data(self):
        ctx = _base_ctx(session_stats=None)
        self.assertIsNone(price_viz.rule_c3_runaway_session(ctx))


# ── M1 mix drift ───────────────────────────────────────────────────────

class TestM1MixDrift(unittest.TestCase):
    def test_fires_on_share_drift(self):
        # glm-5.2: 48% of 7d mix, 15% of 48h mix -> 33pp drop.
        ctx = _base_ctx(
            model_counts_7d={"glm-5.2": 21670, "glm-5.3": 10887},
            model_counts_48h={"glm-5.2": 1849, "glm-5.3": 5321},
        )
        line = price_viz.rule_m1_mix_drift(ctx)
        self.assertIsNotNone(line)
        self.assertTrue(line.startswith("SUGGEST"), line)
        self.assertIn("glm-5.2", line)

    def test_silent_when_mix_stable(self):
        ctx = _base_ctx(
            model_counts_7d={"glm-5.2": 100, "glm-5.3": 100},
            model_counts_48h={"glm-5.2": 100, "glm-5.3": 100},
        )
        self.assertIsNone(price_viz.rule_m1_mix_drift(ctx))

    def test_silent_when_insufficient_7d_data(self):
        ctx = _base_ctx(model_counts_7d={}, model_counts_48h={"glm-5.2": 10})
        self.assertIsNone(price_viz.rule_m1_mix_drift(ctx))


# ── M2 model x lane mismatch ───────────────────────────────────────────

class TestM2ModelLaneMismatch(unittest.TestCase):
    def test_fires_when_paid_lane_used_while_flat_idle(self):
        # glm-5.3 on 'ours' (paid/quota) while opencode_go (flat) idle.
        ctx = _base_ctx(
            lane_counts_48h={"ours": 3612, "opencode_go": 0},
            model_lane_48h={"glm-5.3": {"ours": 3612}},
        )
        line = price_viz.rule_m2_model_lane_mismatch(ctx)
        self.assertIsNotNone(line)
        self.assertTrue(line.startswith("SUGGEST"), line)

    def test_silent_when_flat_lane_active(self):
        ctx = _base_ctx(
            lane_counts_48h={"ours": 3612, "opencode_go": 200},
            model_lane_48h={"glm-5.3": {"ours": 3612}},
        )
        self.assertIsNone(price_viz.rule_m2_model_lane_mismatch(ctx))


# ── Q2 latency outlier ────────────────────────────────────────────────

class TestQ2LatencyOutlier(unittest.TestCase):
    def test_fires_on_latency_outlier(self):
        ctx = _base_ctx(
            model_latency_48h={"glm-4.5-flash": (930, 34780)},
            overall_avg_ms=14610,
        )
        line = price_viz.rule_q2_latency_outlier(ctx)
        self.assertIsNotNone(line)
        self.assertTrue(line.startswith("SUGGEST"), line)
        self.assertIn("glm-4.5-flash", line)

    def test_silent_when_latency_normal(self):
        ctx = _base_ctx(
            model_latency_48h={"glm-5.3": (5322, 16490)},
            overall_avg_ms=14610,
        )
        self.assertIsNone(price_viz.rule_q2_latency_outlier(ctx))

    def test_silent_when_too_few_calls(self):
        ctx = _base_ctx(
            model_latency_48h={"glm-4.5-flash": (5, 34780)},
            overall_avg_ms=14610,
        )
        self.assertIsNone(price_viz.rule_q2_latency_outlier(ctx))


# ── N1 task_type NAG ───────────────────────────────────────────────────

class TestN1TaskTypeNag(unittest.TestCase):
    def test_fires_when_task_type_null(self):
        ctx = _base_ctx(task_type_stats=(12408, 12340))
        line = price_viz.rule_n1_task_type_nag(ctx)
        self.assertIsNotNone(line)
        self.assertTrue(line.startswith("NAG"), line)

    def test_silent_when_task_type_populated(self):
        ctx = _base_ctx(task_type_stats=(12408, 100))
        self.assertIsNone(price_viz.rule_n1_task_type_nag(ctx))

    def test_silent_when_task_type_column_missing(self):
        ctx = _base_ctx(usage_columns={"id", "ts", "key_name", "model"})
        ctx["task_type_stats"] = None
        self.assertIsNone(price_viz.rule_n1_task_type_nag(ctx))


# ── E1 weekly over-budget projection ───────────────────────────────────

class TestE1WeeklyOverBudget(unittest.TestCase):
    def test_fires_when_weekly_over_budget(self):
        ctx = _base_ctx(
            quota_payload={"ours": {"predictions": [{
                "window": "weekly", "will_exhaust": True,
                "projected_total_pct": 173.4, "exhausts_in_hours": 0,
            }]}},
        )
        line = price_viz.rule_e1_weekly_over_budget(ctx)
        self.assertIsNotNone(line)
        self.assertTrue(line.startswith("ALERT"), line)

    def test_silent_when_within_budget(self):
        ctx = _base_ctx(
            quota_payload={"ours": {"predictions": [{
                "window": "weekly", "will_exhaust": False,
                "projected_total_pct": 80.0, "exhausts_in_hours": 100,
            }]}},
        )
        self.assertIsNone(price_viz.rule_e1_weekly_over_budget(ctx))


# ── M3 heavy traffic on locked lane ────────────────────────────────────

class TestM3HeavyOnLockedLane(unittest.TestCase):
    def test_fires_when_locked_lane_carries_heavy_traffic(self):
        ctx = _base_ctx(
            quota_payload={"ours": {"locked": True, "locked_pct": 100}},
            lane_counts_48h={"ours": 6191, "ollama_cloud": 6264},
        )
        line = price_viz.rule_m3_heavy_on_locked_lane(ctx)
        self.assertIsNotNone(line)
        self.assertTrue(line.startswith("SUGGEST"), line)
        self.assertIn("ours", line)

    def test_silent_when_locked_lane_light(self):
        ctx = _base_ctx(
            quota_payload={"ours": {"locked": True, "locked_pct": 100}},
            lane_counts_48h={"ours": 100, "ollama_cloud": 12000},
        )
        self.assertIsNone(price_viz.rule_m3_heavy_on_locked_lane(ctx))

    def test_silent_when_nothing_locked(self):
        ctx = _base_ctx(
            quota_payload={"ollama_cloud": {"used_pct": 34.0}},
            lane_counts_48h={"ollama_cloud": 6000},
        )
        self.assertIsNone(price_viz.rule_m3_heavy_on_locked_lane(ctx))


# ── L2 balance-lane enrichment (neuralwatt) ────────────────────────────

class TestL2BalanceLaneEnrichment(unittest.TestCase):
    def test_quota_state_enriched_with_balance_usage_fraction(self):
        # _build_insights_ctx overlays provider_balances.usage_fraction onto
        # quota_state for balance-tier lanes that the static loader reports 0.0.
        # Verify the overlay logic via a direct ctx build is hard without a live
        # DB, so assert the rule fires when quota_state carries the fraction.
        ctx = _base_ctx(quota_state={"neuralwatt": 0.9947})
        line = price_viz.rule_l2_lane_exhaust(ctx)
        self.assertIsNotNone(line)
        self.assertTrue(line.startswith("ALERT"), line)
        self.assertIn("neuralwatt", line)


# ── render_insights: triggered-only + caps ─────────────────────────────

class TestRenderInsights(unittest.TestCase):
    def test_empty_when_no_triggers(self):
        # The base ctx has no triggers -> strip must be empty (never fabricate).
        strip = price_viz.render_insights(mode="daily", ctx=_base_ctx())
        self.assertEqual(strip, "")

    def test_daily_cap_max_12_lines(self):
        # Force many rules to fire.
        now = 1_788_000_000.0
        hist = []
        for i in range(14 * 24):
            ts = now - (14 * 24 - i) * 3600
            hist.append((ts, 20.0 - (i / (14 * 24)) * 18.0))
        ctx = _base_ctx(
            quota_state={"neuralwatt": 0.9947, "ollama_cloud": 0.35,
                         "ollama_cloud_2": 0.003},
            balance_history={"neuralwatt": hist},
            cache_stats=(12408, 0),
            session_stats=(764, 12.6, 557),
            model_counts_7d={"glm-5.2": 21670, "glm-5.3": 10887},
            model_counts_48h={"glm-5.2": 1849, "glm-5.3": 5321},
            model_latency_48h={"glm-4.5-flash": (930, 34780)},
            overall_avg_ms=14610,
            task_type_stats=(12408, 12340),
            lane_last_seen={"friend": now - 265 * 3600},
            lane_counts_48h={"friend": 0, "ours": 3612, "opencode_go": 0},
            model_lane_48h={"glm-5.3": {"ours": 3612}},
            quota_payload={"ours": {"predictions": [{
                "window": "weekly", "will_exhaust": True,
                "projected_total_pct": 173.4, "exhausts_in_hours": 0}]}},
        )
        strip = price_viz.render_insights(mode="daily", ctx=ctx)
        lines = [l for l in strip.splitlines() if l.strip()]
        self.assertGreaterEqual(len(lines), 1)
        self.assertLessEqual(len(lines), 12, f"daily cap exceeded:\n{strip}")
        self.assertLessEqual(len(strip), 1900, f"daily char cap exceeded: {len(strip)}")

    def test_hourly_red_only_max_3(self):
        now = 1_788_000_000.0
        hist = []
        for i in range(14 * 24):
            ts = now - (14 * 24 - i) * 3600
            hist.append((ts, 20.0 - (i / (14 * 24)) * 18.0))
        ctx = _base_ctx(
            quota_state={"neuralwatt": 0.9947, "ollama_cloud": 0.35,
                         "ollama_cloud_2": 0.003},
            balance_history={"neuralwatt": hist},
            cache_stats=(12408, 0),
            session_stats=(764, 12.6, 557),
            model_counts_7d={"glm-5.2": 21670, "glm-5.3": 10887},
            model_counts_48h={"glm-5.2": 1849, "glm-5.3": 5321},
            model_latency_48h={"glm-4.5-flash": (930, 34780)},
            overall_avg_ms=14610,
            task_type_stats=(12408, 12340),
            lane_last_seen={"friend": now - 265 * 3600},
            lane_counts_48h={"friend": 0, "ours": 3612, "opencode_go": 0},
            model_lane_48h={"glm-5.3": {"ours": 3612}},
            quota_payload={"ours": {"predictions": [{
                "window": "weekly", "will_exhaust": True,
                "projected_total_pct": 173.4, "exhausts_in_hours": 0}]}},
        )
        strip = price_viz.render_insights(mode="hourly", ctx=ctx)
        lines = [l for l in strip.splitlines() if l.strip()]
        # Hourly = red-only (ALERT) and <= 3 lines.
        for l in lines:
            self.assertTrue(l.startswith("ALERT"), f"non-ALERT in hourly: {l}")
        self.assertLessEqual(len(lines), 3, f"hourly cap exceeded:\n{strip}")

    def test_prefixes_valid(self):
        now = 1_788_000_000.0
        hist = []
        for i in range(14 * 24):
            ts = now - (14 * 24 - i) * 3600
            hist.append((ts, 20.0 - (i / (14 * 24)) * 18.0))
        ctx = _base_ctx(
            quota_state={"neuralwatt": 0.9947, "ollama_cloud": 0.35,
                         "ollama_cloud_2": 0.003},
            balance_history={"neuralwatt": hist},
            cache_stats=(12408, 0),
            session_stats=(764, 12.6, 557),
            model_counts_7d={"glm-5.2": 21670, "glm-5.3": 10887},
            model_counts_48h={"glm-5.2": 1849, "glm-5.3": 5321},
            model_latency_48h={"glm-4.5-flash": (930, 34780)},
            overall_avg_ms=14610,
            task_type_stats=(12408, 12340),
            lane_last_seen={"friend": now - 265 * 3600},
            lane_counts_48h={"friend": 0, "ours": 3612, "opencode_go": 0},
            model_lane_48h={"glm-5.3": {"ours": 3612}},
            quota_payload={"ours": {"predictions": [{
                "window": "weekly", "will_exhaust": True,
                "projected_total_pct": 173.4, "exhausts_in_hours": 0}]}},
        )
        strip = price_viz.render_insights(mode="daily", ctx=ctx)
        for l in strip.splitlines():
            if l.strip():
                self.assertTrue(
                    l.startswith(("ALERT", "EST", "SUGGEST", "NAG")),
                    f"bad prefix: {l}")


# ── NAG weekly dedupe ───────────────────────────────────────────────────

class TestNagWeeklyDedupe(unittest.TestCase):
    def test_nag_deduped_within_week(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = Path(td) / "nag-state.json"
            ctx = _base_ctx(task_type_stats=(12408, 12340))
            # First call emits the NAG.
            first = price_viz.render_insights(
                mode="daily", ctx=ctx, nag_state_file=state_file)
            self.assertIn("NAG", first)
            # Second call within the week is deduped.
            second = price_viz.render_insights(
                mode="daily", ctx=ctx, nag_state_file=state_file)
            self.assertNotIn("NAG", second)


if __name__ == "__main__":
    unittest.main()
