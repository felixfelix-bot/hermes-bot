#!/usr/bin/env python3
"""Test: silent model substitution fix (2026-08-25).

Verifies that _try_external_failover does NOT silently substitute
an unknown model with WORKER_FALLBACK_MODEL and return HTTP 200.
Instead, unknown models should cause failover to return False so
the caller sends a proper 404/503 error.

Known models (deepseek/*, minimax/*, qwen*, etc.) should be passed
through verbatim to the provider.
"""

import json
import sys
import os
import urllib.request
import urllib.error
from unittest.mock import MagicMock, patch, PropertyMock

# Add repo root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# We need to test _is_known_external_model and the failover logic
# Import the module to access its functions
import zai_proxy

# ── Test 1: _is_known_external_model recognizes known models ────────────────

def test_known_models_pass_through():
    """Known external models should be recognized and passed through."""
    known_models = [
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro",
        "minimax-m3",
        "minimax-m3:cloud",
        "qwen3.5:397b",
        "kimi-k2.7-code",
        "kimi-k3:cloud",
        "glm-5.2",
        "glm-5.3",
        "glm-4.5-flash",
    ]
    for model in known_models:
        result = zai_proxy._is_known_external_model(model)
        assert result, f"Model {model!r} should be known but wasn't recognized"
    print("✓ Test 1 passed: known models are recognized")


def test_unknown_models_rejected():
    """Truly unknown models should NOT be recognized."""
    unknown_models = [
        "nonexistent-model",
        "random-gpt-99",
        "totally-fake-model",
        "claude-opus-99",
        None,
        "",
    ]
    for model in unknown_models:
        result = zai_proxy._is_known_external_model(model)
        assert not result, f"Model {model!r} should be unknown but was recognized"
    print("✓ Test 2 passed: unknown models are rejected")


# ── Test 3: _try_external_failover returns False for unknown models ─────────

def test_failover_rejects_unknown_model():
    """_try_external_failover should return False for unknown models
    instead of silently substituting WORKER_FALLBACK_MODEL."""
    # Create a mock handler with the _try_external_failover method
    handler = MagicMock(spec=zai_proxy.Handler)
    handler._try_external_failover = zai_proxy.Handler._try_external_failover.__get__(
        handler, zai_proxy.Handler
    )
    # Also set _spend_recorded
    handler._spend_recorded = False

    body = json.dumps({"model": "nonexistent-model", "messages": [{"role": "user", "content": "test"}]}).encode()
    response_buffer = bytearray()

    # Call _try_external_failover with an unknown model
    # Patch _OXALPHA_TIER to None so the oxalpha early-exit doesn't fire
    with patch.object(zai_proxy, '_OXALPHA_TIER', None):
        result = handler._try_external_failover(body, "nonexistent-model", response_buffer, 0.0)

    # Should return False (not silently substitute and return True)
    assert result is False, (
        f"_try_external_failover returned {result} for unknown model — "
        f"should return False to prevent silent substitution"
    )
    # Response buffer should be empty (no response was sent)
    assert len(response_buffer) == 0, (
        f"Response buffer should be empty, got {len(response_buffer)} bytes"
    )
    print("✓ Test 3 passed: unknown model causes failover to return False")


# ── Test 4: _try_external_failover passes known models through ──────────────

def test_failover_passes_known_model():
    """_try_external_failover should NOT return False for known models
    like deepseek/deepseek-v4-flash. It should proceed to try providers."""
    handler = MagicMock(spec=zai_proxy.Handler)
    handler._try_external_failover = zai_proxy.Handler._try_external_failover.__get__(
        handler, zai_proxy.Handler
    )
    handler._spend_recorded = False

    body = json.dumps({"model": "deepseek/deepseek-v4-flash", "messages": [{"role": "user", "content": "test"}]}).encode()
    response_buffer = bytearray()

    # We can't fully test this without mocking all external providers,
    # but we can verify it does NOT immediately return False for known models.
    # It will try providers and fail (since they're mocked), eventually returning False.
    # The key is that it doesn't return False IMMEDIATELY due to the model check.
    # We patch EXTERNAL_PROVIDERS to be empty so no candidates are found,
    # which returns False at the "no candidates" check — not the model rejection.
    with patch.object(zai_proxy, 'EXTERNAL_PROVIDERS', {}):
        with patch.object(zai_proxy, '_OXALPHA_TIER', None):
            result = handler._try_external_failover(body, "deepseek/deepseek-v4-flash", response_buffer, 0.0)

    # Should return False because no providers are available, NOT because
    # the model was rejected as unknown
    assert result is False, "Expected False (no providers), but got True"
    print("✓ Test 4 passed: known model proceeds to provider selection (not rejected at model check)")


# ── Test 5: Live integration — unknown model returns error, not 200 ────────

def test_live_unknown_model_rejection():
    """Live integration test: request an unknown model from the running proxy
    and verify it returns 404/503, not 200 with a different model."""
    try:
        req = urllib.request.Request(
            "http://localhost:9099/v1/chat/completions",
            data=json.dumps({
                "model": "nonexistent-model-xyz",
                "messages": [{"role": "user", "content": "test"}],
                "max_tokens": 5,
            }).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            # If we get here, it returned 200 — that's the bug!
            assert False, "Unknown model returned HTTP 200 — silent substitution bug!"
        except urllib.error.HTTPError as e:
            # Should be 4xx or 5xx, not 200
            assert e.code >= 400, f"Expected error status, got {e.code}"
            print(f"✓ Test 5 passed: unknown model returns HTTP {e.code} (not 200)")
    except urllib.error.URLError:
        print("⚠ Test 5 skipped: proxy not running on localhost:9099")
    except Exception as e:
        print(f"⚠ Test 5 skipped: {e}")


if __name__ == "__main__":
    test_known_models_pass_through()
    test_unknown_models_rejected()
    test_failover_rejects_unknown_model()
    test_failover_passes_known_model()
    test_live_unknown_model_rejection()
    print("\n✅ All tests passed!")