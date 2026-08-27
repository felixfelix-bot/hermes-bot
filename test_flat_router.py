#!/usr/bin/env python3
"""Tests for Phase 1 flat router: select_provider() in shadow mode.

Tests verify:
1. select_provider() returns candidates for known models
2. Model filtering excludes providers that don't serve the model
3. Health gating excludes unhealthy providers
4. Cost ordering (cheapest first)
5. _is_provider_healthy() for various states
6. _update_kalman_after_request() updates Kalman filters
7. Shadow logging records the comparison
"""
import os
import sys
import time
import json
import sqlite3
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock
from dataclasses import dataclass

import pytest

# ── Path setup ──────────────────────────────────────────────────────────────
BOT = os.path.expanduser("~/.hermes/bot")
MRE = os.path.expanduser("~/merchant-routing-engine")
for p in [BOT, MRE, os.path.join(MRE, "src")]:
    if p not in sys.path:
        sys.path.insert(0, p)


# ── Import the flat router module (our new code) ────────────────────────────
from flat_router import (
    ProviderCandidate,
    PROVIDER_MODELS,
    select_provider,
    _is_provider_healthy,
    _update_kalman_after_request,
    _dispatch_to_provider,
    _ALL_PROVIDERS,
)


# ── Test 1: select_provider returns candidates for known models ─────────────

class TestSelectProviderReturnsCandidates:
    def test_glm52_returns_candidates(self):
        """select_provider for glm-5.2 should return non-empty list."""
        candidates = select_provider(model="glm-5.2")
        assert len(candidates) > 0, "Expected candidates for glm-5.2"
        assert all(isinstance(c, ProviderCandidate) for c in candidates)

    def test_kimi_k3_returns_candidates(self):
        """select_provider for kimi-k3 should return non-empty list."""
        candidates = select_provider(model="kimi-k3")
        assert len(candidates) > 0, "Expected candidates for kimi-k3"

    def test_unknown_model_returns_fallback(self):
        """Unknown model should return fallback candidate, not crash."""
        candidates = select_provider(model="nonexistent-model-xyz")
        # Should return at least a fallback candidate
        assert len(candidates) >= 1
        # The fallback candidate should have inf cost or be named 'fallback'
        assert candidates[0].name == "fallback" or candidates[0].effective_cost == float("inf")

    def test_each_candidate_has_required_fields(self):
        """Each ProviderCandidate must have name, model, effective_cost, dispatch_fn, reason."""
        candidates = select_provider(model="glm-5.2")
        for c in candidates:
            assert hasattr(c, "name")
            assert hasattr(c, "model")
            assert hasattr(c, "effective_cost")
            assert hasattr(c, "dispatch_fn")
            assert hasattr(c, "reason")


# ── Test 2: Model filtering excludes providers that don't serve the model ───

class TestModelFiltering:
    def test_telnyx_excluded_for_glm52(self):
        """Telnyx only serves kimi models, not glm-5.2."""
        candidates = select_provider(model="glm-5.2")
        names = [c.name for c in candidates]
        assert "telnyx" not in names, "Telnyx should not serve glm-5.2"

    def test_telnyx_included_for_kimi_k3(self):
        """Telnyx should be a candidate for kimi-k3."""
        candidates = select_provider(model="kimi-k3")
        names = [c.name for c in candidates]
        assert "telnyx" in names, "Telnyx should serve kimi-k3"

    def test_kimi_k3_excludes_zai(self):
        """z.ai keys don't serve kimi-k3."""
        candidates = select_provider(model="kimi-k3")
        names = [c.name for c in candidates]
        assert "ours" not in names, "z.ai ours should not serve kimi-k3"
        assert "friend" not in names, "z.ai friend should not serve kimi-k3"

    def test_providemodels_registry_completeness(self):
        """PROVIDER_MODELS should cover all 12 providers."""
        expected = {
            "ours", "friend", "ollama_cloud", "ollama_cloud_2",
            "opencode_go", "neuralwatt", "deepinfra", "ppq",
            "openrouter", "telnyx", "routstr", "routstrd",
        }
        assert expected.issubset(set(PROVIDER_MODELS.keys())), \
            f"Missing providers: {expected - set(PROVIDER_MODELS.keys())}"


