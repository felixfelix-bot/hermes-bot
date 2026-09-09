"""Tests for T3 no-op API-call drop (cost-reduction-sprint, t_66d2c01e).

Writes the failing test FIRST (Gate 1, TDD) against the drop predicate +
`_log_api_call` guard. The predicate and guard do not exist yet, so importing
`_is_noop_api_call` from zai_proxy fails → RED. After the implementation the
same assertions pass → GREEN.

The no-op signature (characterized from zai_usage.db, see
docs/noop-call-drop-2026-09-09.md, tightened per cold-review CHANGES_REQUESTED
2026-09-09): a row with `key_name IN ('ours','friend')` AND `total_tokens=0`
AND empty model AND no status_code AND no error. 37,461 such historical rows
(2026-08-14→23), 2-11 ms (never reached upstream). We drop ONLY this exact
signature so genuine failure/exhaustion telemetry (model-less bodies on the
503/404 paths with a real key, or non-z.ai providers) is never suppressed.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "src"))

# Pin the REPO zai_proxy copy so the deployed ~/.hermes/bot layout cannot
# shadow this worktree's copy under test (mirrors test_caller_class_gate.py).
import importlib.util as _ilu

for _mod_name in ("zai_proxy", "flat_router"):
    _mp = _REPO / f"{_mod_name}.py"
    _mspec = _ilu.spec_from_file_location(_mod_name, str(_mp))
    _mmod = _ilu.module_from_spec(_mspec)
    sys.modules[_mod_name] = _mmod
    _mspec.loader.exec_module(_mmod)

import zai_proxy  # noqa: E402 — bind the pinned repo module under test


@pytest.fixture()
def fake_usage_db(monkeypatch):
    """Redirect _usage_db() to a throwaway in-memory DB and capture inserts.

    _log_api_call() executes INSERTs on the connection returned by _usage_db().
    We point it at a fresh in-memory SQLite conn, create the api_calls table,
    and return (conn, rows) so tests can assert how many rows were written.
    """
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute(
        "CREATE TABLE api_calls (ts REAL, key_name TEXT, key_suffix TEXT, "
        "model TEXT, prompt_tokens INTEGER, completion_tokens INTEGER, "
        "total_tokens INTEGER, tier TEXT, cache_hit INTEGER, ollama_hit INTEGER, "
        "ppq_hit INTEGER, status_code INTEGER, error TEXT, duration_ms INTEGER, "
        "cost_usd REAL, cost_source TEXT, session_id TEXT, task_type TEXT)"
    )
    monkeypatch.setattr(
        "zai_proxy._usage_db", lambda: conn, raising=False
    )

    class _Capture:
        def rowcount(self):
            return conn.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0]

    return _Capture()


# ── Drop predicate (the pure decision) ──────────────────────────────────────

class TestNoopPredicate:
    def test_drop_ours_friend_empty_full_signature(self):
        """Audited no-op: ours/friend key, zero tokens, no model/status/error."""
        for key in ("ours", "friend"):
            assert zai_proxy._is_noop_api_call(
                key_name=key, model=None, status_code=None,
                error=None, total_tokens=0) is True

    def test_drop_empty_string_model(self):
        assert zai_proxy._is_noop_api_call(
            key_name="ours", model="", status_code=None,
            error="", total_tokens=0) is True

    def test_keep_non_zai_provider(self):
        """A non z.ai provider key is never touched even with no model/status."""
        assert zai_proxy._is_noop_api_call(
            key_name="deepseek", model=None, status_code=None,
            error=None, total_tokens=0) is False
        assert zai_proxy._is_noop_api_call(
            key_name="telnyx", model=None, status_code=None,
            error=None, total_tokens=0) is False

    def test_keep_any_nonzero_tokens(self):
        """token>0 proves an upstream round-trip happened — never drop."""
        assert zai_proxy._is_noop_api_call(
            key_name="ours", model=None, status_code=None,
            error=None, total_tokens=5) is False

    def test_keep_when_model_present(self):
        """A real call always carries a model — never dropped."""
        assert zai_proxy._is_noop_api_call(
            key_name="ours", model="glm-5.2", status_code=None,
            error=None, total_tokens=0) is False

    def test_keep_when_status_present_even_model_less(self):
        """Genuine diagnostic rows (503 exhaustion / 404, model-less body) KEPT.
        Cold review flagged this class as the over-broad-drop risk.
        """
        assert zai_proxy._is_noop_api_call(
            key_name="ours", model=None, status_code=503,
            error=None, total_tokens=0) is False
        assert zai_proxy._is_noop_api_call(
            key_name="friend", model=None, status_code=404,
            error=None, total_tokens=0) is False

    def test_keep_when_error_present(self):
        assert zai_proxy._is_noop_api_call(
            key_name="ours", model=None, status_code=None,
            error="client disconnect: BrokenPipeError", total_tokens=0) is False

    def test_status_zero_int_kept(self):
        assert zai_proxy._is_noop_api_call(
            key_name="ours", model=None, status_code=0,
            error=None, total_tokens=0) is False


# ── Logging chokepoint skips the insert for no-ops ─────────────────────────-

class TestLogApiCallGuard:
    def test_noop_call_writes_no_row(self, fake_usage_db):
        """A no-op signature must NOT insert an api_calls row."""
        zai_proxy._log_api_call(
            key_name="ours", model=None, status_code=None, error=None,
            total_tokens=0, prompt_tokens=0, completion_tokens=0, tier="zai",
            cost_usd=0.0, cost_source="flat_rate")
        assert fake_usage_db.rowcount() == 0

    def test_real_call_still_writes_row(self, fake_usage_db):
        """A genuine call (model + status + tokens) is unaffected."""
        zai_proxy._log_api_call(
            key_name="ours", model="glm-5.2", prompt_tokens=10,
            completion_tokens=20, total_tokens=30, tier="zai",
            status_code=200, error=None, cost_usd=0.0,
            cost_source="flat_rate", session_id="sess-1", task_type="coding")
        assert fake_usage_db.rowcount() == 1

    def test_real_failure_row_still_written(self, fake_usage_db):
        """Model present w/ an HTTP error = real failure, kept."""
        zai_proxy._log_api_call(
            key_name="friend", model="glm-5.2", total_tokens=0,
            prompt_tokens=0, completion_tokens=0, tier="zai",
            status_code=429, error="HTTPError 429", cost_usd=0.0,
            cost_source="estimated")
        assert fake_usage_db.rowcount() == 1

    def test_503_exhaustion_model_less_row_still_written(self, fake_usage_db):
        """Model-less 503 'all providers exhausted' diagnostic — kept (cold
        review: this is the failure-telemetry class that must never be dropped)."""
        zai_proxy._log_api_call(
            key_name="ours", model=None, status_code=503, error=None,
            total_tokens=0, prompt_tokens=0, completion_tokens=0, tier="zai",
            cost_usd=0.0, cost_source="flat_rate")
        assert fake_usage_db.rowcount() == 1

    def test_non_zai_provider_noop_kept(self, fake_usage_db):
        """A non-z.ai provider row with the empty signature is logged (their
        no-op signature is not covered by this audit's drop)."""
        zai_proxy._log_api_call(
            key_name="deepseek", model=None, status_code=None, error=None,
            total_tokens=0, prompt_tokens=0, completion_tokens=0, tier="deepseek",
            cost_usd=0.0, cost_source="estimated")
        assert fake_usage_db.rowcount() == 1
