#!/usr/bin/env python3
"""test_dead_code_cleanup.py — t_1bd8747e dead-code cleanup guards.

From the 2026-08-15 code-read verdict:

  (1) ``_can_proactive_switch`` is DEAD CODE — zero callers anywhere in the
      repo.  It was removed together with its orphaned constant
      ``_PROACTIVE_COOLDOWN_SECONDS`` (referenced only inside the dead
      function's docstring, never in executable code).  The tests in
      TestDeadCodeRemoved pin that removal — reintroduction fails CI.

  (2) The cleanup must NOT take collateral survivors with it:
      ``_proactive_switch_state`` is still read by the /status handler's
      ``proactive_cooldown`` block (zai_proxy.py ~:4167), and
      ``_PROACTIVE_PREDICTION_TTL`` drives the prediction-cache TTL check in
      ``_fetch_predictions``.  TestCollateralSurvivors guards both.

  (3) Module import + public surface must be unchanged by the removal —
      TestNoImportOrBehaviorChange proves the proxy still imports cleanly
      and exposes the same callables.

Run: python3 -m pytest tests/test_dead_code_cleanup.py -v
"""
from __future__ import annotations

import os
import sys
import unittest

# Bootstrap import path (zai_proxy lives in ~/.hermes/bot)
sys.path.insert(0, os.path.expanduser("~/.hermes/bot"))

import zai_proxy as z  # noqa: E402


class TestDeadCodeRemoved(unittest.TestCase):
    """(1) The dead function and its orphaned constant stay removed."""

    def test_can_proactive_switch_is_removed(self):
        self.assertFalse(
            hasattr(z, "_can_proactive_switch"),
            "_can_proactive_switch is dead code (zero callers, verified in "
            "the 2026-08-15 code-read) and must stay removed")

    def test_proactive_cooldown_constant_is_removed(self):
        self.assertFalse(
            hasattr(z, "_PROACTIVE_COOLDOWN_SECONDS"),
            "_PROACTIVE_COOLDOWN_SECONDS was referenced only by the dead "
            "_can_proactive_switch docstring; it must stay removed")


class TestCollateralSurvivors(unittest.TestCase):
    """(2) Cleanup must NOT remove symbols other live code still uses."""

    def test_proactive_switch_state_retained_for_status_endpoint(self):
        # Read by the /status handler's "proactive_cooldown" block.
        self.assertTrue(
            hasattr(z, "_proactive_switch_state"),
            "_proactive_switch_state is still read by the /status handler; "
            "removing it would change the status endpoint's output")

    def test_proactive_prediction_ttl_retained_for_cache(self):
        # Drives the prediction cache TTL check in _fetch_predictions.
        self.assertTrue(
            hasattr(z, "_PROACTIVE_PREDICTION_TTL"),
            "_PROACTIVE_PREDICTION_TTL gates the prediction cache; keep it")


class TestNoImportOrBehaviorChange(unittest.TestCase):
    """(3) Import succeeds and the surrounding surface is intact."""

    def test_module_imports_and_neighbors_callable(self):
        # The immediate neighbors of the removed function must all survive.
        self.assertTrue(callable(z._will_exhaust))
        self.assertTrue(callable(z._get_cached_predictions))
        self.assertTrue(callable(z._usage_db))


if __name__ == "__main__":
    unittest.main(verbosity=2)
