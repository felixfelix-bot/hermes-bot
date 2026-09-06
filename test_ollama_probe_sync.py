#!/usr/bin/env python3
"""Tests for t_30dde4c7 — /quota ↔ ollama key-state reconciliation.

Validates the server-truth override in _get_ollama_quota_status and the
stale-backoff recovery (_recover_ollama_stale_backoffs) that lets weekly/
monthly pool resets recover WITHOUT a proxy restart.

No live calls: fetch_ollama_usage + _get_quota_status are mocked via the
src.ollama_extra_usage module reference (the inline `from ... import` in
zai_proxy resolves it at call time).

Run from the WORKTREE (so `import zai_proxy` resolves to the worktree copy):
    cd ~/worktrees/t_30dde4c7 && python3 -m pytest test_ollama_probe_sync.py -v
"""

import importlib.util
import sys
import time
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Pin to the WORKTREE copy of zai_proxy.py (NOT ~/.hermes/bot or the stale
# merchant-routing-engine production copy). This mirrors the
# test_flat_router.py anti-trap: pytest may have already registered `zai_proxy`
# in sys.modules (from a prior test module) pointing at the LIVE file, so a
# bare `import zai_proxy` would test stale code. Load by absolute path.
_WORKTREE = str(Path(__file__).resolve().parent)
if _WORKTREE not in sys.path:
    sys.path.insert(0, _WORKTREE)

_WT_ZAI_PROXY = str(Path(_WORKTREE) / "zai_proxy.py")
_spec = importlib.util.spec_from_file_location("zai_proxy_wt", _WT_ZAI_PROXY)
_zp_mod = importlib.util.module_from_spec(_spec)
sys.modules["zai_proxy_wt"] = _zp_mod
_spec.loader.exec_module(_zp_mod)


class OllamaProbeSyncFixture(unittest.TestCase):
    def setUp(self):
        import importlib
        import src.ollama_extra_usage as oeu
        self.zp = _zp_mod
        self.oeu = oeu
        mock.patch.object(self.zp, "_OLLAMA_EXTRA_USAGE_ENABLED", True).start()
        # Ensure key vars are non-empty so the server-truth path is taken.
        self.zp.OLLAMA_CLOUD_KEY = "k1"
        self.zp.OLLAMA_CLOUD_KEY_2 = "k2"
        self.zp.OLLAMA_CLOUD_KEY_3 = "k3"
        self.zp.OLLAMA_CLOUD_KEY_4 = "k4"
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        conn = sqlite3.connect(self._tmp.name, isolation_level=None,
                               check_same_thread=False)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS api_calls ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "ts REAL, key_name TEXT, model TEXT, "
            "prompt_tokens INTEGER, completion_tokens INTEGER,"
            "total_tokens INTEGER, status_code INTEGER, tier TEXT,"
            "error TEXT, session_id TEXT, task_type TEXT,"
            "cost_usd REAL, cost_source TEXT, duration_ms INTEGER)")
        mock.patch.object(self.zp, "_usage_db", return_value=conn).start()
        # Clear any stale per-key quota cache between tests.
        self.zp._ollama_quota_cache.clear()
        self.zp._ollama_quota_cache_ts.clear()

    def tearDown(self):
        mock.patch.stopall()

    def _make_usage(self, fracs):
        lim = {}
        for win, frac in fracs.items():
            lim[win] = {"usage": frac, "models": []}
        return {"limits": lim}

    def test_server_truth_overrides_local_counter(self):
        # Local tracker reports 0% (frozen because key stopped serving);
        # server /api/usage says weekly=100%. /quota must reflect server truth.
        local = {"regime": "included", "session_used_pct": 0.0,
                 "weekly_used_pct": 0.0, "monthly_used_pct": 0.0,
                 "session_tokens": 0, "weekly_tokens": 0, "monthly_tokens": 0,
                 "monthly_limit": 500_000_00}
        with mock.patch.object(self.zp, "_get_quota_status",
                               return_value=dict(local)), \
             mock.patch.object(self.oeu, "fetch_ollama_usage",
                               return_value=self._make_usage(
                                   {"session": 0, "weekly": 1.0})):
            status = self.zp._get_ollama_quota_status("ollama_cloud")
        self.assertEqual(status["weekly_used_pct"], 100.0)
        self.assertTrue(status["probe_exhausted"])
        self.assertEqual(status["regime"], "exhausted")
        self.assertEqual(status["server_used_pct"], 100.0)
        self.assertIsNotNone(status["probe_ts"])

    def test_fresh_key_is_not_exhausted_and_regime_included(self):
        with mock.patch.object(self.zp, "_get_quota_status",
                               return_value={"regime": "included",
                                             "session_used_pct": 0.0,
                                             "weekly_used_pct": 0.0,
                                             "monthly_used_pct": 0.0,
                                             "session_tokens": 0,
                                             "weekly_tokens": 0,
                                             "monthly_tokens": 0,
                                             "monthly_limit": 500_000_00}), \
             mock.patch.object(self.oeu, "fetch_ollama_usage",
                               return_value=self._make_usage(
                                   {"monthly": 0.002})):
            status = self.zp._get_ollama_quota_status("ollama_cloud_4")
        self.assertAlmostEqual(status["monthly_used_pct"], 0.2, places=1)
        self.assertFalse(status["probe_exhausted"])
        self.assertEqual(status["regime"], "included")

    def test_recovery_heals_when_pool_resets(self):
        # Key benched by exhaustion backoff; server now says pool reset to <100.
        self.zp._zai_key_health["ollama_cloud_2"] = {
            "healthy": False, "last_error_type": "exhausted",
            "retry_after": time.time() + 3600, "consecutive_failures": 5,
        }
        with mock.patch.object(self.zp, "_get_quota_status",
                               return_value={"regime": "included",
                                             "session_used_pct": 0.0,
                                             "weekly_used_pct": 0.0,
                                             "monthly_used_pct": 0.0,
                                             "session_tokens": 0,
                                             "weekly_tokens": 0,
                                             "monthly_tokens": 0,
                                             "monthly_limit": 500_000_00}), \
             mock.patch.object(self.oeu, "fetch_ollama_usage",
                               return_value=self._make_usage(
                                   {"session": 0.0, "weekly": 0.1})):
            self.zp._recover_ollama_stale_backoffs()
        h = self.zp._zai_key_health.get("ollama_cloud_2", {})
        self.assertTrue(h.get("healthy", False))

    def test_recovery_does_not_heal_still_exhausted(self):
        # Server still 100% → backoff must stay (no retry storm on a dead pool).
        self.zp._zai_key_health["ollama_cloud_3"] = {
            "healthy": False, "last_error_type": "exhausted",
            "retry_after": time.time() + 3600, "consecutive_failures": 5,
        }
        with mock.patch.object(self.zp, "_get_quota_status",
                               return_value={"regime": "included",
                                             "session_used_pct": 0.0,
                                             "weekly_used_pct": 0.0,
                                             "monthly_used_pct": 0.0,
                                             "session_tokens": 0,
                                             "weekly_tokens": 0,
                                             "monthly_tokens": 0,
                                             "monthly_limit": 3_500_000_000}), \
             mock.patch.object(self.oeu, "fetch_ollama_usage",
                               return_value=self._make_usage({"monthly": 1.0})):
            self.zp._recover_ollama_stale_backoffs()
        h = self.zp._zai_key_health.get("ollama_cloud_3", {})
        self.assertFalse(h.get("healthy", True))


if __name__ == "__main__":
    unittest.main()
