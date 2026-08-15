#!/usr/bin/env python3
"""Tests for ppq_common.py + ppq_backfill.py — R6 ppq_queries backfill.

Fixture-based: temp sqlite DBs, fake fetchers. No network, no live writes.

Run:  python3 -m pytest tests/test_ppq_backfill.py -v   (from ~/.hermes/bot)
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

import ppq_common  # noqa: E402
import ppq_backfill  # noqa: E402


def make_burn_db(path):
    conn = sqlite3.connect(path)
    # replicate the legacy collector schema (no unique dedup index)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ppq_queries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            model TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            total_tokens INTEGER,
            cost_usd REAL,
            query_type TEXT,
            api_key_id TEXT
        );
        CREATE TABLE IF NOT EXISTS balance_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, provider TEXT, balance_usd REAL,
            total_credits REAL, total_usage REAL, currency TEXT,
            raw TEXT, error TEXT
        );
        """
    )
    conn.commit()
    return conn


def count_pq(conn):
    return conn.execute("SELECT COUNT(*) FROM ppq_queries").fetchone()[0]


class PpqCommonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = make_burn_db(os.path.join(self.tmp.name, "burn.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_record_inserts_and_normalizes(self):
        recs = [
            {
                "ts": 1780000000.0,
                "model": "kimi-k3",
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
                "cost_usd": 0.0012,
                "query_type": "chat",
                "api_key_id": "key-1",
            }
        ]
        ins, dup = ppq_common.record_ppq_queries(self.conn, recs)
        self.assertEqual((ins, dup), (1, 0))
        row = self.conn.execute(
            "SELECT ts, model, input_tokens, output_tokens, total_tokens,"
            " cost_usd, query_type, api_key_id FROM ppq_queries"
        ).fetchone()
        self.assertEqual(row[1], "kimi-k3")
        self.assertEqual(row[4], 150)
        self.assertAlmostEqual(row[5], 0.0012)

    def test_record_dedups_identical_rows(self):
        rec = {
            "ts": 1780000000.0,
            "model": "kimi-k3",
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
            "cost_usd": 0.0,
            "query_type": "chat",
            "api_key_id": "key-1",
        }
        ppq_common.record_ppq_queries(self.conn, [rec])
        ins, dup = ppq_common.record_ppq_queries(self.conn, [dict(rec)])
        self.assertEqual((ins, dup), (0, 1))
        self.assertEqual(count_pq(self.conn), 1)

    def test_record_handles_missing_fields(self):
        recs = [{"ts": 1780000001.0}]  # sparse record
        ins, _ = ppq_common.record_ppq_queries(self.conn, recs)
        self.assertEqual(ins, 1)
        row = self.conn.execute(
            "SELECT model, input_tokens, cost_usd FROM ppq_queries"
        ).fetchone()
        self.assertEqual(row[1], 0)
        self.assertEqual(row[2], 0.0)

    def test_ensure_schema_idempotent_and_adds_dedup_index(self):
        ppq_common.ensure_schema(self.conn)
        ppq_common.ensure_schema(self.conn)  # twice: no error
        idx = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND name='idx_ppq_queries_dedup'"
        ).fetchone()
        self.assertIsNotNone(idx)

    def test_normalize_api_query_parses_iso_and_coerces(self):
        raw = {
            "timestamp": "2026-08-14T01:48:23Z",
            "model": "glm-5.2",
            "input_count": "1234",
            "output_count": None,
            "price_in_usd": "0.005",
            "query_type": "reasoning",
            "api_key_id": 42,
        }
        rec = ppq_common.normalize_api_query(raw)
        self.assertIsInstance(rec["ts"], float)
        self.assertEqual(rec["input_tokens"], 1234)
        self.assertEqual(rec["output_tokens"], 0)
        self.assertAlmostEqual(rec["cost_usd"], 0.005)
        self.assertEqual(rec["api_key_id"], "42")
        self.assertEqual(rec["total_tokens"], 1234)


class FetchPagesTests(unittest.TestCase):
    def _page(self, queries, total):
        return {"status": "success", "data": queries, "pagination": {"total": total}}

    def test_stops_on_empty_page(self):
        calls = {"n": 0}

        def fetch(page):
            calls["n"] += 1
            return self._page([], 0) if page > 2 else self._page(
                [{"timestamp": "2026-08-14T01:00:00Z"}], 5
            )

        out = ppq_backfill.fetch_history_pages(fetch_page=fetch, max_pages=0)
        self.assertEqual(len(out), 2)
        self.assertEqual(calls["n"], 3)

    def test_stops_on_bad_status(self):
        def fetch(page):
            return {"status": "error"}

        out = ppq_backfill.fetch_history_pages(fetch_page=fetch, max_pages=0)
        self.assertEqual(out, [])

    def test_respects_max_pages(self):
        def fetch(page):
            return self._page([{"timestamp": "2026-08-14T01:00:00Z"}], 999)

        out = ppq_backfill.fetch_history_pages(fetch_page=fetch, max_pages=3)
        self.assertEqual(len(out), 3)

    def test_since_filter(self):
        now = time.time()

        def fetch(page):
            old = "2020-01-01T00:00:00Z"
            new = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 60))
            return self._page(
                [
                    {"timestamp": new},
                    {"timestamp": old},
                ],
                2,
            )

        out = ppq_backfill.fetch_history_pages(
            fetch_page=fetch, max_pages=1, since=now - 3600
        )
        self.assertEqual(len(out), 1)


class ProxyLogBackfillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.burn = make_burn_db(os.path.join(self.tmp.name, "burn.db"))
        self.zai = sqlite3.connect(os.path.join(self.tmp.name, "zai.db"))
        self.zai.executescript(
            """
            CREATE TABLE api_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, key_name TEXT, key_suffix TEXT, model TEXT,
                prompt_tokens INTEGER, completion_tokens INTEGER,
                total_tokens INTEGER, tier TEXT, cache_hit INTEGER,
                ollama_hit INTEGER, ppq_hit INTEGER, status_code INTEGER,
                error TEXT, duration_ms INTEGER, cost_usd REAL,
                cost_source TEXT, session_id TEXT
            );
            """
        )
        base = time.time() - 7200
        rows = [
            (base, "ppq", "kimi-k3", 1000, 200, 1200, 0.01, 1),
            (base + 10, "ppq", "glm-5.2", 500, 100, 600, None, 1),
            (base + 20, "telnyx", "kimi-k3", 9999, 99, 10098, 5.0, 0),
            (base + 30, "ppq", "kimi-k3", 1000, 200, 1200, 0.01, 1),
        ]
        self.zai.executemany(
            "INSERT INTO api_calls (ts, key_name, model, prompt_tokens,"
            " completion_tokens, total_tokens, cost_usd, ppq_hit)"
            " VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
        self.zai.commit()

    def tearDown(self):
        self.burn.close()
        self.zai.close()
        self.tmp.cleanup()

    def test_imports_only_ppq_rows(self):
        n = ppq_backfill.backfill_from_proxy_logs(
            self.burn, self.zai, since_ts=time.time() - 86400
        )
        self.assertEqual(n, 3)
        models = self.burn.execute(
            "SELECT DISTINCT model FROM ppq_queries"
        ).fetchall()
        self.assertEqual(sorted(m[0] for m in models), ["glm-5.2", "kimi-k3"])
        qt = self.burn.execute(
            "SELECT DISTINCT query_type FROM ppq_queries"
        ).fetchall()
        self.assertEqual(qt, [("proxy_log",)])

    def test_cost_null_passed_through(self):
        ppq_backfill.backfill_from_proxy_logs(
            self.burn, self.zai, since_ts=time.time() - 86400
        )
        costs = self.burn.execute(
            "SELECT cost_usd FROM ppq_queries WHERE model='glm-5.2'"
        ).fetchall()
        self.assertEqual(costs, [(None,)])

    def test_skips_rows_covered_by_api_history(self):
        # pre-insert an api_history record identical to zai row #1 (ts+1200)
        covered = ppq_common.normalize_api_query(
            {
                "timestamp": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 7200)
                ),
                "model": "kimi-k3",
                "input_count": 1000,
                "output_count": 200,
            }
        )
        ppq_common.record_ppq_queries(self.burn, [covered])
        n = ppq_backfill.backfill_from_proxy_logs(
            self.burn, self.zai, since_ts=time.time() - 86400
        )
        # 3 ppq rows in zai, 1 already covered by near-identical api row
        self.assertEqual(n, 2)


class CollectorIntegrationTests(unittest.TestCase):
    """collect_all must persist query_records into ppq_queries (R6 bug fix)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "burn.db")
        import api_burn_collector

        self.collector = api_burn_collector
        self.orig_db = api_burn_collector.DB_PATH
        api_burn_collector.DB_PATH = self.db
        self.orig_fetchers = dict(api_burn_collector.FETCHERS)

    def tearDown(self):
        self.collector.DB_PATH = self.orig_db
        self.collector.FETCHERS = self.orig_fetchers
        self.tmp.cleanup()

    def test_collect_all_persists_query_records(self):
        qts = time.time() - 60
        rec = {
            "ts": qts,
            "model": "kimi-k3",
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "cost_usd": 0.0001,
            "query_type": "chat",
            "api_key_id": "k1",
        }
        self.collector.FETCHERS = {
            "ppq": lambda: {
                "provider": "ppq",
                "balance_usd": 0.05,
                "total_credits": None,
                "total_usage": 0.0,
                "query_records": [rec],
            }
        }
        self.collector.PROVIDERS = ["ppq"]
        self.collector.collect_all(dry_run=False)
        conn = sqlite3.connect(self.db)
        self.assertEqual(count_pq(conn), 1)
        # run again: dedup, no growth
        self.collector.collect_all(dry_run=False)
        self.assertEqual(count_pq(conn), 1)
        conn.close()


if __name__ == "__main__":
    unittest.main()
