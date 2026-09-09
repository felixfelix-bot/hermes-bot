#!/usr/bin/env python3
"""test_cached_tokens_instrumentation.py — TDD tests for cached_tokens split.

Cost-audit gap (task T4): provider prompt caches drive a big cost delta
(DeepSeek discounts cached prefixes ~10x, NW charges real prefill compute),
but zai_usage.db had NO cached-token split: the `api_calls` table lacked a
`cached_tokens` column, and the primary z.ai response path never persisted
how many prompt tokens were served from cache.

Fix:
  1. A pure `_extract_cache_read_tokens(usage)` helper that pulls the cached
     prompt-token count from OpenAI-compatible usage:
         - usage.cache_read_input_tokens           (Anthropic/z.ai style)
         - usage.prompt_tokens_details.cached_tokens  (OpenAI standard)
       returns 0 when absent.
  2. `_log_api_call(cached_tokens=...)` threads it into the insert (with the
     full guarded fallback chain).
  3. Backwards-safe migration: guarded `ALTER TABLE api_calls ADD COLUMN
     cached_tokens INTEGER DEFAULT 0` — never rewrites existing rows.

Run:  python3 -m pytest tests/test_cached_tokens_instrumentation.py -v
  or: python3 tests/test_cached_tokens_instrumentation.py
"""
from __future__ import annotations

import os
import sys
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.expanduser("~/.hermes/bot"))

import zai_proxy as z  # noqa: E402


class TestExtractCacheReadTokens(unittest.TestCase):
    """TDD RED phase: _extract_cache_read_tokens must resolve both shapes."""

    def test_openai_prompt_tokens_details_cached(self):
        """OpenAI-standard usage.prompt_tokens_details.cached_tokens."""
        usage = {
            "prompt_tokens": 1000,
            "completion_tokens": 50,
            "total_tokens": 1050,
            "prompt_tokens_details": {"cached_tokens": 900},
        }
        self.assertEqual(z._extract_cache_read_tokens(usage), 900)

    def test_top_level_cache_read_input_tokens(self):
        """z.ai / Anthropic-style usage.cache_read_input_tokens."""
        usage = {
            "cache_read_input_tokens": 512,
            "prompt_tokens": 1000,
        }
        self.assertEqual(z._extract_cache_read_tokens(usage), 512)

    def test_cache_read_input_tokens_wins_over_details(self):
        """If both present, prefer the top-level cache_read_input_tokens
        (the source of truth on z.ai/Anthropic-compatible responses)."""
        usage = {
            "cache_read_input_tokens": 700,
            "prompt_tokens_details": {"cached_tokens": 999},
        }
        self.assertEqual(z._extract_cache_read_tokens(usage), 700)

    def test_absent_returns_zero(self):
        """No cached-token field → 0, never None, never raises."""
        for usage in ({}, {"prompt_tokens": 5}, None, "not-a-dict"):
            self.assertEqual(z._extract_cache_read_tokens(usage), 0,
                             f"usage={usage!r} should yield 0")

    def test_zero_cached_returns_zero(self):
        """Explicit 0 cached tokens → 0."""
        usage = {"prompt_tokens_details": {"cached_tokens": 0}}
        self.assertEqual(z._extract_cache_read_tokens(usage), 0)

    def test_streaming_usage_roundtrip(self):
        """The extraction works on usage objects parsed from a streamed SSE
        final chunk (what _parse_usage returns)."""
        import json
        sse = (
            b'data: {"id":"1","choices":[]}\n'
            b'data: {"id":"1","choices":[{"delta":{},"finish_reason":"stop"}],'
            b'"usage":{"prompt_tokens":100,"completion_tokens":20,'
            b'"total_tokens":120,"prompt_tokens_details":{"cached_tokens":80}}}\n'
            b'data: [DONE]\n'
        )
        usage = z._parse_usage(sse)
        self.assertEqual(z._extract_cache_read_tokens(usage), 80)