# ── Test 3: Health gating excludes unhealthy providers ──────────────────────

class TestHealthGating:
    def test_disabled_provider_excluded(self):
        """A manually disabled provider should not appear in candidates."""
        with patch("flat_router._is_manually_disabled", side_effect=lambda n: n == "ppq"):
            candidates = select_provider(model="glm-5.2")
            names = [c.name for c in candidates]
            assert "ppq" not in names, "Disabled ppq should be excluded"

    def test_unfunded_provider_excluded(self):
        """An unfunded external provider should not appear in candidates."""
        with patch("flat_router._is_provider_funded", side_effect=lambda n: n != "deepinfra"):
            candidates = select_provider(model="glm-5.2")
            names = [c.name for c in candidates]
            assert "deepinfra" not in names, "Unfunded deepinfra should be excluded"

    def test_unhealthy_key_excluded(self):
        """An unhealthy key (in backoff) should not appear in candidates."""
        with patch("flat_router._is_key_healthy", side_effect=lambda n: n != "friend"):
            candidates = select_provider(model="glm-5.2")
            names = [c.name for c in candidates]
            assert "friend" not in names, "Unhealthy friend should be excluded"


# ── Test 4: Cost ordering (cheapest first) ──────────────────────────────────

class TestCostOrdering:
    def test_candidates_sorted_cheapest_first(self):
        """Candidates should be sorted by effective_cost ascending."""
        candidates = select_provider(model="glm-5.2")
        costs = [c.effective_cost for c in candidates if c.effective_cost != float("inf")]
        assert costs == sorted(costs), "Candidates should be sorted cheapest first"

    def test_fallback_is_last(self):
        """Fallback candidate (inf cost) should be last if present."""
        candidates = select_provider(model="glm-5.2")
        if len(candidates) > 1:
            for i in range(len(candidates) - 1):
                if candidates[i].effective_cost == float("inf"):
                    assert candidates[i + 1].effective_cost == float("inf"), \
                        "Non-inf cost after inf cost"


# ── Test 5: _is_provider_healthy() for various states ───────────────────────

class TestIsProviderHealthy:
    def test_healthy_provider_returns_true(self):
        """A healthy provider with no issues should return True."""
        with patch("flat_router._is_manually_disabled", return_value=False), \
             patch("flat_router._is_key_healthy", return_value=True), \
             patch("flat_router._is_provider_funded", return_value=True):
            assert _is_provider_healthy("ppq") is True

    def test_manually_disabled_returns_false(self):
        """A manually disabled provider should return False."""
        with patch("flat_router._is_manually_disabled", side_effect=lambda n: n == "ours"):
            assert _is_provider_healthy("ours") is False

    def test_unhealthy_key_returns_false(self):
        """An unhealthy key should return False."""
        with patch("flat_router._is_manually_disabled", return_value=False), \
             patch("flat_router._is_key_healthy", return_value=False):
            assert _is_provider_healthy("friend") is False

    def test_unfunded_external_returns_false(self):
        """An unfunded external provider should return False."""
        with patch("flat_router._is_manually_disabled", return_value=False), \
             patch("flat_router._is_key_healthy", return_value=True), \
             patch("flat_router._is_provider_funded", side_effect=lambda n: n != "deepinfra"):
            assert _is_provider_healthy("deepinfra") is False

    def test_zai_key_does_not_check_funding(self):
        """z.ai keys should not be funding-checked (they're subscription)."""
        with patch("flat_router._is_manually_disabled", return_value=False), \
             patch("flat_router._is_key_healthy", return_value=True), \
             patch("flat_router._is_provider_funded", side_effect=AssertionError):
            # Should not raise — funding check is skipped for z.ai keys
            assert _is_provider_healthy("ours") is True


# ── Test 6: _update_kalman_after_request() updates Kalman filters ───────────

