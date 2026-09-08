"""Tests for P0-3 caller-class gating (ADR-007 Gate 2 / PLAN P0-3).

Covers the sold-vs-internal traffic separation built into
``flat_router.select_provider`` + the proxy's ``X-Priority`` classification:

1. ``sold_gate_retry_after`` — pure decision: sold traffic blocked (429 +
   Retry-After) iff the pool is predicted to exhaust within the safety
   horizon; internal traffic never gated; fail-open when no prediction.
2. ``select_provider(caller_class=...)`` — sold requests carry the gate
   decision on the returned candidate list; internal requests are unaltered.
3. Proxy ``X-Priority`` header classification (internal default, sold opt-in,
   unknown falls back to internal).
"""
import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "src"))

# Pin the REPO zai_proxy + flat_router by explicit path so the deployed
# ~/.hermes/bot layout (which flat_router's bootstrap prefers) cannot shadow
# this worktree's copies — we are testing the code in THIS worktree. Mirrors
# the technique in test_flat_router.py (which pins the live copy for the
# opposite purpose). flat_router is pinned AFTER zai_proxy so its own
# bootstrap never pulls the deployed zai_proxy into sys.modules.
import importlib.util as _ilu
for _mod_name in ("zai_proxy", "flat_router"):
    _mp = _REPO / f"{_mod_name}.py"
    _mspec = _ilu.spec_from_file_location(_mod_name, str(_mp))
    _mmod = _ilu.module_from_spec(_mspec)
    sys.modules[_mod_name] = _mmod
    _mspec.loader.exec_module(_mmod)

import flat_router as fr
from flat_router import (
    select_provider,
    sold_gate_retry_after,
    SOLD_SAFETY_HOURS,
)


# ── Pure gate decision ──────────────────────────────────────────────────────

class TestSoldGateRetryAfter:
    def test_no_predictions_returns_none(self):
        """No predictions → no gate (fail-open: serve the request)."""
        assert sold_gate_retry_after([]) is None

    def test_no_will_exhaust_returns_none(self):
        """Predicted-safe windows never block sold traffic."""
        preds = [{"will_exhaust": False, "exhausts_in_hours": 100.0},
                 {"will_exhaust": False, "exhausts_in_hours": None}]
        assert sold_gate_retry_after(preds) is None

    def test_exhaust_beyond_horizon_returns_none(self):
        """Exhaustion predicted AFTER the safety horizon is not blocking."""
        preds = [{"will_exhaust": True,
                  "exhausts_in_hours": SOLD_SAFETY_HOURS * 10}]
        assert sold_gate_retry_after(preds) is None

    def test_exhaust_exactly_at_horizon_returns_none(self):
        """Exhaustion predicted exactly AT the horizon is not blocking
        (rule is strictly-less-than)."""
        preds = [{"will_exhaust": True, "exhausts_in_hours": SOLD_SAFETY_HOURS}]
        assert sold_gate_retry_after(preds) is None

    def test_sold_low_exhaustion_returns_retry_after(self):
        """Sold + exhaustion within the horizon → pressure (429 retry-after)."""
        preds = [{"will_exhaust": True, "exhausts_in_hours": 1.0}]
        ra = sold_gate_retry_after(preds)
        assert ra is not None
        assert ra == 3600  # ceil(1.0h * 3600) — Retry-After is in SECONDS

    def test_most_urgent_window_wins(self):
        """The smallest exhausts_in_hours across windows drives the gate."""
        preds = [{"will_exhaust": True, "exhausts_in_hours": 30.0},
                 {"will_exhaust": True, "exhausts_in_hours": 1.2}]
        ra = sold_gate_retry_after(preds)
        assert ra is not None
        assert ra == 4320  # ceil(1.2h * 3600)

    def test_will_exhaust_with_none_hours_ignored(self):
        """A will-exhaust window without a numeric hours projection cannot
        block (insufficient data → skip)."""
        preds = [{"will_exhaust": True, "exhausts_in_hours": None,
                  "note": "Insufficient data"}]
        assert sold_gate_retry_after(preds) is None

    def test_fractional_hours_convert_to_seconds(self):
        """Sub-hour exhaustion → Retry-After = ceil(hours*3600) seconds."""
        preds = [{"will_exhaust": True, "exhausts_in_hours": 0.01}]
        assert sold_gate_retry_after(preds) == 36  # ceil(0.01h*3600) = 36s

    def test_hours_not_returned_as_seconds(self):
        """Regression: a ~2h headroom must yield Retry-After ~6840 (NOT 2)."""
        preds = [{"will_exhaust": True, "exhausts_in_hours": 1.9}]
        assert sold_gate_retry_after(preds) == 6840  # ceil(1.9h*3600)

    def test_custom_horizon_honored(self):
        """A caller-supplied horizon overrides the module default."""
        preds = [{"will_exhaust": True, "exhausts_in_hours": 10.0}]
        # 10h exhaustion inside a 24h horizon → blocked (429 retry-after).
        assert sold_gate_retry_after(preds, sold_safety_hours=24.0) == 36000
        # Same 10h exhaustion with a 5h horizon → NOT blocked.
        assert sold_gate_retry_after(preds, sold_safety_hours=5.0) is None

    def test_zero_horizon_disables_gate(self):
        preds = [{"will_exhaust": True, "exhausts_in_hours": 0.5}]
        assert sold_gate_retry_after(preds, sold_safety_hours=0) is None


