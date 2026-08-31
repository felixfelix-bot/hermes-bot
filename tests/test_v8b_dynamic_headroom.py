#!/usr/bin/env python3
"""V8b dynamic 2-panel headroom tests.

Covers the VIZ-P0A requirements:
  1. Lane registry — single source of truth mapping every provider lane to
     (kind, capacity). Kinds: token-lane, usd-lane, flat-lane.
  2. Two panels in one figure: Panel A token lanes stacked area; Panel B USD
     balance lanes as absolute USD, never stacked into tokens.
  3. 1850-q bug fix: 1 query per pool (not 168 buckets x pools).
  4. Filename headroom-weekly.png + function name render_headroom_weekly(outdir)
     preserved for compat.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure the repo root is importable so `import price_viz` resolves.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Other tests in the full suite may have imported price_viz from the main
# checkout (~/.hermes/bot) into sys.modules, and may have inserted that path at
# sys.path[0]. Load THIS worktree's copy directly by file path so the V8b
# symbols (build_lane_registry, _fetch_quota_payload) are present regardless.
import importlib.util
_price_viz_path = REPO_ROOT / "price_viz.py"
_spec = importlib.util.spec_from_file_location("price_viz", _price_viz_path)
price_viz = importlib.util.module_from_spec(_spec)
sys.modules["price_viz"] = price_viz
_spec.loader.exec_module(price_viz)


# ── Fake DB helpers ──────────────────────────────────────────────────────────

class FakeCursor:
    """Minimal cursor supporting .fetchall() / .fetchone() over canned rows."""

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

    Filters canned rows by the ``key_name``/``provider`` param so each lane
    query returns only its own rows.
    """

    def __init__(self):
        self.calls = []
        self.row_factory = None
        self._rows_by_query = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        # Return canned rows if provided for this exact SQL, else empty.
        for sql_frag, rows in self._rows_by_query:
            if sql_frag in sql:
                # Filter by the lane param (key_name or provider) if present.
                lane = None
                for p in params:
                    if isinstance(p, str):
                        lane = p
                        break
                if lane is not None:
                    filtered = [r for r in rows if getattr(r, "key_name", None) == lane
                                or getattr(r, "provider", None) == lane]
                    return FakeCursor(filtered)
                return FakeCursor(rows)
        return FakeCursor([])

    def close(self):
        pass


def _row(**kw):
    """Build a sqlite3.Row-like object (attribute + subscript access)."""
    class _R:
        def __init__(self, data):
            self.__dict__.update(data)
        def __getitem__(self, k):
            return self.__dict__[k]
    return _R(kw)


# ── Fixtures ─────────────────────────────────────────────────────────────────

QUOTA_PAYLOAD = {
    "ours": {
        "windows": [{"name": "weekly", "type": "CREDIT_LIMIT", "used_pct": 100}],
        "locked": True, "locked_window": "weekly", "locked_pct": 100,
        "max_pct": 100, "age_s": 42, "predictions": [],
    },
    "ollama_cloud": {"used_pct": 33.76, "remaining": 331200000.0,
                     "total": 500000000, "regime": "included"},
    "ollama_cloud_2": {"used_pct": 0.32, "remaining": 498400000.0,
                       "total": 500000000, "regime": "included"},
    "opencode_go": {"used_pct": 0.0, "remaining": float("inf"),
                    "total": float("inf"), "regime": "included"},
    "neuralwatt": {"used_pct": 99.47, "remaining": 0.0698, "total": 13.3333,
                   "remaining_usd": 8.9951, "total_credits_usd": 11.0},
    "extra_lane": {"used_pct": 5.0, "remaining": 100.0, "total": 200.0},
}


def _make_usage_db():
    """Fake usage DB with token rows for the token lanes + telnyx spend."""
    db = FakeDB()
    now = 1_788_000_000.0
    # One row per token lane, plus telnyx cost rows.
    token_lanes = ["ours", "friend", "ollama_cloud", "ollama_cloud_2"]
    rows = []
    for lane in token_lanes:
        rows.append(_row(key_name=lane, ts=now - 3600, total_tokens=1_000_000))
        rows.append(_row(key_name=lane, ts=now - 7200, total_tokens=2_000_000))
    # telnyx spend (USD lane read from api_calls cost_usd)
    rows.append(_row(key_name="telnyx", ts=now - 3600, total_tokens=0, cost_usd=2.0))
    rows.append(_row(key_name="telnyx", ts=now - 7200, total_tokens=0, cost_usd=1.0))
    db._rows_by_query = [("FROM api_calls", rows)]
    return db


def _make_burn_db():
    """Fake api_burn DB with provider_balances rows for USD lanes."""
    db = FakeDB()
    now = 1_788_000_000.0
    rows = [
        _row(collected_at=now - 3600, provider="routstrd", limit_remaining=15.5),
        _row(collected_at=now - 7200, provider="routstrd", limit_remaining=16.0),
        _row(collected_at=now - 3600, provider="ppq", limit_remaining=0.001),
        _row(collected_at=now - 7200, provider="ppq", limit_remaining=0.002),
    ]
    db._rows_by_query = [("FROM provider_balances", rows)]
    return db


# ── Tests ────────────────────────────────────────────────────────────────────

