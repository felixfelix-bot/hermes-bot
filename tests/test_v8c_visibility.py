#!/usr/bin/env python3
"""V8c visibility fixes — TDD suite.

Covers the three V8c fixes to :func:`price_viz.render_headroom_weekly`:

  1. Sub-pixel z.ai token lanes (ours/friend, 14M weekly) were invisible on the
     shared linear Panel A y-axis (0.4% of the 3.5B ollama scale). Fix: Panel A
     switched to log scale so 14M and 3.5B lanes are both visible, with a
     clamped floor and a "log scale" annotation.

  2. Anonymous flat hatch (opencode_go / ollama_cloud_3, regime="included")
     was drawn as ONE unnamed gray band. Fix: each flat lane is its own NAMED
     band labeled "<lane> — <N tokens> tokens / <N calls> calls (7d)".

  3. neuralwatt $0 flatline in Panel B: the reader read
     ``provider_balances.limit_remaining`` which the collector writes as 0.0
     on every neuralwatt row; the real balance lives in ``raw_json``
     (``remaining_usd``). Fix: reader-side fallback to ``raw_json.remaining_usd``
     when ``limit_remaining`` is 0/None.

Tests assert on data/labels via real matplotlib axes (Agg) — legend text,
y-axis scale, and the neuralwatt line's y-data — never on pixel output.
"""
import os
import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Load THIS worktree's price_viz.py directly (mirrors test_v8b_dynamic_headroom).
import importlib.util
_price_viz_path = REPO_ROOT / "price_viz.py"
_spec = importlib.util.spec_from_file_location("price_viz", _price_viz_path)
price_viz = importlib.util.module_from_spec(_spec)
sys.modules["price_viz"] = price_viz
_spec.loader.exec_module(price_viz)

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


# ── Fake DB helpers (mirror test_v8b_dynamic_headroom) ──────────────────────

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
    def __init__(self):
        self.calls = []
        self.row_factory = None
        self._rows_by_query = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        for sql_frag, rows in self._rows_by_query:
            if sql_frag in sql:
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
    class _R:
        def __init__(self, data):
            self.__dict__.update(data)
        def __getitem__(self, k):
            return self.__dict__[k]
    return _R(kw)


# ── Fixtures ────────────────────────────────────────────────────────────────

QUOTA_PAYLOAD = {
    "ours": {"windows": [{"name": "weekly", "type": "CREDIT_LIMIT", "used_pct": 100}],
             "locked": True, "locked_window": "weekly", "locked_pct": 100,
             "max_pct": 100, "age_s": 42, "predictions": []},
    "friend": {"windows": [{"name": "weekly", "type": "CREDIT_LIMIT", "used_pct": 5}],
               "locked": False, "max_pct": 5, "age_s": 42, "predictions": []},
    "ollama_cloud": {"used_pct": 33.76, "remaining": 331200000.0,
                     "total": 500000000, "regime": "included"},
    "ollama_cloud_2": {"used_pct": 0.32, "remaining": 498400000.0,
                       "total": 500000000, "regime": "included"},
    "opencode_go": {"used_pct": 0.0, "remaining": float("inf"),
                    "total": float("inf"), "regime": "included"},
    "neuralwatt": {"used_pct": 99.47, "remaining": 0.0698, "total": 13.3333,
                   "remaining_usd": 8.9951, "total_credits_usd": 11.0},
}

_NOW = 1_788_000_000.0


def _make_usage_db(flat_tokens=12_400_000, flat_calls=370):
    """Fake usage DB: token lanes + flat-lane usage rows + telnyx spend."""
    db = FakeDB()
    token_rows = []
    for lane in ["ours", "friend", "ollama_cloud", "ollama_cloud_2"]:
        token_rows.append(_row(key_name=lane, ts=_NOW - 3600, total_tokens=1_000_000))
        token_rows.append(_row(key_name=lane, ts=_NOW - 7200, total_tokens=2_000_000))
    # opencode_go (flat) usage: flat_calls rows summing to flat_tokens
    per = max(1, flat_calls)
    base = flat_tokens // per
    for i in range(per):
        token_rows.append(_row(key_name="opencode_go", ts=_NOW - 3600 - i * 60,
                               total_tokens=base))
    token_rows.append(_row(key_name="telnyx", ts=_NOW - 3600, total_tokens=0, cost_usd=2.0))
    token_rows.append(_row(key_name="telnyx", ts=_NOW - 7200, total_tokens=0, cost_usd=1.0))
    db._rows_by_query = [("FROM api_calls", token_rows)]
    return db


