#!/usr/bin/env python3
"""Fixture-based tests for zai_proxy response parsing edge cases.

Tests the critical response parsing logic that handles:
- Normal responses with content
- Empty content with reasoning_content (reasoning injection)
- finish_reason=length (truncated/truncated responses)
- Error responses (quota/auth errors)
- Streaming SSE with usage in final chunk
- Empty/error body detection

Run: python3 test_response_parsing.py
"""
import json
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))


# ── Test fixtures ────────────────────────────────────────────────────────────

FIXTURE_NORMAL = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "model": "glm-5.2",
    "choices": [{
        "index": 0,
        "message": {"role": "assistant", "content": "Hello! How can I help?"},
        "finish_reason": "stop",
    }],
    "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
}

FIXTURE_REASONING_ONLY = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "model": "glm-5.2",
    "choices": [{
        "index": 0,
        "message": {
            "role": "assistant",
            "content": "",
            "reasoning_content": "I need to think about this step by step...",
        },
        "finish_reason": "length",
    }],
    "usage": {"prompt_tokens": 100, "completion_tokens": 500, "total_tokens": 600},
}

FIXTURE_ERROR_QUOTA = {
    "error": {
        "code": "1303",
        "message": "quota exceeded",
    }
}

FIXTURE_STREAMING = (
    b'data: {"id":"1","choices":[{"delta":{"content":"Hi"}}]}\n'
    b'data: {"id":"1","choices":[{"delta":{"content":" there"}}]}\n'
    b'data: {"id":"1","choices":[{"delta":{},"finish_reason":"stop"}],'
    b'"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}\n'
    b'data: [DONE]\n'
)

FIXTURE_EMPTY = b""

FIXTURE_DONE_ONLY = b"data: [DONE]"


# ── Test helpers ─────────────────────────────────────────────────────────────

def _check_response_parsing(resp_bytes: bytes) -> dict:
    """Simulate the proxy's response parsing logic and return analysis."""
    result = {
        "is_empty": False,
        "is_error": False,
        "is_truncated": False,
        "reasoning_injected": False,
        "usage": {},
    }

    resp_text = resp_bytes.decode('utf-8', errors='ignore').strip()
    result["is_empty"] = not resp_text or resp_text == "data: [DONE]"

    if not result["is_empty"]:
        try:
            resp_json = json.loads(resp_text)
            if "error" in resp_json and "choices" not in resp_json:
                result["is_error"] = True
            else:
                choices = resp_json.get("choices", [])
                if choices:
                    msg_obj = choices[0].get("message", {})
                    content = msg_obj.get("content", "")
                    finish_reason = choices[0].get("finish_reason", "")
                    if finish_reason == "length":
                        result["is_truncated"] = True
                    if not content or not content.strip():
                        reasoning = msg_obj.get("reasoning_content", "")
                        if reasoning and reasoning.strip():
                            result["reasoning_injected"] = True
                            msg_obj["content"] = reasoning
                            result["patched"] = json.dumps(resp_json).encode()
                        else:
                            result["is_empty"] = True
        except Exception:
            pass

    return result


def _parse_usage(response_buffer: bytes) -> dict:
    """Replica of zai_proxy._parse_usage for testing."""
    if not response_buffer:
        return {}
    try:
        obj = json.loads(response_buffer)
        if isinstance(obj, dict) and isinstance(obj.get("usage"), dict):
            return obj["usage"]
    except Exception:
        pass
    try:
        for line in response_buffer.decode("utf-8", "ignore").splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]" or not payload:
                continue
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("usage"), dict):
                return obj["usage"]
    except Exception:
        pass
    return {}


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_normal_response():
    """Normal response with content is parsed correctly."""
    raw = json.dumps(FIXTURE_NORMAL).encode()
    result = _check_response_parsing(raw)
    assert not result["is_empty"], "Normal response should not be empty"
    assert not result["is_error"], "Normal response should not be error"
    assert not result["is_truncated"], "Normal response should not be truncated"
    assert not result["reasoning_injected"], "Normal response should not need injection"
    print("  PASS: Normal response parsed correctly")


