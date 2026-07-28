#!/usr/bin/env python3
"""Tests for zai_proxy._classify_response — the provider-telemetry response
classifier.

This covers the logic that decides each row's response_valid / error_type in
the `provider_telemetry` table. It was untested before P3.7; the SSE fix
(commit 6a99a41) lived inline in do_POST's `finally` block.

The key regression: a non-JSON body arriving WITH a known `error_text`
(e.g. a DNS/connection failure that writes a plain-text
``proxy error: <urlopen error ...>`` body) must be reported as that
error_text, NOT clobbered with a generic 'parse_error'. Before P3.7 the
inline code always set 'parse_error' for any unparseable body, hiding two
real network failures (id=2054, id=2241) inside the parse_error bucket.

Run: python3 test_telemetry_classification.py
"""
import json
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))


def _classify():
    """Lazy import — zai_proxy has import-time side effects (DB, LiveRouter)."""
    import zai_proxy
    return zai_proxy._classify_response


# ── Fixtures ─────────────────────────────────────────────────────────────────

JSON_WITH_CHOICES = json.dumps({
    "id": "x", "object": "chat.completion", "model": "glm-5.2",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
}).encode()

JSON_ERROR = json.dumps({"error": {"code": "1303", "message": "quota exceeded"}}).encode()

JSON_NO_CHOICES_NO_ERROR = json.dumps({"id": "x", "created": 1}).encode()

SSE_WITH_CHOICES = (
    b'data: {"id":"1","choices":[{"delta":{"content":"Hi"}}]}\n'
    b'data: {"id":"1","choices":[{"delta":{},"finish_reason":"stop"}],'
    b'"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}\n'
    b'data: [DONE]\n'
)

SSE_WITH_ERROR = (
    b'data: {"error":{"message":"model overloaded","code":"internal"}}\n'
    b'data: [DONE]\n'
)

SSE_DONE_ONLY = b"data: [DONE]\n"

PLAINTEXT_PROXY_ERROR = b"proxy error: <urlopen error [Errno -3] Temporary failure in name resolution>"

PLAINTEXT_GATEWAY = b"<html><body>502 Bad Gateway</body></html>"


# ── Tests ────────────────────────────────────────────────────────────────────

def test_valid_json_with_choices():
    f = _classify()
    recv, valid, etype = f(JSON_WITH_CHOICES, None)
    assert (recv, valid, etype) == (True, True, "none"), (recv, valid, etype)
    print("  PASS: valid JSON w/ choices -> received, valid, 'none'")


def test_valid_json_with_choices_ignores_error_text():
    # A genuine 200 success should be 'none' even if some stale error_text lingered.
    f = _classify()
    recv, valid, etype = f(JSON_WITH_CHOICES, "HTTPError 429")
    assert (recv, valid, etype) == (True, True, "none"), (recv, valid, etype)
    print("  PASS: valid JSON overrides any stale error_text -> 'none'")


def test_valid_sse_stream():
    f = _classify()
    recv, valid, etype = f(SSE_WITH_CHOICES, None)
    assert (recv, valid, etype) == (True, True, "none"), (recv, valid, etype)
    print("  PASS: valid SSE w/ choices line -> received, valid, 'none'")


def test_json_error_body():
    f = _classify()
    recv, valid, etype = f(JSON_ERROR, None)
    assert (recv, valid, etype) == (True, False, "api_error"), (recv, valid, etype)
    print("  PASS: JSON error body -> 'api_error'")


def test_sse_error_body():
    f = _classify()
    recv, valid, etype = f(SSE_WITH_ERROR, None)
    assert (recv, valid, etype) == (True, False, "api_error"), (recv, valid, etype)
    print("  PASS: SSE error body -> 'api_error'")


def test_json_no_choices_no_error():
    f = _classify()
    recv, valid, etype = f(JSON_NO_CHOICES_NO_ERROR, None)
    assert recv is True and valid is False and etype == "none", (recv, valid, etype)
    print("  PASS: JSON w/o choices/error -> received, not valid, 'none'")


def test_empty_buffer_no_error_text():
    f = _classify()
    recv, valid, etype = f(b"", None)
    assert (recv, valid, etype) == (False, False, "no_response"), (recv, valid, etype)
    print("  PASS: empty buffer, no error_text -> 'no_response'")


def test_empty_buffer_with_error_text():
    f = _classify()
    recv, valid, etype = f(b"", "HTTPError 502")
    assert (recv, valid, etype) == (False, False, "HTTPError 502"), (recv, valid, etype)
    print("  PASS: empty buffer w/ error_text -> error_text preserved")


def test_dns_failure_preserves_error_text():
    """REGRESSION (P3.7): the two production DNS failures (id=2054, id=2241)
    wrote a plain-text 'proxy error: ...' body and were mislabeled 'parse_error'.
    They must now surface the real connection error."""
    f = _classify()
    et = "proxy error: <urlopen error [Errno -3] Temporary failure in name resolution>"
    recv, valid, etype = f(PLAINTEXT_PROXY_ERROR, et)
    assert (recv, valid, etype) == (True, False, et), (recv, valid, etype)
    assert etype != "parse_error", "DNS/connection error must NOT be 'parse_error'"
    print("  PASS: plain-text proxy-error body preserves real error_text (not parse_error)")


def test_unparseable_200_is_parse_error():
    """A genuinely unparseable 200 body (non-JSON, non-SSE) with NO error_text
    stays 'parse_error' — that is a real provider anomaly worth flagging."""
    f = _classify()
    recv, valid, etype = f(PLAINTEXT_GATEWAY, None)
    assert (recv, valid, etype) == (True, False, "parse_error"), (recv, valid, etype)
    print("  PASS: unparseable 200 body (no error_text) -> 'parse_error'")


def test_done_only_no_error_text():
    # 'data: [DONE]' alone is non-empty but carries no choices/error payload.
    f = _classify()
    recv, valid, etype = f(SSE_DONE_ONLY, None)
    assert recv is True and valid is False, (recv, valid, etype)
    assert etype == "parse_error", (recv, valid, etype)
    print("  PASS: DONE-only body -> received, not valid, 'parse_error'")


def test_never_raises_on_garbage():
    f = _classify()
    for bad in (b"\xff\xfe\x00bad", b"{", b"data: {not json}\n", None):
        try:
            f(bad or b"", None)
        except Exception as e:  # noqa: BLE001
            raise AssertionError(f"_classify_response raised on {bad!r}: {e}")
    print("  PASS: never raises on garbage / None input")


# ── Runner ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_valid_json_with_choices,
        test_valid_json_with_choices_ignores_error_text,
        test_valid_sse_stream,
        test_json_error_body,
        test_sse_error_body,
        test_json_no_choices_no_error,
        test_empty_buffer_no_error_text,
        test_empty_buffer_with_error_text,
        test_dns_failure_preserves_error_text,
        test_unparseable_200_is_parse_error,
        test_done_only_no_error_text,
        test_never_raises_on_garbage,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"  FAIL: {t.__name__}: {e}")
            failed += 1
    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed, {len(tests)} total")
    sys.exit(1 if failed else 0)
