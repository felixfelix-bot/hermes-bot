"""Shared hermeticity fixtures for the proxy/routing test suite.

The routing modules under test (``zai_proxy`` and ``flat_router``, plus the
``exhaust_weight`` integration path) read LIVE operator/runtime state at call
time from the real home directory (``~/.hermes/bot/``):

  * manual-disable flag files        (``.key_disabled_<name>``)
  * ollama paywall flag files        (``.ollama_exhausted_until*``)
  * the quota snapshot               (``zai_proxy_state.json``)
  * the in-memory quota/pacing cache (``zai_proxy.quota_cache``)

All of these are resolved through ``Path.home()`` (e.g.
``zai_proxy._is_key_healthy`` checks ``(Path.home()/".hermes"/"bot"/f".key_disabled_{name}").exists()``
directly, and ``zai_proxy.STATE_FILE = Path.home()/".hermes"/"bot"/"zai_proxy_state.json"``).
On a development host these files carry the live proxy's *current* state
(keys disabled, a paywalled quota, a locked weekly window), so any test that
exercises key-health / provider-ordering walks straight into that ambient
state and its result drifts with the host instead of the code under test. On
a clean machine (no state files) the same tests pass.

To make the suite hermetic we redirect ``Path.home()`` to a fresh, empty temp
directory for the *duration of each test*, so every ambient read resolves to
a clean baseline ("clean host"). The modules are all already imported by the
time any fixture runs, so this only affects runtime reads — never the
collection-time imports that need real ``.env``/keys. Per-test
``patch.object`` / ``monkeypatch`` on the same symbols simply override this
baseline, so existing tests that set their own state keep working unchanged.
"""

import pathlib

import pytest


@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path, monkeypatch):
    """Redirect ``Path.home()`` to a fresh, empty temp dir per test.

    The proxy tri-state lives under the real ``$HOME/.hermes/bot/``. Every
    ambient read (manual-disable flags, paywall flags, quota snapshot, kvstore)
    funnels through ``Path.home()``. Redirecting that one function gives every
    test a deterministic clean-host baseline regardless of live operator state.

    ``tmp_path`` is unique per test, so two tests can never observe each
    other's ``.key_disabled_*`` or quota cache.
    """
    clean_home = tmp_path / "hermetic_home"
    clean_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(pathlib.Path, "home",
                        staticmethod(lambda: clean_home))

    # Empty in-memory quota/pacing cache too — no live weekly-locked windows
    # should entrain into provider-ordering tests.
    import zai_proxy as z  # noqa: F401
    monkeypatch.setattr(z, "quota_cache", {})
    monkeypatch.setattr(z, "_ollama_quota_cache", {})
    monkeypatch.setattr(z, "_ollama_quota_cache_ts", {})