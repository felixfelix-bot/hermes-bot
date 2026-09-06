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

There are two distinct hermeticity layers, both handled here:

1. *Module-resolution pinning* (``pytest_configure``). ``flat_router``'s path
   bootstrap (``flat_router.py``) unconditionally prepends ``~/.hermes/bot``
   to ``sys.path`` the moment it is imported. Most test modules import
   ``flat_router`` *before* ``zai_proxy``, so a later ``import zai_proxy``
   would resolve to the **deployed live copy** ``~/.hermes/bot/zai_proxy.py``
   (which registers extra providers like ``ollama_cloud_4`` and reads the live
   state file) instead of the in-repo copy under test — making ordering/health
   tests drift with the deployed host. We pre-import the in-repo ``zai_proxy``
   (and ``flat_router``) at session start, before ``~/.hermes/bot`` is ever
   placed on ``sys.path``, so Python's ``sys.modules`` cache pins the *in-repo*
   modules for the whole session regardless of later path mutations.

2. *Runtime-state redirection* (``_hermetic_home`` autouse fixture). Redirect
   ``Path.home()`` to a fresh, empty temp dir for the duration of each test,
   so every ambient runtime read resolves to a clean baseline ("clean host").
   The modules are all already imported by the time any fixture runs, so this
   only affects runtime reads — never the collection-time imports that need
   real ``.env``/keys. Per-test ``patch.object`` / ``monkeypatch`` on the same
   symbols simply override this baseline, so existing tests that set their own
   state keep working unchanged.
"""

import pathlib

import pytest


def pytest_configure(config):
    """Pin the *in-repo* ``zai_proxy``/``flat_router`` modules at session start.

    Without this, the first test module to ``import flat_router`` runs its
    path bootstrap, which prepends ``~/.hermes/bot`` ahead of the repo root on
    ``sys.path``. The next ``import zai_proxy`` then resolves to the deployed
    live proxy (``~/.hermes/bot/zai_proxy.py``, with extra providers and live
    state) instead of the copy under test — failing the oc3 / exhaust-weight
    suites only on hosts that carry that ambient state. Importing the in-repo
    modules here (while the repo root is the only relevant path entry) caches
    them in ``sys.modules`` so no later import re-resolves them.

    Must be done before any test module is collected, so it cannot live in the
    per-test fixture below.
    """
    # ensure the repo root + its src/ are importable regardless of CWD
    root = pathlib.Path(__file__).resolve().parent.parent
    for p in (root, root / "src"):
        s = str(p)
        if s not in __import__("sys").path:
            __import__("sys").path.insert(0, s)
    # NOTE: order matters — import zai_proxy FIRST (while the repo root is the
    # only relevant path entry) so it binds the in-repo module. Then import
    # flat_router, whose path bootstrap prepends ~/.hermes/bot; that cannot
    # re-resolve zai_proxy because it is already cached in sys.modules.
    import zai_proxy  # noqa: F401  (pins the in-repo module in sys.modules)
    import flat_router  # noqa: F401  (path bootstrap runs; harmless once pinned)


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
