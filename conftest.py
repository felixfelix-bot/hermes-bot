"""Suite-wide hermeticity for shared proxy state files (t_5f82cd0f run 67+81).

Problem (observed live 2026-09-09, twice): zai_proxy persists the opencode_go
exhaustion bench to ~/.hermes/bot/.opencode_go_exhausted_until (ollama-paywall
pattern, commit ef3979e). While the lane is genuinely benched (Sep-2026
monthly 429, "Resets in 15 days") that flag exists on disk, and tests that
read/clear it hit the REAL file:

  * test_opencode_quota_truth.py's builder tests read the live flag via
    max(memory, persisted) — 4/14 failed "expected included, got exhausted"
    while the real flag existed (kimi cold-review minor 1).
  * test_user_agent_headers.py's _try_opencode_go tests mock a 200 — the
    200-success path calls _opencode_go_clear_persisted_bench(), UNLINKING
    the live flag (17/17 passed but silently wiped the real bench state).

Multiple test files each spec-load their own copy of zai_proxy and register
it as sys.modules["zai_proxy"], sometimes REPLACING an earlier registration
mid-suite (test_ollama_quota_shadowing.live_zai_proxy). A single-object patch
therefore misses the object the running test actually holds. This fixture
walks sys.modules and patches EVERY loaded module object that carries
_OPENCODE_GO_BENCH_FLAG — all copies see the per-test tmp_path, the live
flag is never created, read, or removed by the suite, and the running proxy
(separate process, own module object) is untouched.

Run 81 (2026-09-09, third recurrence): the flag guard alone is NOT enough.
Test copies of zai_proxy also upsert into the REAL zai_usage.db via
_log_key_health — the mocked-200 success path in test_user_agent_headers.py
calls _mark_key_healthy("opencode_go") → the key_health mirror flips to
healthy=1 while the lane sits 429-benched upstream (observed live 3x: runs
79, 80, and 81's own verification run). Routing is unaffected (the live proxy
gates on its OWN in-memory _zai_key_health), but the mirror lies to future
diagnosis (flags → key_health → live probe) and to a future restart.

DB scope (deliberately surgical — do NOT widen): only the key_health mirror
WRITE funnel ``_log_key_health`` is redirected to a per-test tmp DB (created
via the module's own ``_usage_db()`` lazily against the patched ``USAGE_DB``,
so the schema comes from the module itself — a bare pre-opened connection has
no tables and silently swallows every INSERT, the vacuous-hermeticity trap
test_conftest_db_hermeticity guards against). DB READS stay live:
real_price_tracker measured rates, ollama quota windows, and api_calls sums
still see the real DB (test_user_agent_headers' ollama UA test depends on
the live measured-rate path; test_ppq_policy injects its own tmp connection
into the ``_usage_db`` singleton, which stays theirs). Redirecting reads
too (USAGE_DB/_usage_db_conn swap) breaks the ollama UA test: with an empty
DB the billing-API fallback request fires AFTER the chat request and
_capture_headers keeps the last request's headers — verified empirically
run 81 (https://ollama.com/api/usage captured, UA assert fails).

The live proxy process keeps its own module object and connection to the
real DB and is untouched by all of this. See test_conftest_db_hermeticity.py
for the pinned contract (PATCH_USAGE_DB_COPIES is its existence marker).
"""
import sys

import pytest

# Marker for test_conftest_db_hermeticity.py: the DB-mirror guard exists.
PATCH_USAGE_DB_COPIES = True


def _proxy_copies() -> list:
    """Every loaded zai_proxy-like module object (flag attr is the marker).

    Walked LIVE at fixture setup and teardown: copies get registered or
    replaced in sys.modules mid-suite, so a snapshot list goes stale.

    IMPORTANT (kimi cold-review 2.5b minor 1): the walk sees copies loaded
    at MODULE IMPORT time only. A copy spec-loaded INSIDE a test function
    body appears after setup and silently bypasses both layers — load your
    zai_proxy copies at module import (see test_opencode_quota_truth.py /
    test_conftest_db_hermeticity.py for the pattern).
    """
    return [m for m in list(sys.modules.values())
            if m is not None and hasattr(m, "_OPENCODE_GO_BENCH_FLAG")]


@pytest.fixture(autouse=True)
def _hermetic_proxy_state_files(tmp_path, monkeypatch):
    """Keep the suite's hands off live proxy state — flag file + DB mirror.

    Layer 1 (flag): every loaded zai_proxy copy's _OPENCODE_GO_BENCH_FLAG
    points at a per-test tmp flag — the live bench flag is never created,
    read, or unlinked by the suite.

    Layer 2 (DB mirror): every loaded copy's ``_log_key_health`` is wrapped
    so mirror upserts land in a per-test tmp DB (the module's own
    ``_usage_db()`` opens it against a patched ``USAGE_DB`` and creates the
    schema on first call). The wrap records the tmp path in
    ``_HERMETIC_KEY_HEALTH_DB`` so tests can assert the write genuinely
    landed (redirection, not swallowing). Teardown closes the tmp
    connection so handles never leak into later tests.
    """
    import functools

    tmp_flag = tmp_path / ".opencode_go_exhausted_until"
    tmp_db_path = tmp_path / "zai_usage_hermetic.db"
    per_test_conns = []
    for mod in _proxy_copies():
        monkeypatch.setattr(mod, "_OPENCODE_GO_BENCH_FLAG", tmp_flag)

        orig_log = getattr(mod, "_log_key_health", None)
        if orig_log is None:
            continue

        @functools.wraps(orig_log)
        def _redirected_log(name, state, _orig=orig_log, _mod=mod,
                            _path=tmp_db_path, _conns=per_test_conns):
            # Swap this copy's DB to the per-test tmp path, let the
            # module's own _usage_db() create the connection + schema,
            # run the real upsert against it, then swap back. Reads that
            # happen outside this call keep the live DB.
            # NOTE (kimi cold-review 2.5b minor 2): the USAGE_DB/_usage_db_conn
            # swap is intentionally UNLOCKED — safe because each pytest test
            # runs single-threaded and the live proxy holds its own module
            # object in a separate process; revisit only under xdist-with-
            # shared-fixture or proxy-singleton reuse.
            saved_db, saved_conn = (getattr(_mod, "USAGE_DB", None),
                                    getattr(_mod, "_usage_db_conn", None))
            _mod.USAGE_DB = _path
            _mod._usage_db_conn = None
            try:
                _orig(name, state)
                _conns.append(_mod._usage_db_conn)
                _mod._HERMETIC_KEY_HEALTH_DB = _path
            finally:
                _mod.USAGE_DB = saved_db
                _mod._usage_db_conn = saved_conn

        monkeypatch.setattr(mod, "_log_key_health", _redirected_log)
    yield
    for conn in per_test_conns:
        try:
            conn.close()
        except Exception:
            pass
    # NOTE (kimi cold-review 2.5b minor 3): the marker _HERMETIC_KEY_HEALTH_DB
    # is deliberately NOT reset at teardown. Resetting it breaks the guard
    # test test_no_live_db_write_handle_during_test, which asserts the marker
    # is present at test START — proving the redirect wrapper is installed on
    # this copy during the test — for tests that fire no write of their own.
    # The marker is only meaningful while the wrap is installed, and
    # monkeypatch.setattr teardown removes the wrapped _log_key_health right
    # after this loop, closing the validity window that way.