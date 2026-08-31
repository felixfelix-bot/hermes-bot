#!/usr/bin/env python3
"""V9 model-mix + V10 model x lane tests.

Covers the VIZ-P1 requirements (operator-approved consultant spec):
  1. V9 render_model_mix(outdir): 7-day stacked area of TOKENS by model
     (share evolution — is glm-5.3 share growing). Filename model-mix-7d.png.
  2. V10 render_model_by_lane(outdir): HORIZONTAL stacked bars per model,
     segments = lanes, right column = realized $/M per model. Filename
     model-by-lane.png. Answers who-uses-which-model-on-which-key-at-what-cost.
  3. Both are wired into render_all() so the --digest sender picks them up.
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
# V8b / V11 tests) so the V9/V10 symbols are present regardless of what other
# tests imported into sys.modules.
import importlib.util
_price_viz_path = REPO_ROOT / "price_viz.py"
_spec = importlib.util.spec_from_file_location("price_viz", _price_viz_path)
price_viz = importlib.util.module_from_spec(_spec)
sys.modules["price_viz"] = price_viz
_spec.loader.exec_module(price_viz)


# ── Fake DB helpers (mirror the V8b test) ───────────────────────────────────

class FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self._i = 0

    def fetchall(self):
        return self._rows

    def fetchone(self):
        if self._i < len(self._rows):
            r = self._rows[self._i]
            self._i += 1
            return r
        return None


class FakeDB:
    """Fake sqlite connection that records every execute() call.

    Returns canned rows ONLY for queries whose SQL contains a registered
    fragment; everything else returns empty. This keeps unrelated renders
    (envelope, heatmap, headroom) from choking on rows that lack their
    expected columns.
    """

    def __init__(self):
        self.calls = []
        self.row_factory = None
        self._rows_by_query = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        for sql_frag, rows in self._rows_by_query:
            if sql_frag in sql:
                return FakeCursor(rows)
        # Quota-state query (COALESCE(SUM(total_tokens),0) AS t) — return 0.
        if "AS t FROM api_calls" in sql or "as t FROM api_calls" in sql:
            return FakeCursor([_row(t=0)])
        return FakeCursor([])

    def close(self):
        pass


# Distinctive SQL fragments for the V9/V10 data loaders.
FRAG_TOKEN_SERIES = "FROM api_calls WHERE ts > ? AND model IS NOT NULL AND model != '' ORDER BY ts"
FRAG_MODEL_LANE = "GROUP BY model, key_name"
FRAG_REALIZED_PM = "AND cost_usd > 0 GROUP BY model"


def _row(**kw):
    class _R:
        def __init__(self, data):
            self.__dict__.update(data)
        def __getitem__(self, k):
            return self.__dict__[k]
    return _R(kw)


# ── Fixtures ────────────────────────────────────────────────────────────────

NOW = 1_788_000_000.0
DAY = 86400.0

# 7 days of token rows: glm-5.3 share grows over the week; glm-5.2 shrinks.
def _make_usage_db():
    db = FakeDB()
    # V9 token-series rows (model, ts, total_tokens).
    series_rows = []
    for d in range(7):
        series_rows.append(_row(model="glm-5.3", ts=NOW - (6 - d) * DAY,
                                total_tokens=100_000 + d * 50_000))
        series_rows.append(_row(model="glm-5.2", ts=NOW - (6 - d) * DAY,
                                total_tokens=300_000 - d * 30_000))
        series_rows.append(_row(model="deepseek-v4-flash:0731", ts=NOW - (6 - d) * DAY,
                                total_tokens=200_000))
    # V10 model x lane rows (model, key_name, tok) — tok matches SQL alias.
    lane_rows = [
        _row(model="glm-5.3", key_name="ours", tok=1_750_000),
        _row(model="glm-5.2", key_name="ollama_cloud", tok=1_470_000),
        _row(model="deepseek-v4-flash:0731", key_name="ollama_cloud", tok=1_400_000),
    ]
    # V10 realized $/M rows (model, tok, cost) — aliases match SQL.
    cost_rows = [
        _row(model="glm-5.3", tok=1_000_000, cost=0.324),
        _row(model="glm-5.2", tok=1_000_000, cost=0.134),
    ]
    db._rows_by_query = [
        (FRAG_TOKEN_SERIES, series_rows),
        (FRAG_MODEL_LANE, lane_rows),
        (FRAG_REALIZED_PM, cost_rows),
    ]
    return db


def _mock_plt():
    mock_plt = MagicMock()
    fig = MagicMock()
    ax = MagicMock()
    mock_plt.subplots.return_value = (fig, ax)
    # gca() must return a STABLE mock so method_calls accumulate on one object.
    mock_plt.gca.return_value = ax
    return mock_plt


# ── V9 data helper: model token series ──────────────────────────────────────

class TestLoadModelTokenSeries(unittest.TestCase):
    def test_buckets_tokens_by_model_over_days(self):
        db = _make_usage_db()
        with patch.object(price_viz.time, "time", return_value=NOW), \
             patch.object(price_viz, "_connect_usage_db", return_value=db):
            series, boundaries = price_viz._load_model_token_series(
                hours_back=168, bucket_s=DAY)
        # 3 models present.
        self.assertEqual(set(series.keys()), {"glm-5.3", "glm-5.2", "deepseek-v4-flash:0731"})
        # 7 daily buckets.
        self.assertEqual(len(boundaries), 7)
        for model, vals in series.items():
            self.assertEqual(len(vals), 7, model)
        # glm-5.3 grows: last bucket > first non-empty bucket.
        self.assertGreater(series["glm-5.3"][-1], series["glm-5.3"][1])
        # glm-5.2 shrinks: last bucket < first non-empty bucket.
        self.assertLess(series["glm-5.2"][-1], series["glm-5.2"][1])

    def test_returns_empty_on_no_rows(self):
        db = FakeDB()
        with patch.object(price_viz.time, "time", return_value=NOW), \
             patch.object(price_viz, "_connect_usage_db", return_value=db):
            series, boundaries = price_viz._load_model_token_series(
                hours_back=168, bucket_s=DAY)
        self.assertEqual(series, {})
        self.assertEqual(len(boundaries), 7)


# ── V9 render_model_mix ─────────────────────────────────────────────────────

class TestRenderModelMix(unittest.TestCase):
    def test_filename_and_signature(self):
        self.assertTrue(hasattr(price_viz, "render_model_mix"))
        import inspect
        sig = inspect.signature(price_viz.render_model_mix)
        self.assertEqual(list(sig.parameters)[0], "outdir")

    def test_renders_model_mix_7d_png(self):
        db = _make_usage_db()
        with patch.object(price_viz.time, "time", return_value=NOW), \
             patch.object(price_viz, "_connect_usage_db", return_value=db):
            with tempfile.TemporaryDirectory() as td:
                out = price_viz.render_model_mix(Path(td))
                self.assertEqual(out.name, "model-mix-7d.png")
                self.assertTrue(out.exists())

    def test_uses_stacked_area(self):
        db = _make_usage_db()
        mock_plt = _mock_plt()
        with patch.object(price_viz.time, "time", return_value=NOW), \
             patch.object(price_viz, "_connect_usage_db", return_value=db), \
             patch.object(price_viz, "plt", mock_plt):
            with tempfile.TemporaryDirectory() as td:
                price_viz.render_model_mix(Path(td))
        # fill_between must be called (stacked area), not just bar.
        self.assertTrue(mock_plt.gca().fill_between.called or
                        any(c[0] == "fill_between" for c in mock_plt.gca().method_calls),
                        "expected stacked area (fill_between)")

    def test_handles_empty_db_gracefully(self):
        db = FakeDB()
        with patch.object(price_viz.time, "time", return_value=NOW), \
             patch.object(price_viz, "_connect_usage_db", return_value=db):
            with tempfile.TemporaryDirectory() as td:
                out = price_viz.render_model_mix(Path(td))
                self.assertEqual(out.name, "model-mix-7d.png")
                self.assertTrue(out.exists())


# ── V10 data helpers ─────────────────────────────────────────────────────────

class TestLoadModelLaneTokens(unittest.TestCase):
    def test_groups_tokens_by_model_and_lane(self):
        db = _make_usage_db()
        with patch.object(price_viz, "_connect_usage_db", return_value=db):
            m2l = price_viz._load_model_lane_tokens(hours_back=168)
        # glm-5.3 on ours, glm-5.2 + dsv4 on ollama_cloud.
        self.assertIn("glm-5.3", m2l)
        self.assertIn("ours", m2l["glm-5.3"])
        self.assertIn("glm-5.2", m2l)
        self.assertIn("ollama_cloud", m2l["glm-5.2"])
        # glm-5.3 total = 100k+150k+200k+250k+300k+350k+400k = 1.75M
        self.assertAlmostEqual(m2l["glm-5.3"]["ours"], 1_750_000, delta=1)


class TestLoadModelRealizedPm(unittest.TestCase):
    def test_computes_realized_dollars_per_million(self):
        db = _make_usage_db()
        with patch.object(price_viz, "_connect_usage_db", return_value=db):
            pm = price_viz._load_model_realized_pm(hours_back=168)
        # glm-5.3: 0.324 / 1M * 1e6 = 0.324
        self.assertAlmostEqual(pm["glm-5.3"], 0.324, places=3)
        # glm-5.2: 0.134
        self.assertAlmostEqual(pm["glm-5.2"], 0.134, places=3)

    def test_skips_models_without_cost(self):
        db = _make_usage_db()
        with patch.object(price_viz, "_connect_usage_db", return_value=db):
            pm = price_viz._load_model_realized_pm(hours_back=168)
        # deepseek-v4-flash has tokens but no cost rows -> not in realized map.
        self.assertNotIn("deepseek-v4-flash:0731", pm)


# ── V10 render_model_by_lane ────────────────────────────────────────────────

class TestRenderModelByLane(unittest.TestCase):
    def test_filename_and_signature(self):
        self.assertTrue(hasattr(price_viz, "render_model_by_lane"))
        import inspect
        sig = inspect.signature(price_viz.render_model_by_lane)
        self.assertEqual(list(sig.parameters)[0], "outdir")

    def test_renders_model_by_lane_png(self):
        db = _make_usage_db()
        with patch.object(price_viz.time, "time", return_value=NOW), \
             patch.object(price_viz, "_connect_usage_db", return_value=db):
            with tempfile.TemporaryDirectory() as td:
                out = price_viz.render_model_by_lane(Path(td))
                self.assertEqual(out.name, "model-by-lane.png")
                self.assertTrue(out.exists())

    def test_uses_horizontal_bars(self):
        db = _make_usage_db()
        mock_plt = _mock_plt()
        with patch.object(price_viz.time, "time", return_value=NOW), \
             patch.object(price_viz, "_connect_usage_db", return_value=db), \
             patch.object(price_viz, "plt", mock_plt):
            with tempfile.TemporaryDirectory() as td:
                price_viz.render_model_by_lane(Path(td))
        # barh (horizontal bars) must be called.
        gca = mock_plt.gca()
        self.assertTrue(gca.barh.called,
                        "expected horizontal bars (barh)")

    def test_handles_empty_db_gracefully(self):
        db = FakeDB()
        with patch.object(price_viz.time, "time", return_value=NOW), \
             patch.object(price_viz, "_connect_usage_db", return_value=db):
            with tempfile.TemporaryDirectory() as td:
                out = price_viz.render_model_by_lane(Path(td))
                self.assertEqual(out.name, "model-by-lane.png")
                self.assertTrue(out.exists())


# ── render_all wiring ───────────────────────────────────────────────────────

class TestRenderAllWiring(unittest.TestCase):
    def test_render_all_includes_v9_and_v10(self):
        db = _make_usage_db()
        with patch.object(price_viz.time, "time", return_value=NOW), \
             patch.object(price_viz, "_connect_usage_db", return_value=db), \
             patch.object(price_viz, "_connect_api_burn_db", return_value=FakeDB()), \
             patch.object(price_viz, "_fetch_quota_payload", return_value=None):
            with tempfile.TemporaryDirectory() as td:
                files = price_viz.render_all(Path(td))
        names = {f.name for f in files}
        self.assertIn("model-mix-7d.png", names)
        self.assertIn("model-by-lane.png", names)


if __name__ == "__main__":
    unittest.main()
