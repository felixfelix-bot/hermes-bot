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

    def test_non_dict_prediction_entries_never_raise(self):
        """Non-dict entries in the predictions list cannot block and never
        raise (cold-review finding 3 — keeps the 'never raises' contract)."""
        preds = [None, "window", 42,
                 {"will_exhaust": True, "exhausts_in_hours": 0.5}]
        ra = sold_gate_retry_after(preds)
        assert ra is not None and ra > 0  # dict entry still drives the gate

    def test_all_non_dict_entries_serve(self):
        """A predictions list of only non-dict entries degrades to serve."""
        assert sold_gate_retry_after([None, [], "junk", 3.14]) is None


class TestSoldSafetyHoursEnvParse:
    def test_malformed_env_degrades_to_default(self, monkeypatch):
        """A malformed SOLD_SAFETY_HOURS env value must NOT raise at import
        (cold-review finding 2): it degrades to the 2.0 default so the flat
        router and the sold gate survive a config typo."""
        import flat_router as _fr
        monkeypatch.setenv("SOLD_SAFETY_HOURS", "2h")  # not a float
        assert _fr._sold_safety_hours_default() == 2.0
        monkeypatch.setenv("SOLD_SAFETY_HOURS", "not-a-number")
        assert _fr._sold_safety_hours_default() == 2.0

    def test_valid_env_value_honored(self, monkeypatch):
        import flat_router as _fr
        monkeypatch.setenv("SOLD_SAFETY_HOURS", "3.5")
        assert _fr._sold_safety_hours_default() == 3.5
        monkeypatch.delenv("SOLD_SAFETY_HOURS", raising=False)
        assert _fr._sold_safety_hours_default() == 2.0


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


# ── C3 doxed-lane allowlist filter (PLAN inference-routing-remediation C3,
# task t_f36fc1ef, 2026-09-09) ───────────────────────────────────────────────
#
# Sold traffic must NEVER be routed to a doxed lane, even when the doxed lane
# is the cheapest or ONLY candidate for the model. Internal traffic keeps
# full pool access (operator override 2026-09-09: doxed lanes re-enabled for
# internal use — free included quota instead of ~$40/day PAYGO bleed).

#: The doxed lane set (docs/opsec-dox-status-log.md — the 6 flagged keys).
#: Kept in sync with flat_router.DOXED_PROVIDERS by the registry test below.
_DOXED_LANES = {
    "ollama_cloud", "ollama_cloud_2", "ollama_cloud_3", "ollama_cloud_4",
    "openrouter", "telnyx",
}


def _force_static_pool(monkeypatch):
    """Make select_provider() fully deterministic: every gate that consults
    live state (health, live-catalog, exhaust weight, garbage multiplier,
    sold-gate predictions, effective cost) is patched to a constant so the
    candidate pool is EXACTLY the PROVIDER_MODELS registry membership."""
    monkeypatch.setattr(fr, "_is_provider_healthy", lambda name: True)
    monkeypatch.setattr(fr, "_passes_live_catalog_guard",
                        lambda provider, model_id: True)
    monkeypatch.setattr(fr, "_apply_exhaust_weight",
                        lambda name, cost: cost)
    monkeypatch.setattr(fr, "_garbage_mult_or_one",
                        lambda name, model: 1.0)
    monkeypatch.setattr(fr, "_sold_pool_predictions", lambda candidates: [])
    monkeypatch.setattr(fr, "_get_effective_cost",
                        lambda name, model_id, difficulty: 1.0)


def _pool(model, caller_class="internal"):
    """Candidate lane-name set (fallback excluded) for a request."""
    cands = select_provider(model=model, caller_class=caller_class)
    return {c.name for c in cands if c.name != "fallback"}


def _registry_servers(model):
    """Lanes whose PROVIDER_MODELS entry lists ``model`` (live module)."""
    return {lane for lane, models in fr.PROVIDER_MODELS.items()
            if model in models}


class TestDoxedLaneFilterRegistry:
    def test_doxed_lane_constant_exists(self):
        """DOXED_PROVIDERS is exported and is a set of lane names."""
        assert hasattr(fr, "DOXED_PROVIDERS")
        assert isinstance(fr.DOXED_PROVIDERS, (set, frozenset))

    def test_doxed_lane_constant_matches_opsec_log(self):
        """The constant must list exactly the 6 doxed lanes from the opsec
        log (ollama_cloud 1-4, openrouter, telnyx)."""
        assert set(fr.DOXED_PROVIDERS) == _DOXED_LANES

    def test_sold_lane_allowed_never_raises(self, monkeypatch):
        """The allowlist check fails OPEN on a broken comparison: a
        non-hashable/uncomparable name must never raise into routing and
        never block the lane (the remaining gates still apply)."""
        monkeypatch.setattr(fr, "DOXED_PROVIDERS", ["ollama_cloud"])
        # list.__contains__ with a set arg raises TypeError -> fail-open
        assert fr._sold_lane_allowed({"unhashable": 1}) is True
        assert fr._sold_lane_allowed("ollama_cloud") is False
        monkeypatch.setattr(fr, "DOXED_PROVIDERS", None)
        assert fr._sold_lane_allowed("anything") is True


