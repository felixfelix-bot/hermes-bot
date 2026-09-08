#!/usr/bin/env python3
"""TDD test for the PYTHONPATH shadowing fix (t_52763d41).

The proxy runs zai_proxy.py from ~/.hermes/bot but its path bootstrap inserts
~/merchant-routing-engine at sys.path[0] BEFORE importing src.ollama_quota_tracker.
That makes `src.ollama_quota_tracker` resolve to the merchant-routing-engine copy,
which LACKS DEFAULT_MONTHLY_LIMIT -> startup log
"[ollama_quota] DISABLED — cannot import name 'DEFAULT_MONTHLY_LIMIT'".

FAILING-FIRST: this test imports the LIVE zai_proxy under the proxy's runtime
path (MRE at sys.path[0]) and asserts the ollama_quota_tracker resolved to the
BOT's own copy (which HAS DEFAULT_MONTHLY_LIMIT). Before the fix, the import
resolves to the MRE copy, the except branch fires, and `_get_quota_status` is
None -> the test FAILS. After the fix, it resolves to the bot copy and the test
PASSES.
"""
import importlib.util as _ilu
import os
import sys

import pytest

BOT = os.path.expanduser("~/.hermes/bot")
MRE = os.path.expanduser("~/merchant-routing-engine")
BOT_OQT = os.path.join(BOT, "src", "ollama_quota_tracker.py")
MRE_OQT = os.path.join(MRE, "src", "ollama_quota_tracker.py")


@pytest.fixture(scope="module")
def live_zai_proxy():
    """Load the LIVE ~/.hermes/bot/zai_proxy.py under the proxy's runtime path
    (MRE at sys.path[0], mirroring the systemd ExecStart + path bootstrap)."""
    # Reproduce the proxy's path bootstrap: MRE first, then bot.
    for p in [MRE, BOT]:
        if p not in sys.path:
            sys.path.insert(0, p)
    spec = _ilu.spec_from_file_location("zai_proxy", os.path.join(BOT, "zai_proxy.py"))
    mod = _ilu.module_from_spec(spec)
    sys.modules["zai_proxy"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_bot_copy_has_default_monthly_limit():
    """The bot's own copy defines DEFAULT_MONTHLY_LIMIT (line 41)."""
    assert os.path.exists(BOT_OQT), f"bot copy missing: {BOT_OQT}"
    spec = _ilu.spec_from_file_location("_bot_oqt", BOT_OQT)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "DEFAULT_MONTHLY_LIMIT")
    assert mod.DEFAULT_MONTHLY_LIMIT == 3_500_000_000


def test_mre_copy_lacks_default_monthly_limit():
    """The merchant-routing-engine copy lacks the symbol — the shadowing hazard
    that caused the DISABLED log."""
    assert os.path.exists(MRE_OQT), f"MRE copy missing: {MRE_OQT}"
    spec = _ilu.spec_from_file_location("_mre_oqt", MRE_OQT)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert not hasattr(mod, "DEFAULT_MONTHLY_LIMIT")


def test_proxy_ollama_quota_tracker_resolves_to_bot_copy(live_zai_proxy):
    """The proxy's startup import of src.ollama_quota_tracker must resolve to the
    BOT's own copy (which has DEFAULT_MONTHLY_LIMIT), not the MRE copy. Before the
    fix this resolves to MRE, the except branch fires, and _get_quota_status is
    None -> FAILS."""
    assert live_zai_proxy._get_quota_status is not None, (
        "[ollama_quota] DISABLED — src.ollama_quota_tracker resolved to the "
        "merchant-routing-engine copy (lacks DEFAULT_MONTHLY_LIMIT)"
    )
    assert live_zai_proxy._OC_MONTHLY_LIMIT == 3_500_000_000
    assert live_zai_proxy._OC_SESSION_LIMIT == 500_000_000
    assert live_zai_proxy._oc_load_limits is not None