class TestLaneRegistry(unittest.TestCase):
    """Requirement 1: lane registry maps every lane -> (kind, capacity)."""

    def test_all_required_lanes_present(self):
        reg = price_viz.build_lane_registry(QUOTA_PAYLOAD)
        required = ["ours", "friend", "ollama_cloud", "ollama_cloud_2",
                    "opencode_go", "neuralwatt", "routstrd", "telnyx", "ppq"]
        for lane in required:
            self.assertIn(lane, reg, f"lane {lane} missing from registry")
            self.assertIn("kind", reg[lane])
            self.assertIn("capacity", reg[lane])

    def test_kind_classification(self):
        reg = price_viz.build_lane_registry(QUOTA_PAYLOAD)
        # token lanes
        for lane in ("ours", "friend", "ollama_cloud", "ollama_cloud_2"):
            self.assertEqual(reg[lane]["kind"], "token", lane)
        # usd lanes
        for lane in ("routstrd", "telnyx", "ppq", "neuralwatt"):
            self.assertEqual(reg[lane]["kind"], "usd", lane)
        # flat lane
        self.assertEqual(reg["opencode_go"]["kind"], "flat")

    def test_extra_payload_lanes_absorbed(self):
        reg = price_viz.build_lane_registry(QUOTA_PAYLOAD)
        self.assertIn("extra_lane", reg)

    def test_token_capacity_from_limits(self):
        reg = price_viz.build_lane_registry(QUOTA_PAYLOAD)
        # ours/friend weekly limit is 14M; ollama lanes 3.5B
        self.assertEqual(reg["ours"]["capacity"], 14_000_000)
        self.assertEqual(reg["ollama_cloud"]["capacity"], 3_500_000_000)

    def test_registry_without_payload_still_has_static_lanes(self):
        reg = price_viz.build_lane_registry(None)
        for lane in ("ours", "friend", "ollama_cloud", "ollama_cloud_2",
                     "opencode_go", "neuralwatt", "routstrd", "telnyx", "ppq"):
            self.assertIn(lane, reg)


def _mock_plt():
    """Return a mocked plt whose subplots() yields (fig, (ax_a, ax_b))."""
    mock_plt = MagicMock()
    fig = MagicMock()
    ax_a = MagicMock()
    ax_b = MagicMock()
    mock_plt.subplots.return_value = (fig, (ax_a, ax_b))
    return mock_plt


class TestQueryCountFix(unittest.TestCase):
    """Requirement 3: 1 query per pool, not 168 buckets x pools."""

    def test_one_query_per_token_pool(self):
        usage_db = _make_usage_db()
        burn_db = _make_burn_db()
        with patch.object(price_viz, "_connect_usage_db", return_value=usage_db), \
             patch.object(price_viz, "_connect_api_burn_db", return_value=burn_db), \
             patch.object(price_viz, "_fetch_quota_payload", return_value=QUOTA_PAYLOAD), \
             patch.object(price_viz, "plt", _mock_plt()):
            with tempfile.TemporaryDirectory() as td:
                price_viz.render_headroom_weekly(Path(td))

        # Count queries against api_calls (token lanes + telnyx spend).
        api_calls_queries = [c for c in usage_db.calls if "FROM api_calls" in c[0]]
        # 4 token lanes + 1 telnyx = 5 queries max. The old bug was 168*N.
        self.assertLessEqual(len(api_calls_queries), 6,
                             f"expected ~1 query/pool, got {len(api_calls_queries)}")
        # No per-hour loop: no query should be issued 168 times.
        self.assertLess(len(api_calls_queries), 20)


class TestTwoPanelFigure(unittest.TestCase):
    """Requirement 2: two panels — token stacked + USD absolute."""

    def test_two_subplots_created(self):
        usage_db = _make_usage_db()
        burn_db = _make_burn_db()
        mock_plt = _mock_plt()
        with patch.object(price_viz, "_connect_usage_db", return_value=usage_db), \
             patch.object(price_viz, "_connect_api_burn_db", return_value=burn_db), \
             patch.object(price_viz, "_fetch_quota_payload", return_value=QUOTA_PAYLOAD), \
             patch.object(price_viz, "plt", mock_plt):
            with tempfile.TemporaryDirectory() as td:
                price_viz.render_headroom_weekly(Path(td))
        # subplots must be called with nrows=2 (two panels).
        args, kwargs = mock_plt.subplots.call_args
        self.assertEqual(kwargs.get("nrows"), 2,
                         f"expected 2-panel figure, subplots called with {kwargs}")


class TestCompat(unittest.TestCase):
    """Requirement 4: filename + function signature preserved."""

    def test_filename_and_signature(self):
        self.assertTrue(hasattr(price_viz, "render_headroom_weekly"))
        import inspect
        sig = inspect.signature(price_viz.render_headroom_weekly)
        params = list(sig.parameters)
        self.assertEqual(params[0], "outdir")

    def test_renders_headroom_weekly_png(self):
        usage_db = _make_usage_db()
        burn_db = _make_burn_db()
        with patch.object(price_viz, "_connect_usage_db", return_value=usage_db), \
             patch.object(price_viz, "_connect_api_burn_db", return_value=burn_db), \
             patch.object(price_viz, "_fetch_quota_payload", return_value=QUOTA_PAYLOAD):
            with tempfile.TemporaryDirectory() as td:
                out = price_viz.render_headroom_weekly(Path(td))
                self.assertEqual(out.name, "headroom-weekly.png")
                self.assertTrue(out.exists())


if __name__ == "__main__":
    unittest.main()