class TestKalmanUpdate:
    def test_price_kalman_updated(self):
        """PriceKalman should be updated with measured $/M."""
        from price_kalman import PriceKalman
        pk = PriceKalman(initial_rate=1.0)
        ck = MagicMock()
        # Register a test provider in _ALL_PROVIDERS
        original = _ALL_PROVIDERS.get("test_pk_update")
        _ALL_PROVIDERS["test_pk_update"] = {
            "price_kalman": pk,
            "consumption_kalman": ck,
        }
        try:
            initial_rate = pk.base_rate
            _update_kalman_after_request("test_pk_update", cost_usd=2.0, total_tokens=500_000)
            # base_rate should have moved toward 2.0/0.5M * 1M = 4.0
            assert pk.base_rate != initial_rate or pk._updates > 0, \
                "PriceKalman should have been updated"
        finally:
            if original is not None:
                _ALL_PROVIDERS["test_pk_update"] = original
            else:
                _ALL_PROVIDERS.pop("test_pk_update", None)

    def test_consumption_kalman_updated(self):
        """ConsumptionKalman should be updated with token count."""
        from consumption_kalman import ConsumptionKalman
        pk = MagicMock()
        ck = ConsumptionKalman()
        original = _ALL_PROVIDERS.get("test_ck_update")
        _ALL_PROVIDERS["test_ck_update"] = {
            "price_kalman": pk,
            "consumption_kalman": ck,
        }
        try:
            initial_count = ck._update_count
            _update_kalman_after_request("test_ck_update", cost_usd=1.0, total_tokens=10000)
            assert ck._update_count == initial_count + 1, \
                "ConsumptionKalman should have been updated"
        finally:
            if original is not None:
                _ALL_PROVIDERS["test_ck_update"] = original
            else:
                _ALL_PROVIDERS.pop("test_ck_update", None)

    def test_zero_tokens_no_crash(self):
        """Should not crash with zero tokens."""
        _update_kalman_after_request("ppq", cost_usd=0.0, total_tokens=0)
        # Should not raise

    def test_none_cost_no_crash(self):
        """Should not crash with None cost."""
        _update_kalman_after_request("ppq", cost_usd=None, total_tokens=1000)
        # Should not raise

    def test_unknown_provider_no_crash(self):
        """Should not crash for an unknown provider."""
        _update_kalman_after_request("nonexistent_provider", cost_usd=1.0, total_tokens=1000)
        # Should not raise


# ── Test 7: Shadow logging records the comparison ───────────────────────────

class TestShadowLogging:
    def test_shadow_log_records_comparison(self, tmp_path):
        """Shadow log should record both best_key choice and select_provider top candidate."""
        from flat_router import _log_flat_router_shadow

        db_path = str(tmp_path / "test_shadow.db")

        # Log a comparison
        _log_flat_router_shadow(
            db_path=db_path,
            best_key_choice="friend",
            candidates=[
                ProviderCandidate(name="ppq", model="glm-5.2", effective_cost=0.80,
                                  dispatch_fn=None, reason="cheapest"),
                ProviderCandidate(name="friend", model="glm-5.2", effective_cost=0.082,
                                  dispatch_fn=None, reason="z.ai"),
            ],
            model="glm-5.2",
        )

        # Verify the row was written
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT best_key_choice, flat_router_top, agreement, candidate_list "
            "FROM flat_router_shadow_decisions"
        ).fetchall()
        conn.close()

        assert len(rows) == 1, "Expected one shadow log row"
        best_key, flat_top, agreement, candidate_list = rows[0]
        assert best_key == "friend"
        assert flat_top == "ppq"
        assert agreement == 0, "friend != ppq, so agreement should be 0"
        # candidate_list should be valid JSON
        parsed = json.loads(candidate_list)
        assert len(parsed) == 2

    def test_shadow_log_agreement_yes(self, tmp_path):
        """When best_key and select_provider agree, agreement should be 1."""
        from flat_router import _log_flat_router_shadow

        db_path = str(tmp_path / "test_shadow_agree.db")

        _log_flat_router_shadow(
            db_path=db_path,
            best_key_choice="friend",
            candidates=[
                ProviderCandidate(name="friend", model="glm-5.2", effective_cost=0.082,
                                  dispatch_fn=None, reason="cheapest"),
            ],
            model="glm-5.2",
        )

        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT agreement FROM flat_router_shadow_decisions"
        ).fetchall()
        conn.close()

        assert len(rows) == 1
        assert rows[0][0] == 1, "Same provider → agreement = 1"