class TestDoxedLaneFilterSold:
    """caller_class='sold' NEVER receives a doxed candidate."""

    def test_sold_pool_has_no_doxed_lanes(self, monkeypatch):
        """Across every registry model, the sold pool contains zero doxed
        lanes — the filter DROPS them, not re-orders them."""
        _force_static_pool(monkeypatch)
        for model in sorted({m for models in fr.PROVIDER_MODELS.values()
                             for m in models}):
            leaked = _pool(model, "sold") & _DOXED_LANES
            assert not leaked, (
                f"sold pool for {model!r} leaked doxed lane(s) "
                f"{sorted(leaked)}")

    def test_sold_pool_is_exactly_the_clean_lanes(self, monkeypatch):
        """With every gate forced open, the sold pool for a model is EXACTLY
        (registry servers of that model) minus (doxed lanes) — no more, no
        less: the filter neither over-blocks clean lanes nor misses doxed
        ones."""
        _force_static_pool(monkeypatch)
        for model in ("glm-5.2", "kimi-k3", "deepseek/deepseek-v4-flash"):
            expected = _registry_servers(model) - _DOXED_LANES
            assert expected, f"test model {model!r} has no clean lane?"
            assert _pool(model, "sold") == expected

    def test_sold_clean_lanes_still_reachable(self, monkeypatch):
        """The plan's clean-lane regression set (deepseek, chutes, ours,
        friend, opencode_go, ppq, neuralwatt) stays reachable by sold for
        models they serve."""
        _force_static_pool(monkeypatch)
        sold_flash = _pool("deepseek/deepseek-v4-flash", "sold")
        assert "deepseek" in sold_flash, "clean lane deepseek lost"
        assert "chutes" in sold_flash, "clean lane chutes lost"
        assert "opencode_go" in sold_flash, "clean lane opencode_go lost"
        assert "ppq" in sold_flash, "clean lane ppq lost"
        assert "neuralwatt" in sold_flash, "clean lane neuralwatt lost"
        sold_glm = _pool("glm-5.2", "sold")
        assert {"ours", "friend", "opencode_go", "ppq"} <= sold_glm

    def test_sold_sole_doxed_provider_yields_fallback_only(self, monkeypatch):
        """When a doxed lane is the ONLY provider for a model (registry
        patched to just that lane), a sold request yields NO candidates —
        the fallback (clean 503) path, never the doxed lane. An internal
        request for the same model still gets the lane."""
        _force_static_pool(monkeypatch)
        orig = fr.PROVIDER_MODELS
        try:
            fr.PROVIDER_MODELS = {"ollama_cloud": set(orig["ollama_cloud"])}
            assert _pool("glm-5.2", "sold") == set()
            cands = select_provider(model="glm-5.2", caller_class="sold")
            assert [c.name for c in cands] == ["fallback"]
            assert _pool("glm-5.2", "internal") == {"ollama_cloud"}
        finally:
            fr.PROVIDER_MODELS = orig


class TestDoxedLaneFilterInternal:
    """caller_class='internal' keeps the doxed lanes (operator override
    2026-09-09: free included quota for internal use)."""

    def test_internal_pool_keeps_doxed_lanes(self, monkeypatch):
        """Forced-healthy internal pool contains every REGISTERED doxed lane
        that serves the model (ollama_cloud_4 has no registry entry yet —
        its filter coverage is the constant-level test above)."""
        _force_static_pool(monkeypatch)
        for model in ("glm-5.2", "kimi-k3"):
            expected_doxed = _registry_servers(model) & _DOXED_LANES
            assert expected_doxed, f"model {model!r} serves no doxed lane?"
            assert expected_doxed <= _pool(model, "internal"), (
                f"internal pool for {model!r} lost doxed lanes "
                f"{sorted(expected_doxed - _pool(model, 'internal'))}")

    def test_internal_pool_equals_full_registry(self, monkeypatch):
        """The filter is INERT for internal: pool == all registry servers."""
        _force_static_pool(monkeypatch)
        for model in ("glm-5.2", "kimi-k3", "deepseek/deepseek-v4-flash"):
            assert _pool(model, "internal") == _registry_servers(model)

    def test_default_caller_class_is_unfiltered(self, monkeypatch):
        """The default caller_class (internal) applies no doxed filter."""
        _force_static_pool(monkeypatch)
        assert _pool("kimi-k3") == _registry_servers("kimi-k3")


class TestDoxedLaneFilterLiveGates:
    """The doxed filter must not weaken the OTHER gates: an unhealthy doxed
    lane stays excluded for internal, and a healthy clean lane still passes
    health for sold (filter ordering interplay)."""

    def test_unhealthy_doxed_lane_excluded_for_internal(self, monkeypatch):
        _force_static_pool(monkeypatch)
        monkeypatch.setattr(
            fr, "_is_provider_healthy",
            lambda name: name != "ollama_cloud")
        assert "ollama_cloud" not in _pool("glm-5.2", "internal")

    def test_health_gate_still_applies_to_sold_clean_lanes(self, monkeypatch):
        _force_static_pool(monkeypatch)
        monkeypatch.setattr(
            fr, "_is_provider_healthy",
            lambda name: name != "opencode_go")
        assert "opencode_go" not in _pool("glm-5.2", "sold")
        assert "opencode_go" not in _pool("glm-5.2", "internal")

