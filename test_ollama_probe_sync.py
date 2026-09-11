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
import os
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
#
# Deployment check: set ZAI_PROXY_TARGET=/home/c03rad0r/.hermes/bot/zai_proxy.py
# to run this same suite against the ARTIFACT THAT IS ACTUALLY RUNNING (run it
# from ~/.hermes/bot so the live module's relative imports resolve):
#     cd ~/.hermes/bot && ZAI_PROXY_TARGET=~/.hermes/bot/zai_proxy.py \
#         pytest ~/worktrees/t_30dde4c7/test_ollama_probe_sync.py -q
_WORKTREE = str(Path(__file__).resolve().parent)
if _WORKTREE not in sys.path:
    sys.path.insert(0, _WORKTREE)

_WT_ZAI_PROXY = os.environ.get("ZAI_PROXY_TARGET") or str(
    Path(_WORKTREE) / "zai_proxy.py")
_spec = importlib.util.spec_from_file_location("zai_proxy_wt", _WT_ZAI_PROXY)
_zp_mod = importlib.util.module_from_spec(_spec)
sys.modules["zai_proxy_wt"] = _zp_mod
_spec.loader.exec_module(_zp_mod)


class ExtraUsageModuleImportTest(unittest.TestCase):
    """Regression for t_30dde4c7 production breakage.

    _get_ollama_quota_status and _recover_ollama_stale_backoffs both do
    `from src.ollama_extra_usage import fetch_ollama_usage` inside a
    try/except that SWALLOWS import errors. If that module cannot be imported
    (dataclass field-order bug → TypeError), the server-truth override and
    pool-reset recovery silently become dead code and /quota falls back to the
    frozen local-token "included/0%" view — exactly the stale-health livelock
    this task exists to remove.

    This test asserts the module imports cleanly so a future field-ordering
    regression fails loudly instead of silently degrading to the fallback.
    """
    def test_ollama_extra_usage_imports_cleanly(self):
        import importlib
        try:
            m = importlib.import_module("src.ollama_extra_usage")
        except Exception as e:  # pragma: no cover — surfaces the real traceback
            self.fail(f"src.ollama_extra_usage failed to import: "
                      f"{type(e).__name__}: {e}")
        self.assertTrue(hasattr(m, "fetch_ollama_usage"))
        self.assertTrue(hasattr(m, "ExtraUsageStatus"))

    def test_extra_usage_status_constructs_ok(self):
        import importlib
        m = importlib.import_module("src.ollama_extra_usage")
        # All construction sites in the module pass keyword args, so the field
        # ORDER must keep non-default fields before defaulted ones. Exercise a
        # full construction to prove no TypeError on instantiation.
        s = m.ExtraUsageStatus(
            session_usage=0.5, weekly_usage=1.0,
            session_tokens=10, weekly_tokens=20,
            extra_usage=True, reason="test")
        d = s.to_dict()
        self.assertEqual(d["session_usage"], 0.5)
        self.assertEqual(d["weekly_usage"], 1.0)
        self.assertTrue(d["extra_usage"])


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


