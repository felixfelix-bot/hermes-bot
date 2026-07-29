#!/usr/bin/env python3
"""test_live_router.py — P3.4 regression tests.

P3.4 wires LiveRouter into the PRODUCTION failover path (the request
handler's retry-loop terminal fallback at zai_proxy.py:~2326), which
previously bypassed LiveRouter entirely (841 dual-exhaustion events in
2h all hit the hardcoded ollama->external chain, 0 live events).

These tests target ``zai_proxy._consult_live_router()`` — the unit the
retry-loop terminal fallback now calls (Fix 1) — and the
``routing_live_decisions`` table it logs to (Fix 2). They also pin the
latent tuple-unpack bug: the provider must come back as a STRING (e.g.
"deepinfra"), not a ``(provider, model)`` tuple.

Required coverage (per task body):
  (a) LiveRouter picks a provider when both keys 429-exhausted via the
      retry-loop path.
  (b) Kill-switch OFF disables LiveRouter (0 live events, instant revert).
  (c) LiveRouter exception -> safe fallthrough to the hardcoded chain.

Run: python3 tests/test_live_router.py    (or pytest tests/test_live_router.py)
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Bootstrap import path (zai_proxy lives in ~/.hermes/bot)
sys.path.insert(0, os.path.expanduser("~/.hermes/bot"))

import zai_proxy as z  # noqa: E402


def _temp_db():
    """Isolated in-file SQLite DB + connection for log isolation."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn, path