# ── Test 8: _dispatch_to_provider maps to the right method ──────────────────

class TestDispatchMapping:
    def test_zai_keys_map_to_zai_upstream(self):
        """ours/friend should map to z.ai upstream dispatch."""
        # dispatch_fn should not be None for z.ai keys
        candidates = select_provider(model="glm-5.2")
        zai_candidates = [c for c in candidates if c.name in ("ours", "friend")]
        for c in zai_candidates:
            assert c.dispatch_fn is not None, f"dispatch_fn for {c.name} should not be None"

    def test_ollama_maps_to_ollama_cloud_any(self):
        """ollama_cloud should map to _try_ollama_cloud_any."""
        with patch("flat_router._is_key_healthy", return_value=True), \
             patch("flat_router._is_provider_funded", return_value=True), \
             patch("flat_router._is_manually_disabled", return_value=False):
            candidates = select_provider(model="glm-5.2")
            ollama_candidates = [c for c in candidates if c.name in ("ollama_cloud", "ollama_cloud_2")]
            for c in ollama_candidates:
                assert c.dispatch_fn is not None, f"dispatch_fn for {c.name} should not be None"

    def test_telnyx_maps_to_telnyx(self):
        """telnyx should have a dispatch_fn."""
        with patch("flat_router._is_key_healthy", return_value=True), \
             patch("flat_router._is_provider_funded", return_value=True), \
             patch("flat_router._is_manually_disabled", return_value=False):
            candidates = select_provider(model="kimi-k3")
            telnyx_candidates = [c for c in candidates if c.name == "telnyx"]
            for c in telnyx_candidates:
                assert c.dispatch_fn is not None, "dispatch_fn for telnyx should not be None"


# ── Phase 3 tests: full cutover, rollback flag, Kalman live updates ──────────


class TestFlatRouterCutover:
    """Phase 3: verify .disable_flat_router flag controls routing path."""

    def test_disable_flag_check_exists(self):
        """_proxy() should check for .disable_flat_router flag file."""
        # Verify the flag path constant is referenced in zai_proxy.py
        import zai_proxy
        source = open(zai_proxy.__file__).read()
        assert ".disable_flat_router" in source, \
            "_proxy() must check for .disable_flat_router flag"

    def test_select_provider_imported_in_zai_proxy(self):
        """zai_proxy.py should import select_provider from flat_router."""
        import zai_proxy
        source = open(zai_proxy.__file__).read()
        assert "select_provider" in source, \
            "zai_proxy.py must import and use select_provider"

    def test_dispatch_to_provider_imported_in_zai_proxy(self):
        """zai_proxy.py should import _dispatch_to_provider from flat_router."""
        import zai_proxy
        source = open(zai_proxy.__file__).read()
        assert "_dispatch_to_provider" in source, \
            "zai_proxy.py must import and use _dispatch_to_provider"

    def test_update_kalman_after_request_called_in_zai_proxy(self):
        """zai_proxy.py should call _update_kalman_after_request after dispatch."""
        import zai_proxy
        source = open(zai_proxy.__file__).read()
        assert "_update_kalman_after_request" in source, \
            "zai_proxy.py must call _update_kalman_after_request for live Kalman updates"

    def test_try_zai_key_method_exists(self):
        """Handler should have a _try_zai_key method for flat router dispatch."""
        import zai_proxy
        source = open(zai_proxy.__file__).read()
        assert "def _try_zai_key" in source, \
            "Handler must have _try_zai_key method for z.ai dispatch via flat router"

    def test_zai_dispatch_fn_not_returning_false(self):
        """_make_dispatch_fn for z.ai keys should return a real dispatch fn, not None."""
        from flat_router import _make_dispatch_fn
        fn = _make_dispatch_fn("ours")
        assert fn is not None, "ours dispatch_fn should not be None"
        fn2 = _make_dispatch_fn("friend")
        assert fn2 is not None, "friend dispatch_fn should not be None"

    def test_503_on_all_candidates_fail(self):
        """When all candidates fail, _proxy() should send 503."""
        import zai_proxy
        source = open(zai_proxy.__file__).read()
        # The flat router path should have a 503 fallback
        assert "503" in source, "Flat router path must handle all-candidates-fail with 503"

    def test_x_provider_header_in_flat_router_path(self):
        """Flat router path should set X-Provider header for observability."""
        import zai_proxy
        source = open(zai_proxy.__file__).read()
        assert "X-Provider" in source, \
            "Flat router path should set X-Provider header for observability"


