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

    def test_do_GET_pressure_limit_param_is_passed_and_clamped(self):
        """Kimi review 2: ?limit= must reach the snapshot, clamped to [1,100]."""
        for q, expected in [("?limit=5", 5), ("?limit=9999", 100),
                            ("?limit=0", 20), ("?limit=abc", 20),
                            ("?other=x", 20)]:
            h = MagicMock()
            h.path = "/pressure" + q
            h.do_GET = z.Handler.do_GET.__get__(h, z.Handler)
            h._pressure_tracker_snapshot = MagicMock(return_value={})
            h.do_GET()
            h._pressure_tracker_snapshot.assert_called_once_with(
                limit=expected)


class TestEnforceHook(unittest.TestCase):
    """S2c (t_b82e5665): enforce-mode application of the FSM decision.

    Handler._pressure_enforce(decision, body, t0) must route ONLY
    enforce-mode Ollama-downgrade decisions to ollama_cloud; everything
    else (shadow mode, off mode, friend-path / interactive / last-resort
    reasons, Ollama failure, dead tracker) falls through unharmed.
    """

    def _handler(self, ollama_ok=True):
        h = MagicMock()
        h._pressure_enforce = z.Handler._pressure_enforce.__get__(h, z.Handler)
        h._try_ollama_cloud = MagicMock(return_value=ollama_ok)
        return h

    @staticmethod
    def _decision(reason, state="RED", interactive=False,
                  serve: str | None = "glm-5.2", provider="ollama_cloud"):
        return pf.Decision(
            requested_model="glm-5.3", would_serve_model=serve,
            would_provider=provider, state=state,
            interactive=interactive, reason=reason)

    @staticmethod
    def _tracker(mode):
        t = MagicMock()
        t.mode.return_value = mode
        return t

    def _with_tracker(self, tracker):
        orig = z._pressure_tracker
        z._pressure_tracker = tracker
        self.addCleanup(lambda: setattr(z, "_pressure_tracker", orig))

    def test_shadow_mode_does_not_enforce(self):
        self._with_tracker(self._tracker("shadow"))
        h = self._handler()
        out = h._pressure_enforce(
            self._decision("bg_downgraded_ollama"), b"{}", 0.0)
        self.assertFalse(out)
        h._try_ollama_cloud.assert_not_called()

    def test_off_mode_does_not_enforce(self):
        self._with_tracker(self._tracker("off"))
        h = self._handler()
        out = h._pressure_enforce(
            self._decision("bg_downgraded_ollama"), b"{}", 0.0)
        self.assertFalse(out)
        h._try_ollama_cloud.assert_not_called()

    def test_enforce_routes_ollama_downgrade(self):
        self._with_tracker(self._tracker("enforce"))
        h = self._handler()
        out = h._pressure_enforce(
            self._decision("bg_downgraded_ollama", state="RED"), b"{}", 0.0)
        self.assertTrue(out)
        h._try_ollama_cloud.assert_called_once()
        args, kwargs = h._try_ollama_cloud.call_args
        self.assertEqual(args[0], b"{}")            # untouched body
        self.assertEqual(args[1], "glm-5.2")        # downgraded model
        self.assertEqual(kwargs.get("reason"), "pressure_enforce_red")

    def test_enforce_routes_ollama_extra_reason(self):
        self._with_tracker(self._tracker("enforce"))
        h = self._handler()
        out = h._pressure_enforce(
            self._decision("bg_downgraded_ollama_extra", state="AMBER"),
            b"{}", 0.0)
        self.assertTrue(out)
        self.assertEqual(
            h._try_ollama_cloud.call_args.kwargs.get("reason"),
            "pressure_enforce_amber")

    def test_enforce_ignores_interactive_and_friend_paths(self):
        self._with_tracker(self._tracker("enforce"))
        for reason in ("interactive_rationed", "interactive_kept",
                       "bg_kept", "bg_quota_neutral", "bg_last_resort",
                       "not_glm_53_passthrough"):
            h = self._handler()
            out = h._pressure_enforce(self._decision(reason), b"{}", 0.0)
            self.assertFalse(out, reason)
            h._try_ollama_cloud.assert_not_called()

    def test_enforce_falls_through_when_ollama_fails(self):
        self._with_tracker(self._tracker("enforce"))
        h = self._handler(ollama_ok=False)
        out = h._pressure_enforce(
            self._decision("bg_downgraded_ollama"), b"{}", 0.0)
        self.assertFalse(out)  # caller continues down the normal cascade
        h._try_ollama_cloud.assert_called_once()

    def test_enforce_none_decision_is_noop(self):
        self._with_tracker(self._tracker("enforce"))
        h = self._handler()
        self.assertFalse(h._pressure_enforce(None, b"{}", 0.0))
        h._try_ollama_cloud.assert_not_called()

    def test_enforce_never_raises_dead_tracker(self):
        t = MagicMock()
        t.mode.side_effect = RuntimeError("boom")
        self._with_tracker(t)
        h = self._handler()
        self.assertFalse(h._pressure_enforce(
            self._decision("bg_downgraded_ollama"), b"{}", 0.0))
        h._try_ollama_cloud.assert_not_called()

    def test_enforce_defaults_missing_serve_model(self):
        self._with_tracker(self._tracker("enforce"))
        h = self._handler()
        out = h._pressure_enforce(
            self._decision("bg_downgraded_ollama", serve=None), b"{}", 0.0)
        self.assertTrue(out)
        self.assertEqual(h._try_ollama_cloud.call_args.args[1], "glm-5.2")


class TestEnforceWiring(unittest.TestCase):
    """_proxy must call the enforce hook AFTER the spend cap and BEFORE
    the Ollama-only short-circuit; _try_ollama_cloud keeps back-compat
    default reasons when called without reason=."""

    def test_proxy_calls_enforce_hook_in_order(self):
        import inspect
        src = inspect.getsource(z.Handler._proxy)
        i_shadow = src.index("_pressure_shadow(")
        i_cap = src.index("_check_global_spend_cap()")
        i_enforce = src.index("self._pressure_enforce(")
        i_ollama_only = src.index("_OLLAMA_ONLY_MODELS")
        self.assertLess(i_shadow, i_enforce)
        self.assertLess(i_cap, i_enforce)
        self.assertLess(i_enforce, i_ollama_only)

    def test_try_ollama_cloud_reason_param_defaults_to_legacy(self):
        import inspect
        src = inspect.getsource(z.Handler._try_ollama_cloud)
        self.assertIn("reason: str | None = None", src)
        self.assertIn("peak_hour_ollama_primary", src)  # legacy default kept
        self.assertIn("pressure_enforce_", inspect.getsource(
            z.Handler._pressure_enforce))


if __name__ == "__main__":
    unittest.main()
