"""Suite-wide hermeticity for shared proxy state files (t_5f82cd0f run 67).

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
"""
import sys

import pytest


@pytest.fixture(autouse=True)
def _hermetic_opencode_bench_flag(tmp_path, monkeypatch):
    """Point every loaded zai_proxy copy's opencode_go bench flag at tmp_path."""
    tmp_flag = tmp_path / ".opencode_go_exhausted_until"
    patched = 0
    for mod in list(sys.modules.values()):
        if mod is not None and hasattr(mod, "_OPENCODE_GO_BENCH_FLAG"):
            monkeypatch.setattr(mod, "_OPENCODE_GO_BENCH_FLAG", tmp_flag)
            patched += 1
    yield