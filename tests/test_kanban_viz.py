#!/usr/bin/env python3
"""kanban_viz lifecycle + cost rollup reader tests.

Covers:
  * _to_epoch normalization (numeric / ISO Z / space-naive / None)
  * daily_days / build_series math (created/completed/queued/active, abandoned
    exclusion, started-before-window handling)
  * board_weekly_completed + throughput top-N "other" collapsing
  * load_task_cost aggregation from a temp task_cost_rollup DB
  * renderers produce a file gracefully, including empty-data paths
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import importlib.util
_spec = importlib.util.spec_from_file_location("kanban_viz", REPO_ROOT / "kanban_viz.py")
kanban_viz = importlib.util.module_from_spec(_spec)
sys.modules["kanban_viz"] = kanban_viz
_spec.loader.exec_module(kanban_viz)
kv = kanban_viz

DAY = 86400.0
D0 = 1782259200.0  # 2026-06-24 00:00 UTC


def _mk(status, created=D0, started=None, finished=None, board="b1"):
    return {"board": board, "id": "x", "title": "t", "status": status,
            "created_at": created, "started_at": started, "completed_at": finished}


class TestToEpoch(unittest.TestCase):
    def test_numeric(self):
        self.assertEqual(kv._to_epoch(1784981052.0), 1784981052.0)

    def test_numeric_str(self):
        self.assertEqual(kv._to_epoch("1784981052"), 1784981052.0)

    def test_iso_z(self):
        self.assertEqual(kv._to_epoch("2026-08-18T13:35:28Z"), 1787060128.0)

    def test_space_naive_utc(self):
        self.assertEqual(kv._to_epoch("2026-08-19 19:42:40"),
                         1787168560.0)

    def test_none_and_garbage(self):
        self.assertIsNone(kv._to_epoch(None))
        self.assertIsNone(kv._to_epoch("not a date"))


class TestBuildSeries(unittest.TestCase):
    def _days(self):
        return [D0, D0 + DAY, D0 + 2 * DAY]

    def test_created_completed_queued_active(self):
        tasks = [
            _mk("done", created=D0, started=D0, finished=D0 + DAY),   # completes day1
            _mk("todo", created=D0),                                  # queued forever
            _mk("running", created=D0, started=D0),                   # active forever
            _mk("archived", created=D0),                              # excluded
            _mk("cancelled", created=D0),                             # excluded
        ]
        s = kv.build_series(tasks, self._days())
        self.assertEqual(s["created"].tolist(), [3, 3, 3])
        self.assertEqual(s["completed"].tolist(), [0, 1, 1])
        self.assertEqual(s["queued"].tolist(), [1, 1, 1])
        self.assertEqual(s["active"].tolist(), [1, 1, 1])
        self.assertEqual(s["open"].tolist(), [2, 2, 2])

    def test_done_without_completed_at_falls_back(self):
        # done task with no completed_at but started_at set -> completes at start
        tasks = [_mk("done", created=D0, started=D0 + DAY, finished=None)]
        s = kv.build_series(tasks, self._days())
        self.assertEqual(s["completed"].tolist(), [0, 1, 1])

    def test_started_before_window_stays_active(self):
        # running task created+started well before window start
        tasks = [_mk("running", created=D0 - 5 * DAY, started=D0 - 5 * DAY)]
        s = kv.build_series(tasks, self._days())
        self.assertEqual(s["active"].tolist(), [1, 1, 1])
        self.assertEqual(s["created"].tolist(), [1, 1, 1])

    def test_queued_then_active_transition(self):
        tasks = [_mk("todo", created=D0, started=D0 + DAY)]
        s = kv.build_series(tasks, self._days())
        self.assertEqual(s["queued"].tolist(), [1, 0, 0])
        self.assertEqual(s["active"].tolist(), [0, 1, 1])


class TestWeekly(unittest.TestCase):
    def test_weekly_completed(self):
        tasks = [
            _mk("done", created=D0, started=D0, finished=D0),            # mon
            _mk("done", created=D0, started=D0, finished=D0 + 2 * DAY),  # same week
            _mk("done", created=D0, started=D0, finished=D0 + 7 * DAY),  # next week
            _mk("todo", created=D0),
        ]
        w = kv.board_weekly_completed(tasks)
        self.assertEqual(sum(w["b1"].values()), 3)
        self.assertEqual(len(w["b1"]), 2)


class TestTopNOther(unittest.TestCase):
    def test_render_throughput_other(self):
        boards = ["a", "b", "c"]
        tasks = []
        for bi, b in enumerate(boards):
            for w in range(3):
                tasks.append(_mk("done", board=b, created=D0 + w * 7 * DAY,
                                 started=D0 + w * 7 * DAY,
                                 finished=D0 + w * 7 * DAY + 60))
        with tempfile.TemporaryDirectory() as d:
            out = kv.render_throughput(tasks, Path(d), top_n=2)
            self.assertTrue(out.exists())


class TestLoadTaskCost(unittest.TestCase):
    def test_aggregates_from_rollup(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "ba.db"
            con = sqlite3.connect(db)
            con.executescript("""
            CREATE TABLE task_cost_rollup (
                ukey TEXT PRIMARY KEY, call_id INTEGER NOT NULL, ts REAL NOT NULL,
                board TEXT, task_id TEXT, profile TEXT, kind TEXT, session_id TEXT,
                method TEXT, tokens REAL NOT NULL, cost REAL NOT NULL);
            INSERT INTO task_cost_rollup VALUES
              ('1|run|b1|t1|p',1,0,'b1','t1','p','run',NULL,'unique_run',100,0.05),
              ('2|run|b1|t1|p',2,0,'b1','t1','p','run',NULL,'unique_run',200,0.10),
              ('3|run|b2|t2|p',3,0,'b2','t2','p','run',NULL,'unique_run',50,0.02);
            """)
            con.commit()
            con.close()
            rows = kv.load_task_cost(db)
            by = {(r["board"], r["task_id"]): r for r in rows}
            self.assertAlmostEqual(by[("b1", "t1")]["cost"], 0.15)
            self.assertEqual(by[("b1", "t1")]["tokens"], 300.0)
            self.assertEqual(len(rows), 2)

    def test_empty_when_db_missing(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(kv.load_task_cost(Path(d) / "nope.db"), [])


class TestRenderEmpty(unittest.TestCase):
    def test_empty_cost_chart_writes_file(self):
        with tempfile.TemporaryDirectory() as d:
            out = kv.render_cost_per_task({}, [], Path(d))
            self.assertTrue(out.exists())

    def test_empty_tasks_burnup(self):
        with tempfile.TemporaryDirectory() as d:
            days = [D0, D0 + DAY]
            out = kv.render_burnup([], days, Path(d))
            self.assertTrue(out.exists())


if __name__ == "__main__":
    unittest.main()