class OllamaPaywallDisarmTest(OllamaProbeSyncFixture):
    """Prong (b) completion: the persisted 403-paywall flag must not outlive a
    server-proven pool reset.

    The 403 handler arms `.ollama_exhausted_until[_N]` until a next-Monday
    00:00 UTC horizon, but the real ollama weekly pool is a ROLLING 7-day
    window and ollama_cloud_3's pool is MONTHLY — so a flagged key stayed
    benched for days after the pool had actually reset. Clearing the in-memory
    backoff alone is not enough: `_is_key_healthy()` consults the paywall flag
    FIRST, before it ever looks at the health dict.
    """

    def setUp(self):
        super().setUp()
        self._flagdir = Path(tempfile.mkdtemp())
        mock.patch.object(self.zp, "_OLLAMA_PAYWALL_FLAGS",
                          self._flags()).start()
        self.zp._ollama_paywall_disarms.clear()

    def _flags(self):
        d = self._flagdir
        return {
            "ollama_cloud": d / ".ollama_exhausted_until",
            "ollama_cloud_2": d / ".ollama_exhausted_until_2",
            "ollama_cloud_3": d / ".ollama_exhausted_until_3",
            "ollama_cloud_4": d / ".ollama_exhausted_until_4",
        }

    def _arm(self, key):
        """Simulate the 403 handler arming the persisted paywall flag."""
        f = self._flags()[key]
        f.write_text(str(time.time() + 48 * 3600))
        return f

    def _local_zero(self, monthly_limit=500_000_00):
        return {"regime": "included", "session_used_pct": 0.0,
                "weekly_used_pct": 0.0, "monthly_used_pct": 0.0,
                "session_tokens": 0, "weekly_tokens": 0, "monthly_tokens": 0,
                "monthly_limit": monthly_limit}

    def _recover_with_probe(self, fracs):
        with mock.patch.object(self.zp, "_get_quota_status",
                               return_value=self._local_zero()), \
             mock.patch.object(self.oeu, "fetch_ollama_usage",
                               return_value=(None if fracs is None
                                             else self._make_usage(fracs))):
            self.zp._recover_ollama_stale_backoffs()

    def test_flag_disarmed_when_probe_proves_reset(self):
        # Key is paywalled (flag armed) but the server pool has reset.
        f = self._arm("ollama_cloud_2")
        # Also benched in memory (the 403 handler marks exhausted too).
        self.zp._zai_key_health["ollama_cloud_2"] = {
            "healthy": False, "last_error_type": "exhausted",
            "retry_after": time.time() + 3600, "consecutive_failures": 3}
        self._recover_with_probe({"session": 0.0, "weekly": 0.1})
        self.assertFalse(f.exists(), "paywall flag must be disarmed on reset")
        h = self.zp._zai_key_health.get("ollama_cloud_2", {})
        self.assertTrue(h.get("healthy", False),
                        "in-memory exhaustion backoff must be cleared too")

    def test_flag_kept_while_probe_still_exhausted(self):
        f = self._arm("ollama_cloud_3")
        self._recover_with_probe({"monthly": 1.0})
        self.assertTrue(f.exists(), "still-exhausted pool must keep the flag")
        self.assertFalse(self.zp._ollama_paywall_disarms.get("ollama_cloud_3"),
                         "no disarm may be recorded while the pool is exhausted")

    def test_flag_kept_when_probe_unavailable(self):
        # Server fetch failed → no probe truth → NO action (retry-storm guard).
        f = self._arm("ollama_cloud")
        self._recover_with_probe(None)
        self.assertTrue(f.exists(), "no fresh probe → never disarm")

    def test_flag_kept_at_boundary_usage(self):
        # 99.5% is not probe_exhausted but is NOT a clear reset either — a
        # disarm here would flap against the 403 gate.
        f = self._arm("ollama_cloud_4")
        self._recover_with_probe({"monthly": 0.995})
        self.assertTrue(f.exists(), "boundary usage must not trigger a disarm")

    def test_disarm_cap_prevents_flap(self):
        # A lapsed-plan 403 re-arms on every dispatch; the cap must stop the
        # probe loop from flapping forever (bounded wasted requests).
        key = "ollama_cloud"
        for _ in range(3):
            f = self._arm(key)
            self._recover_with_probe({"weekly": 0.2})
            self.assertFalse(f.exists(), "first 3 disarms are allowed")
        f = self._arm(key)          # 4th re-arm within the 24h window
        self._recover_with_probe({"weekly": 0.2})
        self.assertTrue(f.exists(), "4th disarm must be refused by the cap")
        self.assertEqual(len(self.zp._ollama_paywall_disarms[key]), 3)

    def test_expired_flags_do_not_count_as_benched(self):
        # `_ollama_paywall_active` is horizon-based; an expired flag leaves the
        # key routable and the recovery loop must not touch it.
        f = self._flags()["ollama_cloud"]
        f.write_text(str(time.time() - 60))
        self._recover_with_probe({"weekly": 0.0})
        self.assertTrue(f.exists())   # untouched (not armed → not our business)
        self.assertFalse(self.zp._ollama_paywall_active("ollama_cloud"))


if __name__ == "__main__":
    unittest.main()
