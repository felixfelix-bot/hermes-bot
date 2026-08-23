#!/usr/bin/env python3
"""Tests for burn_attribution.py + token_backfill.py — R6 attribution engine.

Fixture-based: synthetic runs / sessions / messages / calls. Pure logic,
no live DBs.

Run:  python3 -m pytest tests/test_burn_attribution.py -v   (from ~/.hermes/bot)
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import burn_attribution as ba  # noqa: E402
import token_backfill as tb  # noqa: E402


T0 = 1780000000.0  # fixed epoch for fixtures


def mk_run(board, task, profile, start, end=None, runtime=3600):
    return {
        "board": board,
        "task_id": task,
        "profile": profile,
        "start": start,
        "end": end,
        "max_runtime": runtime,
    }


class RunIntervalTests(unittest.TestCase):
    def test_run_end_uses_ended_at_when_present(self):
        r = ba.run_interval(mk_run("b", "t1", "w1", T0, end=T0 + 100), now=T0 + 9999)
        self.assertEqual(r["end"], T0 + 100)

    def test_run_end_grace_for_open_runs(self):
        # ended_at NULL: clamp to last_heartbeat+grace, never past now
        r = ba.run_interval(
            {"board": "b", "task_id": "t1", "profile": "w1", "start": T0,
             "end": None, "max_runtime": 600, "heartbeat": T0 + 300},
            now=T0 + 9999,
        )
        self.assertEqual(r["end"], T0 + 300 + ba.OPEN_RUN_GRACE)
        r2 = ba.run_interval(
            {"board": "b", "task_id": "t1", "profile": "w1", "start": T0,
             "end": None, "max_runtime": 600, "heartbeat": None},
            now=T0 + 100,
        )
        self.assertEqual(r2["end"], T0 + 100)  # clamped at now

    def test_active_runs_at_ts(self):
        runs = [
            mk_run("b1", "t1", "w1", T0, end=T0 + 100),
            mk_run("b2", "t2", "w2", T0 + 50, end=T0 + 200),
        ]
        ivs = [ba.run_interval(r, now=T0 + 9999) for r in runs]
        self.assertEqual(len(ba.active_runs(ivs, T0 + 10)), 1)
        self.assertEqual(len(ba.active_runs(ivs, T0 + 60)), 2)
        self.assertEqual(len(ba.active_runs(ivs, T0 + 500)), 0)


class AttributeCallTests(unittest.TestCase):
    def setUp(self):
        self.runs = [
            mk_run("b1", "t1", "worker-a", T0, end=T0 + 600),
            mk_run("b2", "t2", "worker-b", T0 + 100, end=T0 + 700),
        ]
        self.ivs = [ba.run_interval(r, now=T0 + 9999) for r in self.runs]
        # message activity: worker-a has 5 msgs near T0+200, worker-b has 1
        self.msgs = {
            "worker-a": [(T0 + 150, "s-a1")] * 5,
            "worker-b": [(T0 + 150, "s-b1")],
        }
        self.sess = {
            "worker-a": [("s-a1", T0, T0 + 600)],
            "worker-b": [("s-b1", T0 + 100, T0 + 700)],
            "manager": [("s-m1", T0 - 50, None)],
        }
        # manager chat active the whole time, 2 msgs near T0+200
        self.msgs["manager"] = [(T0 + 140, "s-m1")] * 2

    def _attr(self, ts, **kw):
        return ba.attribute_call(
            {"id": 1, "ts": ts, "key_name": "telnyx", "model": "m",
             "tokens": 1000, "cost_usd": 1.0},
            self.ivs, self.sess, self.msgs, now=T0 + 9999, **kw,
        )

    def test_unique_run_full_share(self):
        # T0+10: only worker-a's run active. worker-b's run starts T0+100;
        # manager/worker-b have no messages within ±MSG_WINDOW of T0+10,
        # so they are not session candidates → single run candidate.
        res = self._attr(T0 + 10)
        self.assertEqual(len(res), 1)
        r = res[0]
        self.assertEqual((r["board"], r["task_id"], r["profile"]), ("b1", "t1", "worker-a"))
        self.assertEqual(r["kind"], "run")
        self.assertEqual(r["method"], "unique_run")
        self.assertAlmostEqual(r["share"], 1.0)
        self.assertAlmostEqual(r["tokens_share"], 1000)
        self.assertAlmostEqual(r["cost_share"], 1.0)

    def test_weighted_split_across_profiles(self):
        res = self._attr(T0 + 200)
        # worker-a (5 msgs) vs worker-b (1 msg) vs manager session (2 msgs)
        by = {}
        for r in res:
            by[(r["board"], r["task_id"], r["profile"], r["kind"])] = r["share"]
        wa = by.get(("b1", "t1", "worker-a", "run"), 0.0)
        wb = by.get(("b2", "t2", "worker-b", "run"), 0.0)
        wm = by.get((None, None, "manager", "session"), 0.0)
        self.assertAlmostEqual(wa, 5 / 8)
        self.assertAlmostEqual(wb, 1 / 8)
        self.assertAlmostEqual(wm, 2 / 8)
        self.assertAlmostEqual(sum(r["share"] for r in res), 1.0)
        self.assertTrue(all(r["method"] == "weighted" for r in res))
        # manager's session identity is carried in session_id
        mgr = [r for r in res if r["kind"] == "session"]
        self.assertEqual(mgr[0]["session_id"], "s-m1")

    def test_same_profile_concurrent_runs_split_evenly(self):
        runs = [
            mk_run("b1", "t1", "worker-a", T0, end=T0 + 600),
            mk_run("b1", "t3", "worker-a", T0, end=T0 + 600),
        ]
        ivs = [ba.run_interval(r, now=T0 + 9999) for r in runs]
        sess = {"worker-a": [("s-a1", T0, T0 + 600)]}
        msgs = {"worker-a": [(T0 + 100, "s-a1")]}
        res = ba.attribute_call(
            {"id": 1, "ts": T0 + 200, "tokens": 100, "cost_usd": 0.0},
            ivs, sess, msgs, now=T0 + 9999,
        )
        self.assertEqual(len(res), 2)
        for r in res:
            self.assertAlmostEqual(r["share"], 0.5)

    def test_no_candidates_unattributed(self):
        res = ba.attribute_call(
            {"id": 1, "ts": T0 + 5000, "tokens": 100, "cost_usd": 0.0},
            self.ivs, {}, {}, now=T0 + 9999,
        )
        self.assertEqual(res, [])
        self.assertEqual(
            ba.unattributed({"id": 1, "ts": T0 + 5000, "tokens": 100})["method"],
            "unattributed",
        )

    def test_session_without_run_attributed_as_session(self):
        # no runs active at all → all three sessions with recent messages
        # are candidates, weighted by their message activity
        res = ba.attribute_call(
            {"id": 1, "ts": T0 + 200, "tokens": 100, "cost_usd": 0.0},
            [], self.sess, self.msgs, now=T0 + 9999,
        )
        sess_rows = [r for r in res if r["kind"] == "session"]
        self.assertEqual(len(sess_rows), 3)
        by_prof = {r["profile"]: r["share"] for r in sess_rows}
        self.assertAlmostEqual(by_prof["worker-a"], 5 / 8)
        self.assertAlmostEqual(by_prof["worker-b"], 1 / 8)
        self.assertAlmostEqual(by_prof["manager"], 2 / 8)
        mgr = [r for r in sess_rows if r["profile"] == "manager"][0]
        self.assertEqual(mgr["session_id"], "s-m1")

    def test_idle_session_not_candidate(self):
        # manager session active but silent in the window → not a candidate
        msgs = {k: v for k, v in self.msgs.items() if k != "manager"}
        res = ba.attribute_call(
            {"id": 1, "ts": T0 + 200, "tokens": 100, "cost_usd": 0.0},
            [], self.sess, msgs, now=T0 + 9999,
        )
        profs = {r["profile"] for r in res}
        self.assertNotIn("manager", profs)

    def test_tokens_split_proportionally(self):
        res = self._attr(T0 + 200)
        total = sum(r["tokens_share"] for r in res)
        self.assertAlmostEqual(total, 1000.0)


class TotalsTests(unittest.TestCase):
    def test_summary_percent(self):
        s = ba.summarize(
            total_calls=100,
            total_tokens=1000,
            attributed_tokens=810,
            method_counts={"unique": 50, "weighted": 30, "unattributed": 20},
        )
        self.assertAlmostEqual(s["pct_tokens_attributed"], 81.0)


class TokenBackfillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = sqlite3.connect(os.path.join(self.tmp.name, "state.db"))
        self.state.executescript(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT, role TEXT, content TEXT,
                tool_call_id TEXT, tool_calls TEXT, tool_name TEXT,
                timestamp REAL, token_count INTEGER, finish_reason TEXT
            );
            """
        )
        base = time.time() - 3600
        # session s1: assistant msg right after call, another later
        self.state.executemany(
            "INSERT INTO messages (session_id, role, content, timestamp,"
            " token_count) VALUES (?,?,?,?,?)",
            [
                ("s1", "assistant", "hello", base + 20, None),
                ("s1", "assistant", "again", base + 400, None),
                ("s2", "assistant", "other session", base + 25, None),
                ("s1", "assistant", "already set", base + 30, 999),
            ],
        )
        self.state.commit()
        self.call = {"call_id": 7, "ts": base, "profile": "p1",
                     "completion_tokens": 87}

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def test_assigns_completion_to_first_following_assistant(self):
        n = tb.backfill_session_tokens(self.state, "s1", self.call, now=time.time())
        self.assertEqual(n, 1)
        row = self.state.execute(
            "SELECT token_count FROM messages WHERE session_id='s1'"
            " AND content='hello'"
        ).fetchone()
        self.assertEqual(row[0], 87)
        # 'again' untouched (outside match window), 'already set' untouched
        row2 = self.state.execute(
            "SELECT token_count FROM messages WHERE content='again'"
        ).fetchone()
        self.assertIsNone(row2[0])

    def test_never_overwrites_existing(self):
        tb.backfill_session_tokens(self.state, "s1", self.call, now=time.time())
        row = self.state.execute(
            "SELECT token_count FROM messages WHERE content='already set'"
        ).fetchone()
        self.assertEqual(row[0], 999)

    def test_ambiguous_session_not_touched(self):
        # call ts sits between two sessions' messages; scope=s2 only matches s2
        n = tb.backfill_session_tokens(
            self.state, "s2", self.call, now=time.time()
        )
        self.assertEqual(n, 1)

    def test_recent_messages_skipped(self):
        # messages newer than freshness cutoff are not written (live races)
        base = time.time() - 10
        self.state.execute(
            "INSERT INTO messages (session_id, role, content, timestamp,"
            " token_count) VALUES ('s3','assistant','fresh',?,NULL)",
            (base + 5,),
        )
        self.state.commit()
        n = tb.backfill_session_tokens(
            self.state, "s3", {"call_id": 8, "ts": base, "profile": "p1",
                               "completion_tokens": 50},
            now=time.time(),
        )
        self.assertEqual(n, 0)