# ── select_provider wiring ──────────────────────────────────────────────────

class TestSelectProviderCallerClass:
    def test_internal_unaltered_with_low_exhaustion(self, monkeypatch):
        """Internal requests are ALWAYS served: even when the pool predicts
        exhaustion, no gate attribute is attached."""
        monkeypatch.setattr(
            fr, "_sold_pool_predictions",
            lambda candidates: [{"will_exhaust": True,
                                 "exhausts_in_hours": 1.0}])
        candidates = select_provider(model="glm-5.2", caller_class="internal")
        assert not hasattr(candidates[0], "_sold_retry_after") \
            or getattr(candidates[0], "_sold_retry_after", None) is None

    def test_internal_default_is_unaltered(self, monkeypatch):
        """The default caller_class (internal) never triggers the gate."""
        monkeypatch.setattr(
            fr, "_sold_pool_predictions",
            lambda candidates: [{"will_exhaust": True,
                                 "exhausts_in_hours": 1.0}])
        candidates = select_provider(model="glm-5.2")
        assert getattr(candidates[0], "_sold_retry_after", None) is None

    def test_sold_low_exhaustion_attaches_pressure(self, monkeypatch):
        """Sold + low-exhaustion pool → candidates carry retry_after (>0)."""
        monkeypatch.setattr(
            fr, "_sold_pool_predictions",
            lambda candidates: [{"will_exhaust": True,
                                 "exhausts_in_hours": 1.0}])
        candidates = select_provider(model="glm-5.2", caller_class="sold")
        ra = getattr(candidates[0], "_sold_retry_after", None)
        assert ra is not None and ra > 0

    def test_sold_safe_pool_attaches_none(self, monkeypatch):
        """Sold + safe pool (no imminent exhaustion) → no pressure."""
        monkeypatch.setattr(
            fr, "_sold_pool_predictions",
            lambda candidates: [{"will_exhaust": False,
                                 "exhausts_in_hours": None}])
        candidates = select_provider(model="glm-5.2", caller_class="sold")
        assert getattr(candidates[0], "_sold_retry_after", None) is None

    def test_sold_prediction_failure_degrades_to_serve(self, monkeypatch):
        """A broken predictor must never 429 sold traffic (fail-open)."""
        def _boom(candidates):
            raise RuntimeError("predictor down")
        monkeypatch.setattr(fr, "_sold_pool_predictions", _boom)
        candidates = select_provider(model="glm-5.2", caller_class="sold")
        assert getattr(candidates[0], "_sold_retry_after", None) is None

    def test_sold_still_returns_real_candidates(self, monkeypatch):
        """The gate does not alter the candidate set — only attaches the
        decision for the HTTP layer to emit."""
        monkeypatch.setattr(
            fr, "_sold_pool_predictions",
            lambda candidates: [{"will_exhaust": True,
                                 "exhausts_in_hours": 1.0}])
        candidates = select_provider(model="glm-5.2", caller_class="sold")
        names = [c.name for c in candidates if c.name != "fallback"]
        assert names, "sold-gated request must still produce candidates"