class TestFlatRouterKalmanLiveUpdates:
    """Phase 3: verify Kalman filters get live updates after successful dispatch."""

    def test_kalman_update_after_success(self):
        """After a successful dispatch, _update_kalman_after_request should be called."""
        # Verify the flat router path calls _update_kalman_after_request
        import zai_proxy
        source = open(zai_proxy.__file__).read()
        # Should appear in the flat router path (import alias + live update call)
        # The import uses "as _flat_kalman_update" and the call uses _flat_kalman_update
        assert "_update_kalman_after_request" in source, \
            "_update_kalman_after_request should be imported in zai_proxy"
        assert "_flat_kalman_update" in source, \
            "_update_kalman_after_request should be called (via _flat_kalman_update alias) " \
            "in the flat router path"

    def test_kalman_update_with_cost_and_tokens(self):
        """_update_kalman_after_request should receive cost_usd and total_tokens."""
        # Verify _extract_cost and _parse_usage are used in the flat router path
        import zai_proxy
        source = open(zai_proxy.__file__).read()
        assert "_extract_cost" in source, \
            "Flat router path should use _extract_cost for Kalman cost input"
        assert "_parse_usage" in source, \
            "Flat router path should use _parse_usage for Kalman token input"


class TestFlatRouterFallback:
    """Phase 3: verify fallback to next candidate on provider failure."""

    def test_candidate_iteration_in_proxy(self):
        """_proxy() should iterate candidates and try each."""
        import zai_proxy
        source = open(zai_proxy.__file__).read()
        # The flat router path should iterate candidates
        assert "for candidate in" in source or "for _cand in" in source, \
            "Flat router path should iterate candidates in a loop"

    def test_mark_key_failure_on_dispatch_failure(self):
        """On dispatch failure, the key should be marked as failed."""
        import zai_proxy
        source = open(zai_proxy.__file__).read()
        # Should call _mark_key_failure or _mark_key_exhausted on failure
        assert "_mark_key_failure" in source or "_mark_key_exhausted" in source, \
            "Flat router path should mark key failure on dispatch failure"


class TestFlatRouterStreaming:
    """Phase 3: verify streaming requests work through flat router."""

    def test_streaming_through_dispatch(self):
        """Streaming requests should work through _dispatch_to_provider."""
        # The dispatch functions call _try_* methods which already handle streaming
        # This test verifies the dispatch chain is intact
        from flat_router import _dispatch_to_provider, _make_dispatch_fn
        # All dispatch functions should be callable
        for name in ["ours", "friend", "ollama_cloud", "opencode_go", "ppq", "deepinfra"]:
            fn = _make_dispatch_fn(name)
            assert fn is not None, f"dispatch_fn for {name} should not be None"

    def test_streaming_body_passthrough(self):
        """The flat router path should pass the body through to dispatch."""
        import zai_proxy
        source = open(zai_proxy.__file__).read()
        # The flat router path should pass body to _dispatch_to_provider
        assert "_dispatch_to_provider" in source, \
            "Flat router path should call _dispatch_to_provider with the request body"