class TestCachedTokensInApiCallsColumn(unittest.TestCase):
    """The api_calls table must expose a cached_tokens column (idempotent
    migration, backwards-safe) and _log_api_call must write it."""

    def setUp(self):
        self._orig_db_path = z.USAGE_DB
        self._tmpdir = tempfile.mkdtemp(prefix="zai_usage_test_")
        self._db = os.path.join(self._tmpdir, "zai_usage.db")
        # Point the proxy at a scratch DB and force a fresh connection.
        patch.object(z, "USAGE_DB", self._db).start()
        # Reset the cached connection so the next _usage_db() opens our temp DB.
        z._usage_db_conn = None
        self.addCleanup(patch.stopall)

    def _fresh_column_is_present(self):
        """Open the schema, apply the migration helper, return the column names."""
        conn = sqlite3.connect(self._db)
        try:
            z._ensure_api_calls_cached_tokens(conn)
            cols = [r[1] for r in conn.execute("PRAGMA table_info(api_calls)")]
            return cols
        finally:
            conn.close()

    def test_migration_adds_column_to_legacy_db(self):
        """A legacy api_calls table without the column gets it via a guarded
        ALTER — existing rows untouched."""
        conn = sqlite3.connect(self._db)
        try:
            conn.execute("""CREATE TABLE api_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                key_name TEXT,
                model TEXT,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                total_tokens INTEGER,
                tier TEXT,
                cache_hit INTEGER DEFAULT 0)
            """)
            conn.execute("INSERT INTO api_calls (ts, key_name, prompt_tokens) "
                         "VALUES (1.0, 'ours', 10)")
            conn.commit()
        finally:
            conn.close()

        cols = self._fresh_column_is_present()
        self.assertIn("cached_tokens", cols,
                      "migration must add cached_tokens to a legacy DB")

        # Existing row must be unmodified and default to 0.
        conn = sqlite3.connect(self._db)
        try:
            row = conn.execute(
                "SELECT prompt_tokens, cached_tokens FROM api_calls").fetchone()
            self.assertEqual(row[0], 10, "existing prompt_tokens must be untouched")
            self.assertEqual(row[1], 0, "existing row default cached_tokens = 0")
        finally:
            conn.close()

    def test_migration_idempotent(self):
        """Running the migration twice must not raise."""
        conn = sqlite3.connect(self._db)
        try:
            conn.execute("""CREATE TABLE api_calls (ts REAL NOT NULL)""")
        finally:
            conn.close()
        self._fresh_column_is_present()  # first run
        self._fresh_column_is_present()  # second run — must not raise

    def test_log_api_call_writes_cached_tokens(self):
        """_log_api_call(cached_tokens=...) persists the value through the
        primary SQL statement."""
        db = MagicMock()
        cur = MagicMock()
        db.execute.return_value = cur
        with patch.object(z, "_usage_db", return_value=db):
            z._log_api_call(key_name="ours", model="glm-5.2",
                            prompt_tokens=100, completion_tokens=20,
                            total_tokens=120, cached_tokens=90)
        sql, params = db.execute.call_args[0]
        self.assertIn("cached_tokens", sql,
                      "INSERT must reference the cached_tokens column")
        self.assertIn(90, params, "cached_tokens value must be in the INSERT params")

    def test_log_api_call_defaults_zero(self):
        """Omitting cached_tokens must default to 0, not break the insert."""
        db = MagicMock()
        cur = MagicMock()
        db.execute.return_value = cur
        with patch.object(z, "_usage_db", return_value=db):
            z._log_api_call(key_name="ours", model="glm-5.2", total_tokens=5)
        sql, params = db.execute.call_args[0]
        self.assertIn("cached_tokens", sql)
        # Find the cached_tokens position in the params tuple.
        self.assertIn(0, params, "default cached_tokens must be 0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