def test_reasoning_injection():
    """Empty content with reasoning_content triggers injection."""
    raw = json.dumps(FIXTURE_REASONING_ONLY).encode()
    result = _check_response_parsing(raw)
    assert not result["is_empty"], "Reasoning-only should not be empty after injection"
    assert result["reasoning_injected"], "Reasoning should be injected into content"
    assert result["is_truncated"], "finish_reason=length should set truncated flag"
    patched = json.loads(result["patched"])
    assert patched["choices"][0]["message"]["content"] == "I need to think about this step by step..."
    print("  PASS: Reasoning injected into empty content")


def test_truncated_detection():
    """finish_reason=length is detected as truncated."""
    fixture = json.loads(json.dumps(FIXTURE_NORMAL))
    fixture["choices"][0]["finish_reason"] = "length"
    raw = json.dumps(fixture).encode()
    result = _check_response_parsing(raw)
    assert result["is_truncated"], "finish_reason=length should be detected"
    print("  PASS: Truncated response detected (finish_reason=length)")


def test_error_response():
    """Error responses (quota/auth) are detected."""
    raw = json.dumps(FIXTURE_ERROR_QUOTA).encode()
    result = _check_response_parsing(raw)
    assert result["is_error"], "Error response should be detected"
    print("  PASS: Error response detected")


def test_empty_body():
    """Empty response body is detected."""
    result = _check_response_parsing(FIXTURE_EMPTY)
    assert result["is_empty"], "Empty body should be detected"
    print("  PASS: Empty body detected")


def test_done_only():
    """data: [DONE] only body is detected as empty."""
    result = _check_response_parsing(FIXTURE_DONE_ONLY)
    assert result["is_empty"], "DONE-only should be detected as empty"
    print("  PASS: DONE-only body detected as empty")


def test_streaming_usage():
    """Usage is parsed from streaming SSE final chunk."""
    usage = _parse_usage(FIXTURE_STREAMING)
    assert usage.get("total_tokens") == 8, f"Expected 8 tokens, got {usage}"
    assert usage.get("prompt_tokens") == 5
    assert usage.get("completion_tokens") == 3
    print("  PASS: Streaming usage parsed from final chunk")


def test_non_streaming_usage():
    """Usage is parsed from non-streaming JSON."""
    raw = json.dumps(FIXTURE_NORMAL).encode()
    usage = _parse_usage(raw)
    assert usage.get("total_tokens") == 18, f"Expected 18 tokens, got {usage}"
    print("  PASS: Non-streaming usage parsed")


def test_spend_estimation():
    """Cost estimation works for known models."""
    import zai_proxy
    assert zai_proxy._estimate_cost_usd("glm-5.2", 1000000) == 0.0
    assert zai_proxy._estimate_cost_usd("glm-4.5-flash", 1000000) == 0.0
    cost = zai_proxy._estimate_cost_usd("deepseek/deepseek-v4-pro", 1000000)
    assert 1.29 <= cost <= 1.31, f"Expected ~$1.30, got ${cost}"
    cost_flash = zai_proxy._estimate_cost_usd("deepseek/deepseek-v4-flash", 1000000)
    assert 0.08 <= cost_flash <= 0.10, f"Expected ~$0.09, got ${cost_flash}"
    print("  PASS: Cost estimation correct for all models")


def test_spend_tier_classification():
    """Tier classification matches profile-aware failover."""
    import zai_proxy
    assert zai_proxy._spend_tier("glm-5.2") == "manager"
    assert zai_proxy._spend_tier("deepseek/deepseek-v4-pro") == "manager"
    assert zai_proxy._spend_tier("glm-4.5-flash") == "worker"
    assert zai_proxy._spend_tier("deepseek/deepseek-v4-flash") == "worker"
    print("  PASS: Tier classification correct (manager vs worker)")


# ── Runner ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_normal_response,
        test_reasoning_injection,
        test_truncated_detection,
        test_error_response,
        test_empty_body,
        test_done_only,
        test_streaming_usage,
        test_non_streaming_usage,
        test_spend_estimation,
        test_spend_tier_classification,
    ]
    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"  FAIL: {test.__name__}: {e}")
            failed += 1
    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed, {len(tests)} total")
    sys.exit(1 if failed else 0)