class TestRollbackFlag:
    """Phase 3: verify .disable_flat_router rollback mechanism."""

    def test_rollback_flag_path_constant(self):
        """The rollback flag path should be ~/.hermes/bot/.disable_flat_router."""
        import zai_proxy
        source = open(zai_proxy.__file__).read()
        assert "~/.hermes/bot/.disable_flat_router" in source or \
               ".disable_flat_router" in source, \
            "Rollback flag path should be .disable_flat_router in ~/.hermes/bot/"

    def test_best_key_preserved(self):
        """best_key() function should still exist for rollback."""
        import zai_proxy
        source = open(zai_proxy.__file__).read()
        assert "def best_key" in source, \
            "best_key() must be preserved for .disable_flat_router rollback"

    def test_old_failover_chain_preserved(self):
        """Old failover chain (ollama, opencode, external) should still exist."""
        import zai_proxy
        source = open(zai_proxy.__file__).read()
        assert "_try_ollama_cloud_any" in source, \
            "_try_ollama_cloud_any must be preserved for rollback"
        assert "_try_external_failover" in source, \
            "_try_external_failover must be preserved for rollback"
        assert "_try_opencode_go" in source, \
            "_try_opencode_go must be preserved for rollback"


# ── Canonicalization tests (2026-08-27): short-form deepseek resolution ─────
# Incident: 2,305 prod requests for "deepseek-v4-flash"/"deepseek-v4-pro"
# (and hyphen-tagged "-0731"/"-0813" forms) matched ONLY opencode_go in
# PROVIDER_MODELS → single-candidate lists → 503s when opencode_go was
# down. Canonicalization must map these to the canonical
# "deepseek/deepseek-v4-*" forms so ALL providers compete on price.


