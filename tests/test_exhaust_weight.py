"""Tests for src/exhaust_weight.py + flat_router._apply_exhaust_weight.

Covers the soft cost-preference multiplier (Felix: ALERTS-NOT-BLOCKS) that
de-preferences lanes predicted to exhaust their quota soon, without ever
removing a lane or touching the pressure FSM.
"""
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import exhaust_weight as ew
import flat_router as fr


# ── Fixtures ────────────────────────────────────────────────────────────────

def _make_db(path, rows):
    """Create a kalman_samples table at ``path`` and insert ``rows``.

    Each row is a dict with keys: key, ts, exhausts_in_hours, will_exhaust.
    """
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE IF EXISTS kalman_samples")
    conn.execute(
        """CREATE TABLE kalman_samples (
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
        )"""
    )
    for r in rows:
        conn.execute(
            "INSERT INTO kalman_samples (ts, key, window, exhausts_in_hours, will_exhaust) "
            "VALUES (?, ?, ?, ?, ?)",
            (r["ts"], r["key"], r.get("window", "weekly"),
             r.get("exhausts_in_hours"), r.get("will_exhaust", 0)),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def sample_db(tmp_path):
    """A DB with a fresh sample for 'ours' (will_exhaust=1, 2h) and 'friend' (0)."""
    path = str(tmp_path / "zai_usage.db")
    now = time.time()
    _make_db(path, [
        {"key": "ours", "ts": now, "window": "weekly",
         "exhausts_in_hours": 2.0, "will_exhaust": 1},
        {"key": "friend", "ts": now, "window": "weekly",
         "exhausts_in_hours": None, "will_exhaust": 0},
    ])
    return path, now


# ── Unit tests: exhaust_multiplier ──────────────────────────────────────────

class TestExhaustMultiplier:
    def test_fresh_will_exhaust_short_horizon(self, sample_db):
        path, now = sample_db
        # exhausts_in_hours=2, HORIZON=6 → urgency = 1 - 2/6 = 0.6667
        # multiplier = 1 + 0.5 * 0.6667 = 1.3333
        m = ew.exhaust_multiplier("ours", db_path=path, now=now)
        assert m == pytest.approx(1.0 + 0.5 * (1.0 - 2.0 / 6.0))

    def test_will_exhaust_zero_returns_one(self, sample_db):
        path, now = sample_db
        assert ew.exhaust_multiplier("friend", db_path=path, now=now) == 1.0

    def test_exhausts_in_hours_zero_max_penalty(self, sample_db):
        path, now = sample_db
        # exhausts_in_hours=0 → urgency=1 → multiplier = 1 + ALPHA = 1.5
        _make_db(path, [{"key": "ours", "ts": now, "exhausts_in_hours": 0.0,
                         "will_exhaust": 1}])
        assert ew.exhaust_multiplier("ours", db_path=path, now=now) == pytest.approx(1.5)

    def test_exhausts_in_hours_huge_clamps_to_one(self, sample_db):
        path, now = sample_db
        # exhausts_in_hours=100 → min(100/6,1)=1 → urgency=0 → multiplier=1.0
        _make_db(path, [{"key": "ours", "ts": now, "exhausts_in_hours": 100.0,
                         "will_exhaust": 1}])
        assert ew.exhaust_multiplier("ours", db_path=path, now=now) == pytest.approx(1.0)

    def test_stale_sample_returns_one(self, sample_db):
        path, now = sample_db
        # Advance the clock 3h past the sample → stale → no effect
        assert ew.exhaust_multiplier("ours", db_path=path, now=now + 3 * 3600) == 1.0

    def test_missing_table_returns_one(self, tmp_path):
        path = str(tmp_path / "empty.db")
        sqlite3.connect(path).close()  # empty DB, no kalman_samples table
        assert ew.exhaust_multiplier("ours", db_path=path) == 1.0

    def test_missing_key_returns_one(self, sample_db):
        path, now = sample_db
        assert ew.exhaust_multiplier("nonexistent", db_path=path, now=now) == 1.0

    def test_unreadable_db_returns_one(self, tmp_path):
        # A path that is a directory → sqlite3.connect raises → caught → 1.0
        assert ew.exhaust_multiplier("ours", db_path=str(tmp_path)) == 1.0

    def test_exhausts_in_hours_none_returns_one(self, sample_db):
        path, now = sample_db
        _make_db(path, [{"key": "ours", "ts": now, "exhausts_in_hours": None,
                         "will_exhaust": 1}])
        assert ew.exhaust_multiplier("ours", db_path=path, now=now) == 1.0

    def test_multiple_windows_uses_most_urgent(self, sample_db):
        path, now = sample_db
        # Two windows at the same ts: 5-hour (5h) and weekly (1h) → use 1h
        _make_db(path, [
            {"key": "ours", "ts": now, "window": "5-hour",
             "exhausts_in_hours": 5.0, "will_exhaust": 1},
            {"key": "ours", "ts": now, "window": "weekly",
             "exhausts_in_hours": 1.0, "will_exhaust": 1},
        ])
        m = ew.exhaust_multiplier("ours", db_path=path, now=now)
        assert m == pytest.approx(1.0 + 0.5 * (1.0 - 1.0 / 6.0))


# ── Unit tests: _apply_exhaust_weight (flat_router wiring) ──────────────────

class TestApplyExhaustWeight:
    def test_inf_cost_untouched(self):
        assert fr._apply_exhaust_weight("ours", float("inf")) == float("inf")

    def test_nan_cost_untouched(self):
        import math
        assert math.isnan(fr._apply_exhaust_weight("ours", float("nan")))

    def test_multiplies_finite_cost(self, sample_db, monkeypatch):
        path, now = sample_db
        monkeypatch.setattr(ew, "DB_PATH", path)
        # ours: will_exhaust=1, 2h → ×1.3333
        result = fr._apply_exhaust_weight("ours", 0.001)
        assert result == pytest.approx(0.001 * (1.0 + 0.5 * (1.0 - 2.0 / 6.0)))

    def test_no_effect_when_no_exhaust(self, sample_db, monkeypatch):
        path, now = sample_db
        monkeypatch.setattr(ew, "DB_PATH", path)
        assert fr._apply_exhaust_weight("friend", 0.001) == pytest.approx(0.001)


# ── Integration test: select_provider() ordering changes ────────────────────

class TestSelectProviderOrdering:
    def test_lane_flips_will_exhaust_reorders(self, sample_db, monkeypatch):
        """When 'ours' flips will_exhaust=1 with short exhausts_in_hours, its
        effective cost inflates and it sorts below a lane it previously beat."""
        path, now = sample_db
        monkeypatch.setattr(ew, "DB_PATH", path)

        # Baseline: 'ours' not exhausting → cost unchanged.
        _make_db(path, [{"key": "ours", "ts": now, "exhausts_in_hours": None,
                         "will_exhaust": 0}])
        base = fr._apply_exhaust_weight("ours", 0.001)
        assert base == pytest.approx(0.001)

        # Flip: 'ours' will exhaust in 1h → cost ×(1 + 0.5*(1-1/6)) ≈ ×1.4167
        _make_db(path, [{"key": "ours", "ts": now, "exhausts_in_hours": 1.0,
                         "will_exhaust": 1}])
        inflated = fr._apply_exhaust_weight("ours", 0.001)
        assert inflated > base
        assert inflated == pytest.approx(0.001 * (1.0 + 0.5 * (1.0 - 1.0 / 6.0)))

    def test_soft_preference_never_removes_lane(self, sample_db, monkeypatch):
        """A lane predicted to exhaust is still present in the candidate set —
        only its cost (and thus ordering) changes."""
        path, now = sample_db
        monkeypatch.setattr(ew, "DB_PATH", path)
        _make_db(path, [{"key": "ours", "ts": now, "exhausts_in_hours": 0.5,
                         "will_exhaust": 1}])

        candidates = fr.select_provider(model="glm-5.2")
        names = [c.name for c in candidates]
        assert "ours" in names, "exhaust-predicted lane must NOT be removed"
