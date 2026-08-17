#!/usr/bin/env python3
"""test_pressure_wiring.py — zai_proxy integration tests for pressure_fsm (S2b).

Covers the three zai_proxy.py integration points (t_4dfaf0d5):
  1. Import bridge: _pressure_tracker is a PressureTracker (or None-safe).
  2. Shadow hook: _pressure_shadow() helper — never raises, kill-switched,
     returns Decision or None.
  3. Response headers: _pressure_headers() emits X-Served-Model /
     X-Downgrade-Reason ONLY when the silent rewrite actually changed the
     model — rewrite behavior itself must stay untouched (shadow-first).
  4. GET /pressure is routed in do_GET (static check: the branch exists).

Run:  python3 -m pytest tests/test_pressure_wiring.py -v
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.expanduser("~/.hermes/bot"))

import zai_proxy as z   # noqa: E402
import pressure_fsm as pf  # noqa: E402


class TestImportBridge(unittest.TestCase):
    def test_tracker_global_exists(self):
        self.assertTrue(hasattr(z, "_pressure_tracker"))

    def test_tracker_is_pressure_tracker_or_none(self):
        self.assertTrue(z._pressure_tracker is None
                        or isinstance(z._pressure_tracker, pf.PressureTracker))

    def test_shadow_helper_exists(self):
        self.assertTrue(callable(getattr(z, "_pressure_shadow", None)))


class TestShadowHook(unittest.TestCase):
    def test_shadow_returns_decision_when_enabled(self):
        if z._pressure_tracker is None:
            self.skipTest("pressure tracker not loaded in proxy")
        d = z._pressure_shadow("glm-5.3", session_id=None,
                               ollama_regime="included")
        self.assertIsInstance(d, pf.Decision)
        self.assertEqual(d.requested_model, "glm-5.3")

    def test_shadow_never_raises_on_bad_args(self):
        if z._pressure_tracker is None:
            self.skipTest("pressure tracker not loaded in proxy")
        # None/absurd inputs must not propagate
        d = z._pressure_shadow(None, session_id=None)
        self.assertTrue(d is None or isinstance(d, pf.Decision))

    def test_shadow_survives_dead_tracker(self):
        """Tracker object replaced by garbage — helper must still not raise."""
        orig = z._pressure_tracker
        try:
            z._pressure_tracker = MagicMock()
            z._pressure_tracker.shadow_decision.side_effect = RuntimeError("boom")
            d = z._pressure_shadow("glm-5.3", session_id=None)
            self.assertIsNone(d)
        finally:
            z._pressure_tracker = orig


class TestResponseHeaders(unittest.TestCase):
    """_pressure_headers() — pure helper the success path calls."""

    def _handler(self):
        h = MagicMock()
        h._pressure_headers = z.Handler._pressure_headers.__get__(h, z.Handler)
        h._pressure_decision = None
        return h

    def test_no_rewrite_no_headers(self):
        h = self._handler()
        hs = h._pressure_headers("glm-5.3", "glm-5.3", None)
        self.assertEqual(hs, [])

    def test_rewrite_emits_served_model_and_reason(self):
        h = self._handler()
        info = {"tier": "standard", "reason": "kalman_budget_guard"}
        hs = h._pressure_headers("glm-5.3", "glm-5.2", info)
        names = {k for k, _ in hs}
        self.assertEqual(dict(hs).get("X-Served-Model"), "glm-5.2")
        self.assertIn("X-Downgrade-Reason", names)

    def test_rewrite_without_tier_info_uses_generic_reason(self):
        h = self._handler()
        hs = h._pressure_headers("glm-5.3", "glm-5.2", None)
        d = dict(hs)
        self.assertEqual(d.get("X-Served-Model"), "glm-5.2")
        self.assertTrue(d.get("X-Downgrade-Reason"))  # non-empty

    def test_pressure_decision_appends_shadow_note(self):
        h = self._handler()
        h._pressure_decision = pf.Decision(
            requested_model="glm-5.3", would_serve_model="glm-5.2",
            would_provider="ollama_cloud", state="AMBER",
            interactive=False, reason="bg_downgraded_ollama")
        hs = h._pressure_headers("glm-5.3", "glm-5.2", None)
        reason = dict(hs).get("X-Downgrade-Reason", "")
        self.assertIn("shadow:bg_downgraded_ollama", reason)

    def test_headers_never_raise(self):
        h = self._handler()
        h._pressure_decision = object()  # garbage attribute
        # must not raise even with a non-Decison _pressure_decision
        try:
            out = h._pressure_headers("glm-5.3", "glm-5.2", None)
        except Exception as e:
            self.fail(f"_pressure_headers raised: {e}")
        self.assertIsInstance(out, list)


class TestGetPressureEndpoint(unittest.TestCase):
    def test_do_GET_has_pressure_branch(self):
        import inspect
        src = inspect.getsource(z.Handler.do_GET)
        self.assertIn('"/pressure"', src)

    def _get_handler(self, path="/pressure"):
        # Plain MagicMock: wfile/send_response are instance-level attrs the
        # spec= variant can't see (set up by StreamRequestHandler.setup()).
        h = MagicMock()
        h.path = path
        h.do_GET = z.Handler.do_GET.__get__(h, z.Handler)
        return h

    def test_do_GET_pressure_returns_json_payload(self):
        """Drive do_GET for /pressure via a patched tracker."""
        h = self._get_handler()
        snap = {"enabled": True, "mode": "shadow", "state": "GREEN",
                "last_decisions": []}
        h._pressure_tracker_snapshot = MagicMock(return_value=snap)
        h.do_GET()
        h.send_response.assert_called_once_with(200)
        h.send_header.assert_any_call("Content-Type", "application/json")
        written = b"".join(c.args[0] for c in h.wfile.write.call_args_list
                           if c.args)
        import json as _json
        self.assertEqual(_json.loads(written)["state"], "GREEN")

    def test_do_GET_pressure_survives_tracker_error(self):
        h = self._get_handler()
        h._pressure_tracker_snapshot = MagicMock(
            side_effect=RuntimeError("boom"))
        h.do_GET()
        h.send_response.assert_called_once_with(200)
        written = b"".join(c.args[0] for c in h.wfile.write.call_args_list
                           if c.args)
        self.assertIn(b"error", written)


class TestColdReviewFixWiring(unittest.TestCase):
    """Wiring fixes from cold review pass 1: friend_locked passthrough,
    /pressure?query support."""

    def test_pressure_shadow_passes_friend_locked(self):
        """The hook must forward the proxy's lock verdict, not default it."""
        calls = {}

        def fake_shadow(model, session_id=None, ollama_regime=None,
                        friend_locked=False):
            calls["friend_locked"] = friend_locked
            return None

        orig_t, orig_qc, orig_lock = (z._pressure_tracker, None,
                                      z.is_key_locked)
        try:
            z._pressure_tracker = type("T", (), {"shadow_decision":
                                                 staticmethod(fake_shadow)})()
            # (a) empty quota cache -> not locked
            z.quota_cache = {}
            z._pressure_shadow("glm-5.3", None)
            self.assertFalse(calls["friend_locked"])
            # (b) friend locked by a window -> forwarded True
            z.quota_cache = {"friend": ([{"name": "5-hour",
                                          "used_pct": 99}], 0.0)}
            z.is_key_locked = lambda k, w: (True, "5-hour", 99, 60) \
                if k == "friend" else (False, None, 0, 0)
            z._pressure_shadow("glm-5.3", None)
            self.assertTrue(calls["friend_locked"])
        finally:
            z._pressure_tracker = orig_t
            z.is_key_locked = orig_lock

    def test_do_GET_pressure_accepts_query_string(self):
        h = MagicMock()
        h.path = "/pressure?limit=5"
        h.do_GET = z.Handler.do_GET.__get__(h, z.Handler)
        h._pressure_tracker_snapshot = MagicMock(
            return_value={"enabled": True, "state": "GREEN"})
        h.do_GET()
        h.send_response.assert_called_once_with(200)
        h._pressure_tracker_snapshot.assert_called_once()


if __name__ == "__main__":
    unittest.main()