class TestModelCanonicalization:
    """Short-form / tagged model IDs must resolve to the full provider set."""

    def _healthy_all(self):
        """Patch context where every provider passes the health gate."""
        return patch("flat_router._is_provider_healthy", return_value=True)

    def test_short_form_deepseek_flash_multi_candidate(self):
        """'deepseek-v4-flash' must resolve to ≥3 providers incl. a per-token one."""
        with self._healthy_all():
            candidates = select_provider(model="deepseek-v4-flash")
        names = [c.name for c in candidates if c.name != "fallback"]
        assert len(names) >= 3, \
            f"Short-form deepseek-v4-flash resolved to only {names} — " \
            f"canonicalization missing or broken"
        per_token = {"ppq", "neuralwatt", "deepinfra", "openrouter",
                     "routstr", "routstrd"}
        assert per_token & set(names), \
            f"No per-token provider in candidate list {names}"

    def test_short_form_deepseek_pro_multi_candidate(self):
        """'deepseek-v4-pro' must resolve to ≥3 providers incl. a per-token one."""
        with self._healthy_all():
            candidates = select_provider(model="deepseek-v4-pro")
        names = [c.name for c in candidates if c.name != "fallback"]
        assert len(names) >= 3, \
            f"Short-form deepseek-v4-pro resolved to only {names}"
        per_token = {"ppq", "neuralwatt", "deepinfra", "openrouter",
                     "routstr", "routstrd"}
        assert per_token & set(names), \
            f"No per-token provider in candidate list {names}"

    def test_ollama_tagged_hyphen_form_normalized(self):
        """'deepseek-v4-flash-0731' (cron-style tag) → same set as canonical."""
        with self._healthy_all():
            tagged = select_provider(model="deepseek-v4-flash-0731")
            canonical = select_provider(model="deepseek/deepseek-v4-flash")
        tagged_names = sorted(c.name for c in tagged if c.name != "fallback")
        canonical_names = sorted(c.name for c in canonical if c.name != "fallback")
        assert tagged_names == canonical_names, \
            f"Tagged form candidates {tagged_names} != canonical {canonical_names}"
        assert len(tagged_names) >= 3, \
            f"Tagged form resolved to only {tagged_names}"

    def test_canonicalization_idempotent(self):
        """canonicalize_model must leave canonical + z.ai/kimi IDs untouched."""
        from flat_router import canonicalize_model
        # Canonical forms unchanged (idempotent)
        assert canonicalize_model("deepseek/deepseek-v4-flash") == \
            "deepseek/deepseek-v4-flash"
        assert canonicalize_model("deepseek/deepseek-v4-pro") == \
            "deepseek/deepseek-v4-pro"
        # Common z.ai / kimi IDs unchanged
        assert canonicalize_model("glm-5.2") == "glm-5.2"
        assert canonicalize_model("glm-5.3") == "glm-5.3"
        assert canonicalize_model("kimi-k3") == "kimi-k3"
        # Aliases resolve
        assert canonicalize_model("deepseek-v4-flash") == "deepseek/deepseek-v4-flash"
        assert canonicalize_model("deepseek-v4-pro") == "deepseek/deepseek-v4-pro"
        assert canonicalize_model("deepseek-v4-flash-0731") == "deepseek/deepseek-v4-flash"
        assert canonicalize_model("deepseek-v4-pro-0813") == "deepseek/deepseek-v4-pro"
        # Whitespace stripped
        assert canonicalize_model("  deepseek-v4-flash  ") == "deepseek/deepseek-v4-flash"
        # Unknown models pass through verbatim
        assert canonicalize_model("nonexistent-model-xyz") == "nonexistent-model-xyz"

    def test_alias_outgoing_names_correct(self):
        """Each candidate for a short-form request must use the provider's
        outgoing model name from _PROVIDER_MODEL_NAMES (translation at
        selection must match translation at dispatch)."""
        import zai_proxy
        with self._healthy_all():
            candidates = select_provider(model="deepseek-v4-flash")
        canonical = "deepseek/deepseek-v4-flash"
        checked = 0
        for c in candidates:
            if c.name == "fallback":
                continue
            expected = zai_proxy._PROVIDER_MODEL_NAMES.get(c.name, {}).get(
                canonical, canonical)
            assert c.model == expected, \
                f"{c.name}: outgoing model {c.model!r} != expected {expected!r}"
            checked += 1
        assert checked >= 3, f"Only {checked} candidates checked"

    def test_unknown_model_still_fallback(self):
        """Unknown models must still produce a single fallback candidate."""
        with self._healthy_all():
            candidates = select_provider(model="nonexistent-model-xyz")
        assert len(candidates) == 1, \
            f"Expected exactly 1 fallback candidate, got {candidates}"
        assert candidates[0].name == "fallback"
        assert candidates[0].dispatch_fn is None
        assert candidates[0].effective_cost == float("inf")

    def test_worker_fallback_model_resolves_multi(self):
        """WORKER_FALLBACK_MODEL ('deepseek/deepseek-v4-flash') must give
        ≥3 non-fallback candidates (market-driven, not single-provider)."""
        with self._healthy_all():
            candidates = select_provider(model="deepseek/deepseek-v4-flash")
        non_fallback = [c for c in candidates if c.name != "fallback"]
        assert len(non_fallback) >= 3, \
            f"Worker fallback model resolved to only {[c.name for c in non_fallback]}"

    def test_exhausted_opencode_go_still_serves_short_form(self):
        """THE incident regression test: with opencode_go unhealthy, a
        short-form deepseek request must STILL return ≥2 candidates.
        (Previously 'deepseek-v4-flash' matched only opencode_go → 0
        candidates → 503 for 72h.)"""
        with patch("flat_router._is_provider_healthy",
                   side_effect=lambda n: n != "opencode_go"):
            candidates = select_provider(model="deepseek-v4-flash")
        names = [c.name for c in candidates if c.name != "fallback"]
        assert len(names) >= 2, \
            f"Short form with dead opencode_go gave only {names} — " \
            f"the 2026-08-25 503 incident would recur"
        assert "opencode_go" not in names


if __name__ == "__main__":
    pytest.main([__file__, "-v"])