class ConsultLiveRouterTests(unittest.TestCase):
    """Core Fix 1 + Fix 2 tests for ``zai_proxy._consult_live_router``."""

    def setUp(self):
        # ── Isolate the usage DB so log writes land in a temp file ──
        self._db_conn, self._db_path = _temp_db()
        self._orig_usage_db = z._usage_db
        z._usage_db = lambda: self._db_conn

        # ── Stash real router; install a mock for deterministic behaviour ──
        self._orig_router = z._LIVE_ROUTER
        self._router = MagicMock(name="LiveRouter")
        # select_failover returns ((provider, model), (fallback, fb_model))
        self._router.select_failover.return_value = (
            ("deepinfra", "deepseek-ai/DeepSeek-V4-Pro"),
            ("ppq", "deepseek/deepseek-v4-pro"),
        )
        self._router.last_pace_mults = {"deepinfra": 1.0, "ppq": 1.2}
        z._LIVE_ROUTER = self._router

        # ── Kill switch ON by default (real flag exists in the bot dir) ──
        self._orig_flag = z._LIVE_ROUTING_FLAG
        z._LIVE_ROUTING_FLAG = os.path.expanduser(
            "~/.hermes/bot/.enable_live_routing")

        # ── Deterministic snapshots (values don't matter; select is mocked) ──
        self._orig_sq = z._snapshot_quota
        self._orig_sh = z._snapshot_health
        self._orig_peak = z._is_peak_hour
        z._snapshot_quota = lambda: {"ours": {"used_pct": 100.0}}
        z._snapshot_health = lambda: {"ours": False, "friend": False}
        z._is_peak_hour = lambda: False

        # ── Empty pace windows (no refresh loop running under test) ──
        self._orig_pw = z._pace_windows
        z._pace_windows = {}

    def tearDown(self):
        z._usage_db = self._orig_usage_db
        z._LIVE_ROUTER = self._orig_router
        z._LIVE_ROUTING_FLAG = self._orig_flag
        z._snapshot_quota = self._orig_sq
        z._snapshot_health = self._orig_sh
        z._is_peak_hour = self._orig_peak
        z._pace_windows = self._orig_pw
        self._db_conn.close()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    def _live_decision_count(self) -> int:
        try:
            row = self._db_conn.execute(
                "SELECT COUNT(*) FROM routing_live_decisions").fetchone()
            return row[0] if row else 0
        except sqlite3.Error:
            return 0

    # ── (a) LiveRouter picks a provider on dual-exhaustion ──────────────
    def test_picks_provider_on_dual_exhaustion(self):
        """The retry-loop terminal fallback calls _consult_live_router and
        gets back a real provider to route to (was previously bypassed)."""
        pick, model, fb, fb_model = z._consult_live_router()
        self.assertEqual(pick, "deepinfra")
        self.assertEqual(model, "deepseek-ai/DeepSeek-V4-Pro")
        self.assertEqual(fb, "ppq")
        # select_failover was actually invoked with the snapshot args
        self._router.select_failover.assert_called_once()
        _, kwargs = self._router.select_failover.call_args
        self.assertFalse(kwargs["peak"])  # honoured the patched helper
        self.assertIn("quota_state", kwargs)

    def test_pick_is_string_not_tuple(self):
        """Regression for the latent tuple-unpack bug: the old gate did
        ``_provider, _fallback = select_failover(...)`` then used
        ``_provider`` (a (provider, model) tuple) as the provider string.
        The provider MUST be a bare string here."""
        pick, *_ = z._consult_live_router()
        self.assertIsInstance(pick, str)

    # ── Fix 2: routing_live_decisions table gets a row ──────────────────
    def test_logs_live_decision_row(self):
        """Each live engagement writes a row to routing_live_decisions
        (the new table). Was 0 rows before — table didn't exist."""
        z._consult_live_router()
        self.assertGreaterEqual(self._live_decision_count(), 1)
        row = self._db_conn.execute(
            "SELECT live_provider, live_model, shadow_provider, pace_mults "
            "FROM routing_live_decisions ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "deepinfra")          # live_provider
        self.assertEqual(row[1], "deepseek-ai/DeepSeek-V4-Pro")  # live_model
        self.assertEqual(row[2], "ppq")                # fallback (shadow col)
        # pace_mults stored as JSON text
        self.assertIsNotNone(row[3])
        parsed = json.loads(row[3])
        self.assertIn("deepinfra", parsed)

    def test_no_pick_no_log(self):
        """When LiveRouter finds no viable provider, nothing is logged."""
        self._router.select_failover.return_value = ((None, None), (None, None))
        before = self._live_decision_count()
        pick, *_ = z._consult_live_router()
        self.assertIsNone(pick)
        self.assertEqual(self._live_decision_count(), before)

    # ── (b) Kill switch OFF disables LiveRouter ─────────────────────────
    def test_kill_switch_off_disables(self):
        """Flag absent -> no consultation, no logging, instant revert to
        the hardcoded chain. Acceptance criterion 3."""
        z._LIVE_ROUTING_FLAG = "/nonexistent/kill-switch-flag-P3.4-test"
        before = self._live_decision_count()
        pick, model, fb, fb_model = z._consult_live_router()
        self.assertIsNone(pick)
        self.assertIsNone(model)
        self.assertEqual(self._live_decision_count(), before)
        # select_failover must NOT be called when the flag is absent
        self._router.select_failover.assert_not_called()

    # ── (c) Exception -> safe fallthrough ───────────────────────────────
    def test_exception_safe_fallthrough(self):
        """Any LiveRouter failure degrades to (None,...) — the caller then
        falls through to the hardcoded ollama->external chain. LiveRouter
        failures must NEVER break routing. Acceptance criterion 4."""
        self._router.select_failover.side_effect = RuntimeError("boom")
        before = self._live_decision_count()
        pick, model, fb, fb_model = z._consult_live_router()
        self.assertIsNone(pick)
        self.assertIsNone(model)
        self.assertEqual(self._live_decision_count(), before)

    def test_router_none_disables(self):
        """_LIVE_ROUTER is None (import failed) -> safe no-op."""
        z._LIVE_ROUTER = None
        pick, *_ = z._consult_live_router()
        self.assertIsNone(pick)


class ExternalFailoverPreferredTests(unittest.TestCase):
    """Fix 1 enhancement: _try_external_failover(preferred=...) tries the
    LiveRouter-chosen provider FIRST so its pick is actually honoured
    (not silently overridden by the cost-sorted chain)."""

    def setUp(self):
        # Patch the module helpers _try_external_failover depends on so the
        # only variable is candidate ordering.
        self._patches = []
        # All three externals funded + keyed
        self._patches.append(patch.dict(z.EXTERNAL_PROVIDERS, {
            "deepinfra": {"base_url": "https://di/v1", "key": "k1"},
            "ppq": {"base_url": "https://ppq/v1", "key": "k2"},
            "openrouter": {"base_url": "https://or/v1", "key": "k3"},
        }, clear=True))
        self._patches.append(
            patch.object(z, "_is_provider_funded", return_value=True))
        # Give openrouter the HIGHEST cost so without `preferred` it would
        # be tried LAST — proving preferred reorders it to FIRST.
        cost_map = {"openrouter": 0.9, "ppq": 0.5, "deepinfra": 0.4}
        self._patches.append(
            patch.object(z, "_get_provider_cost",
                         side_effect=lambda name, model: cost_map.get(name, 1.0)))
        for p in self._patches:
            p.start()
        self.addCleanup(self._stop_patches)

    def _stop_patches(self):
        for p in reversed(self._patches):
            try:
                p.stop()
            except RuntimeError:
                pass

    def test_preferred_provider_tried_first(self):
        """With preferred='openrouter' (highest cost), openrouter is
        attempted before the cheaper providers."""
        attempted = []

        class _Resp:
            status = 200
            headers = {}  # dict supports .items() (the success path iterates it)

            def read(self, n=-1):
                return b""   # empty body ends the read loop immediately

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            # Capture which provider the request targets via the auth bearer.
            attempted.append(req.headers.get("Authorization", ""))
            # Succeed on the first attempt so the loop stops immediately.
            return _Resp()

        handler = _StubHandler()
        with patch.object(z.urllib.request, "urlopen", side_effect=fake_urlopen), \
             patch.object(z, "_record_spend"), \
             patch.object(z, "_deduct_deepinfra_balance", return_value=1.0), \
             patch.object(z, "_log_api_call"), \
             patch.object(z, "_parse_usage", return_value={}):
            ok = z.Handler is not None  # ensure Handler exists
            # Call the unbound method on a stub instance.
            ok = z.Handler._try_external_failover(
                handler, body=b'{"model":"x"}', model="glm-5.2",
                response_buffer=bytearray(), t0=0.0, preferred="openrouter")
        self.assertTrue(ok)
        # The first attempt's bearer must be openrouter's key (k3).
        self.assertTrue(attempted, "no provider was attempted")
        self.assertEqual(attempted[0], "Bearer k3",
                         f"preferred openrouter not first; order={attempted}")


class _StubHandler:
    """Minimal stand-in for the Handler instance methods/attrs that
    _try_external_failover touches on ``self``."""
    def __init__(self):
        self._spend_recorded = False
        self.wfile = io.BytesIO()
        self._sent_status = None
        self._headers = {}

    def send_response(self, code):
        self._sent_status = code

    def send_header(self, k, v):
        self._headers[k] = v

    def end_headers(self):
        pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