class TestSoldPredictionCache:
    def test_cache_serves_second_call(self, monkeypatch):
        """predict_exhaustion is invoked ONCE per provider per TTL window —
        the sold path must not self-HTTP /quota per candidate per request."""
        import flat_router as _fr
        import burn_predictor as _bp

        calls = {"n": 0}

        def _fake(name):
            calls["n"] += 1
            return [{"key": name, "will_exhaust": True,
                     "exhausts_in_hours": 0.5}]

        _orig = _bp.predict_exhaustion
        _bp.predict_exhaustion = _fake
        monkeypatch.setattr(_fr, "_sold_pred_cache", {})
        try:
            # Cold call on a fresh cache → must fetch from the predictor.
            p1 = _fr._sold_predictions_cached("ours")
            assert calls["n"] == 1
            # Warm call within the TTL → memo hit, predictor NOT re-invoked.
            p2 = _fr._sold_predictions_cached("ours")
            assert p2 == p1 and calls["n"] == 1
            # A different provider key is a distinct memo entry → fetch again.
            _fr._sold_predictions_cached("friend")
            assert calls["n"] == 2
        finally:
            _bp.predict_exhaustion = _orig

    def test_cache_fail_open_on_predictor_error(self, monkeypatch):
        """A throwing predictor yields [] (serve) and never poisons routing."""
        import flat_router as _fr
        import burn_predictor as _bp

        calls = {"n": 0}

        def _boom(name):
            calls["n"] += 1
            raise RuntimeError("predictor down")

        _orig = _bp.predict_exhaustion
        _bp.predict_exhaustion = _boom
        monkeypatch.setattr(_fr, "_sold_pred_cache", {})
        try:
            assert _fr._sold_predictions_cached("ours") == []
            # Errors are NOT memoized: every call re-attempts so the gate
            # recovers as soon as the predictor heals (no stale serve window).
            assert _fr._sold_predictions_cached("ours") == []
            assert calls["n"] == 2
        finally:
            _bp.predict_exhaustion = _orig


# ── Proxy X-Priority classification ─────────────────────────────────────────

class TestCallerClassClassification:
    def test_missing_header_defaults_internal(self):
        from zai_proxy import _resolve_caller_class
        assert _resolve_caller_class({}) == "internal"

    def test_absent_priority_defaults_internal(self):
        from zai_proxy import _resolve_caller_class
        headers = {"X-Priority": ""}
        assert _resolve_caller_class(headers) == "internal"

    def test_sold_header_classifies_sold(self):
        from zai_proxy import _resolve_caller_class
        headers = {"X-Priority": "sold"}
        assert _resolve_caller_class(headers) == "sold"

    def test_internal_header_classifies_internal(self):
        from zai_proxy import _resolve_caller_class
        headers = {"X-Priority": "internal"}
        assert _resolve_caller_class(headers) == "internal"

    def test_unknown_header_falls_back_internal(self):
        from zai_proxy import _resolve_caller_class
        headers = {"X-Priority": "VIP-customer"}
        assert _resolve_caller_class(headers) == "internal"

    def test_header_case_insensitive(self):
        from zai_proxy import _resolve_caller_class
        headers = {"X-Priority": "SOLD"}
        assert _resolve_caller_class(headers) == "sold"