class LoadAttributionTargetsTests(unittest.TestCase):
    """Regression: ATTR_DB and ZAI_USAGE_DB are SEPARATE databases.

    load_attribution_targets used to JOIN attribution × api_calls inside
    the ATTR_DB connection, which crashes with 'no such table: api_calls'
    on the real layout. It must query each DB separately.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.attr_db = os.path.join(self.tmp.name, "attr.db")
        self.zai_db = os.path.join(self.tmp.name, "zai.db")

        conn = sqlite3.connect(self.attr_db)
        conn.executescript(ba.ATTR_SCHEMA)
        conn.execute(
            """INSERT INTO attribution
               (attributed_at, window_since, call_id, ts, profile, kind,
                session_id, method, share, tokens_share, cost_share)
               VALUES (?, ?, ?, ?, ?, 'session', ?, 'weighted', 1.0, 10, 0)""",
            (T0, T0, 42, T0 + 10, "worker-a", "sess-a1"),
        )
        conn.commit()
        conn.close()

        zconn = sqlite3.connect(self.zai_db)
        zconn.execute(
            "CREATE TABLE api_calls (id INTEGER PRIMARY KEY, ts REAL,"
            " completion_tokens INTEGER)"
        )
        zconn.execute(
            "INSERT INTO api_calls (id, ts, completion_tokens) VALUES (42, ?, 77)",
            (T0 + 10,),
        )
        zconn.commit()
        zconn.close()

        self._orig_attr, ba.ATTR_DB = ba.ATTR_DB, self.attr_db
        self._orig_zai, tb.ZAI_DB = tb.ZAI_DB, self.zai_db

    def _add_attribution(self, window, call_id, profile, session_id):
        conn = sqlite3.connect(self.attr_db)
        conn.execute(
            """INSERT INTO attribution
               (attributed_at, window_since, call_id, ts, profile, kind,
                session_id, method, share, tokens_share, cost_share)
               VALUES (?, ?, ?, ?, ?, 'session', ?, 'weighted', 1.0, 10, 0)""",
            (window, window, call_id, window + 10, profile, session_id),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        ba.ATTR_DB = self._orig_attr
        tb.ZAI_DB = self._orig_zai
        self.tmp.cleanup()

    def test_cross_db_lookup_returns_call_details(self):
        targets = tb.load_attribution_targets(T0)
        self.assertEqual(len(targets), 1)
        t = targets[0]
        self.assertEqual(t["profile"], "worker-a")
        self.assertEqual(t["session_id"], "sess-a1")
        self.assertEqual(t["call"]["call_id"], 42)
        self.assertEqual(t["call"]["ts"], T0 + 10)
        self.assertEqual(t["call"]["completion_tokens"], 77)

    def test_unknown_call_id_skipped(self):
        # attribution row pointing at a pruned api_calls row → skipped
        self._add_attribution(T0, 99, "worker-b", "sess-b1")
        targets = tb.load_attribution_targets(T0)
        self.assertEqual(len(targets), 1)  # only the resolvable one

    def test_window_drift_tolerated(self):
        # burn_attribution.py ran in another process: its window_since is
        # time.time()-48h computed there, never == our time.time()-48h.
        # A drifted window (T0+120, distinct profile+call) must still match;
        # a far window (T0+7200) must not — even when its call exists.
        zconn = sqlite3.connect(self.zai_db)
        zconn.execute(
            "INSERT INTO api_calls (id, ts, completion_tokens)"
            " VALUES (43, ?, 88)", (T0 + 130,),
        )
        zconn.execute(
            "INSERT INTO api_calls (id, ts, completion_tokens)"
            " VALUES (44, ?, 99)", (T0 + 7210,),
        )
        zconn.commit()
        zconn.close()
        self._add_attribution(T0 + 120, 43, "worker-d", "sess-d1")
        self._add_attribution(T0 + 7200, 44, "worker-c", "sess-c1")
        targets = tb.load_attribution_targets(T0)
        profiles = {t["profile"] for t in targets}
        self.assertIn("worker-d", profiles)  # drifted window included
        self.assertNotIn("worker-c", profiles)  # far window excluded


if __name__ == "__main__":
    unittest.main()
