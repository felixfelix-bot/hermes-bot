#!/usr/bin/env python3
"""Regression test for the token-audit false-positive bug.

BUG (Phase 2.5.4): the provider-telemetry block in zai_proxy._proxy() passed
``usage["total_tokens"]`` (prompt + completion) into ``_audit_token_count`` as
the "billed" count.  But ``_audit_token_count`` estimates ``actual_tokens`` from
``len(response_buffer) // 4`` — and the response buffer contains ONLY the
completion text (the prompt is never echoed back by the upstream API).  So the
billed count (prompt + completion) was always much larger than the
completion-only estimate, which guarantees a spurious >20% "billing mismatch"
on any request whose prompt is more than ~25% of the total.  In other words:
the audit compared ``total_tokens`` against ``completion_tokens`` and reported a
false positive almost every time.

FIX: the call site now passes ``usage["completion_tokens"]``, which is the
correct apples-to-apples comparison against the completion-only buffer
estimate.

These tests:
  * Reproduce the false positive with the OLD field (total_tokens).
  * Prove the fix (completion_tokens) yields no mismatch for the same payload.
  * Cover the realistic prompt-heavy scenario that triggered the soak-test alert.
  * Exercise the inline never-raising fallback stub in isolation.

Run: python3 test_token_audit_false_positive.py   OR   pytest -q test_token_audit_false_positive.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ── Inline fallback stub (copied verbatim from zai_proxy.py) ──────────────────
# Tested in isolation so the fallback path (used if the src import fails) keeps
# the same contract as the real src.token_audit.audit_token_count.
def _fallback_audit_token_count(billed_tokens, response_buffer, threshold=0.20):
    try:
        _buf = response_buffer if response_buffer is not None else b""
        _actual = len(_buf) // 4
        _billed = int(billed_tokens or 0)
        if _billed <= 0 or _actual <= 0:
            return (_actual, False, 0.0)
        _rate = abs(_billed - _actual) / max(_billed, 1)
        return (_actual, _rate > threshold, _rate)
    except Exception:
        return (0, False, 0.0)


def _audit_from_zai_proxy():
    """Return the production audit function actually wired into zai_proxy.

    In a normal runtime this is src.token_audit.audit_token_count (imported at
    module load).  We import lazily so the test does not pay the import cost if
    the caller only wants the stub tests.
    """
    import zai_proxy  # noqa: F401  (triggers the src import side-effect)
    return zai_proxy._audit_token_count


# ── Scenarios ─────────────────────────────────────────────────────────────────
# A response buffer whose length corresponds to ~300 completion tokens
# (1200 bytes / 4 = 300).  This is the completion text the upstream echoes back.
COMPLETION_BYTES = b"x" * 1200          # ≈ 300 tokens of completion content

# Realistic usage object for a prompt-heavy coding request:
#   prompt_tokens     = 2000   (large context — never present in the response body)
#   completion_tokens = 300    (what the buffer above represents)
#   total_tokens      = 2300   (prompt + completion)
USAGE_PROMPT_HEAVY = {
    "prompt_tokens": 2000,
    "completion_tokens": 300,
    "total_tokens": 2300,
}


def test_old_total_tokens_field_was_a_false_positive():
    """Reproduce the bug: passing total_tokens against a completion-only buffer
    falsely flags a >20% billing mismatch even though the provider billed
    exactly the right number of COMPLETION tokens."""
    audit = _audit_from_zai_proxy()
    billed_total = int(USAGE_PROMPT_HEAVY["total_tokens"])  # the OLD call site
    actual, mismatch, rate = audit(billed_total, COMPLETION_BYTES)
    assert actual == 300                      # buffer estimate ≈ completion tokens
    assert mismatch is True, "expected the OLD behaviour to false-positive"
    assert rate > 0.20


def test_fix_completion_tokens_no_mismatch():
    """The fix: passing completion_tokens yields no mismatch for the same
    payload — provider billed 300 completion tokens, buffer estimates ~300."""
    audit = _audit_from_zai_proxy()
    billed_completion = int(USAGE_PROMPT_HEAVY["completion_tokens"])  # NEW call site
    actual, mismatch, rate = audit(billed_completion, COMPLETION_BYTES)
    assert actual == 300
    assert mismatch is False, (
        f"completion_tokens ({billed_completion}) vs actual ({actual}) must not "
        f"flag — got mismatch={mismatch} rate={rate:.2f}"
    )
    assert rate == 0.0


def test_completion_path_still_catches_real_overbilling():
    """The fix must not blind the audit: if a provider genuinely over-bills the
    completion, completion_tokens still trips the threshold."""
    audit = _audit_from_zai_proxy()
    # Provider claims 1000 completion tokens but only ~300 worth of text arrived.
    actual, mismatch, rate = audit(1000, COMPLETION_BYTES)
    assert actual == 300
    assert mismatch is True
    assert rate > 0.20


def test_genuine_mismatch_on_completion_still_detected():
    """A real silent-downgrade: provider bills 600 completion tokens for a
    buffer that only holds ~300 tokens of content."""
    audit = _audit_from_zai_proxy()
    actual, mismatch, rate = audit(600, COMPLETION_BYTES)
    assert actual == 300
    assert mismatch is True
    assert rate == 0.5


def test_zero_completion_tokens_no_false_positive():
    """Free z.ai subscription path: completion_tokens may be 0/absent.  The
    audit must return mismatch=False, not crash, not flag."""
    audit = _audit_from_zai_proxy()
    actual, mismatch, rate = audit(0, COMPLETION_BYTES)
    assert actual == 300
    assert mismatch is False
    assert rate == 0.0


# ── Fallback-stub parity ──────────────────────────────────────────────────────
def test_fallback_stub_matches_real_function_on_no_mismatch():
    """The inline fallback (used if the src import fails) must agree with the
    real function on the fixed (completion_tokens) path."""
    real = _audit_from_zai_proxy()
    billed = USAGE_PROMPT_HEAVY["completion_tokens"]
    assert real(billed, COMPLETION_BYTES) == _fallback_audit_token_count(
        billed, COMPLETION_BYTES
    )


def test_fallback_stub_never_raises_on_garbage():
    """The fallback path must never raise, mirroring the production contract."""
    assert _fallback_audit_token_count("junk", None) == (0, False, 0.0)
    assert _fallback_audit_token_count(100, None) == (0, False, 0.0)
    # None billed with a valid buffer: actual is estimated (100), but billed<=0
    # short-circuits to mismatch=False — the point is no crash, no false flag.
    _r = _fallback_audit_token_count(None, b"x" * 400)
    assert _r[0] == 100 and _r[1] is False and _r[2] == 0.0
    # Garbage buffer with no __len__ → swallowed
    assert _fallback_audit_token_count(100, 12345)[1] is False


# ── Runner ────────────────────────────────────────────────────────────────────
_TESTS = [
    test_old_total_tokens_field_was_a_false_positive,
    test_fix_completion_tokens_no_mismatch,
    test_completion_path_still_catches_real_overbilling,
    test_genuine_mismatch_on_completion_still_detected,
    test_zero_completion_tokens_no_false_positive,
    test_fallback_stub_matches_real_function_on_no_mismatch,
    test_fallback_stub_never_raises_on_garbage,
]


if __name__ == "__main__":
    passed = 0
    failed = 0
    for test in _TESTS:
        try:
            test()
            print(f"  PASS: {test.__name__}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL: {test.__name__}: {exc!r}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