def _make_burn_db(neuralwatt_remaining_usd=25.28):
    """Fake api_burn DB: USD lanes. neuralwatt has limit_remaining=0.0 but
    real balance in raw_json.remaining_usd."""
    db = FakeDB()
    rows = [
        _row(collected_at=_NOW - 3600, provider="routstrd", limit_remaining=15.5),
        _row(collected_at=_NOW - 7200, provider="routstrd", limit_remaining=16.0),
        _row(collected_at=_NOW - 3600, provider="ppq", limit_remaining=0.001),
        _row(collected_at=_NOW - 7200, provider="ppq", limit_remaining=0.002),
        # neuralwatt: mirror column 0.0, real balance in raw_json
        _row(collected_at=_NOW - 3600, provider="neuralwatt", limit_remaining=0.0,
             raw_json=json.dumps({"remaining_usd": neuralwatt_remaining_usd,
                                  "total_credits_usd": 56.0})),
        _row(collected_at=_NOW - 7200, provider="neuralwatt", limit_remaining=0.0,
             raw_json=json.dumps({"remaining_usd": neuralwatt_remaining_usd + 1.0,
                                  "total_credits_usd": 56.0})),
    ]
    db._rows_by_query = [("FROM provider_balances", rows)]
    return db


# ── Render harness: capture real axes ────────────────────────────────────────

def _render_and_capture():
    """Run render_headroom_weekly with fakes, returning (ax_a, ax_b)."""
    usage_db = _make_usage_db()
    burn_db = _make_burn_db()
    captured = {}

    _real_subplots = price_viz.plt.subplots

    def _capturing_subplots(*a, **kw):
        fig, axes = _real_subplots(*a, **kw)
        captured["fig"] = fig
        captured["axes"] = axes
        return fig, axes

    with patch.object(price_viz, "_connect_usage_db", return_value=usage_db), \
         patch.object(price_viz, "_connect_api_burn_db", return_value=burn_db), \
         patch.object(price_viz, "_fetch_quota_payload", return_value=QUOTA_PAYLOAD), \
         patch.object(price_viz.plt, "subplots", _capturing_subplots):
        with tempfile.TemporaryDirectory() as td:
            price_viz.render_headroom_weekly(Path(td))
    ax_a, ax_b = captured["axes"]
    return ax_a, ax_b


def _legend_labels(ax):
    leg = ax.get_legend()
    if leg is None:
        return []
    return [t.get_text() for t in leg.get_texts()]


# ── Fix 1: z.ai lanes visible on log-scale Panel A ───────────────────────────

class TestZaiLaneVisibility(unittest.TestCase):
    def test_panel_a_is_log_scale(self):
        ax_a, _ = _render_and_capture()
        self.assertEqual(ax_a.get_yscale(), "log",
                         "Panel A must be log-scale so 14M z.ai lanes are visible")

    def test_legend_contains_ours_and_friend(self):
        ax_a, _ = _render_and_capture()
        labels = _legend_labels(ax_a)
        self.assertIn("ours", labels, f"legend missing 'ours': {labels}")
        self.assertIn("friend", labels, f"legend missing 'friend': {labels}")


# ── Fix 2: flat lanes are NAMED bands with a usage note ──────────────────────

class TestFlatLaneNaming(unittest.TestCase):
    def test_opencode_go_named_with_usage_note(self):
        ax_a, _ = _render_and_capture()
        labels = _legend_labels(ax_a)
        oc = [l for l in labels if l.startswith("opencode_go")]
        self.assertTrue(oc, f"legend missing opencode_go label: {labels}")
        # usage note carries tokens + calls
        self.assertIn("tokens", oc[0], f"opencode_go label lacks usage note: {oc[0]}")

    def test_each_flat_lane_has_own_label(self):
        # opencode_go is the only flat lane in the static registry; assert it is
        # rendered as its own label rather than the old anonymous band.
        ax_a, _ = _render_and_capture()
        labels = _legend_labels(ax_a)
        flat_named = [l for l in labels if "tokens / " in l]
        self.assertTrue(flat_named, f"no flat lane rendered with usage note: {labels}")


# ── Fix 3: neuralwatt $0 flatline → raw_json.remaining_usd fallback ──────────

class TestNeuralWattReaderFallback(unittest.TestCase):
    def test_parse_usd_remaining_falls_back_to_raw_json(self):
        f = price_viz._parse_usd_remaining
        self.assertEqual(f(0.0, json.dumps({"remaining_usd": 25.28})), 25.28)
        self.assertEqual(f(None, json.dumps({"remaining_usd": 25.28})), 25.28)
        # healthy mirror column is used as-is
        self.assertEqual(f(15.5, json.dumps({"remaining_usd": 99.0})), 15.5)
        # no raw_json + zero → None
        self.assertIsNone(f(0.0, None))
        self.assertIsNone(f(0.0, "not json"))

    def test_neuralwatt_line_nonzero(self):
        _, ax_b = _render_and_capture()
        target = None
        for line in ax_b.get_lines():
            if line.get_label() == "neuralwatt":
                target = line
                break
        self.assertIsNotNone(target, "neuralwatt line missing from Panel B")
        ydata = [y for y in target.get_ydata() if y == y]  # drop NaN
        self.assertTrue(ydata and max(ydata) > 0,
                        f"neuralwatt line flat at $0 despite raw_json balance: {ydata}")


if __name__ == "__main__":
    unittest.main()
