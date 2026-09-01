#!/usr/bin/env python3
"""test_deploy_import_shadow.py — Gate 1: clean import + shadow tap logging.

Verifies that the deployed merchant-routing-engine src/:
  1. All modules import without errors in the proxy context.
  2. ShadowHook is initialized and recording to zai_usage.db.

Run: python3 -m pytest tests/test_deploy_import_shadow.py -v
"""
from __future__ import annotations

import os
import sqlite3
import sys
import unittest

# Bootstrap import path (zai_proxy lives in ~/.hermes/bot)
sys.path.insert(0, os.path.expanduser("~/.hermes/bot"))

import zai_proxy as z  # noqa: E402


class CleanImportTests(unittest.TestCase):
    """Gate 1a: every deployed src/ module imports without error."""

    def test_zai_proxy_imports(self):
        """The proxy loads without import errors."""
        self.assertIsNotNone(z)
        self.assertTrue(hasattr(z, "_LIVE_ROUTER"), "LiveRouter not loaded")
        self.assertTrue(hasattr(z, "_shadow_hook"), "ShadowHook not loaded")
        self.assertTrue(hasattr(z, "_shadow_optimizer"), "shadow optimizer not loaded")

    def test_live_router_import(self):
        """LiveRouter is a non-None instance (initialized at import time)."""
        self.assertIsNotNone(z._LIVE_ROUTER, "LiveRouter is None — init failed")

    def test_shadow_hook_import(self):
        """ShadowHook is a non-None instance (initialized at import time)."""
        self.assertIsNotNone(z._shadow_hook, "ShadowHook is None — init failed")

    def test_shadow_optimizer_import(self):
        """Shadow optimizer is a non-None instance (initialized at import time)."""
        self.assertIsNotNone(z._shadow_optimizer, "shadow optimizer is None — init failed")

    def test_enable_live_routing_present(self):
        """.enable_live_routing must be PRESENT (Phase 3: flat router primary).

        The Phase-3 cutover (2026-08-24) made the flat router the primary
        routing system, and zai-proxy.service touches this flag in
        ExecStartPost. The original Phase-1 test asserted absence
        (shadow-only); that invariant was inverted at cutover and this test
        had been failing stale ever since.
        """
        flag = z._LIVE_ROUTING_FLAG
        self.assertTrue(
            os.path.exists(flag),
            f"Kill switch {flag} missing — live routing disabled?",
        )


class ShadowTapLoggingTests(unittest.TestCase):
    """Gate 1b: ShadowHook records decisions to zai_usage.db."""

    def _db_path(self) -> str:
        """Resolve the DB path — matches zai_proxy's _ZAI_USAGE_DB."""
        return os.path.expanduser("~/.hermes/bot/zai_usage.db")

    def test_shadow_db_exists(self):
        """zai_usage.db exists (ShadowHook is logging)."""
        db = self._db_path()
        self.assertTrue(os.path.exists(db), f"Shadow DB not found at {db}")

    def test_shadow_decisions_table(self):
        """routing_shadow_decisions table exists with expected schema."""
        db = self._db_path()
        conn = sqlite3.connect(db)
        try:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='routing_shadow_decisions'"
            )
            row = cur.fetchone()
            self.assertIsNotNone(row, "routing_shadow_decisions table missing")
        finally:
            conn.close()

    def test_shadow_has_decision_rows(self):
        """At least one shadow decision row exists (logging is active)."""
        db = self._db_path()
        conn = sqlite3.connect(db)
        try:
            cur = conn.execute("SELECT COUNT(*) FROM routing_shadow_decisions")
            count = cur.fetchone()[0]
            self.assertGreater(count, 0, "No shadow decisions recorded — tap may be dead")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
