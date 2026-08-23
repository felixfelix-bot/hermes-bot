#!/usr/bin/env python3
"""test_shadow_drop_ours.py — S3b (kanban t_872743b5).

The proxy's read-only SHADOW taps must no longer include the dead 'ours'
z.ai key (registered as ``zai_ours`` in the _shadow_optimizer tap). The
key was disabled Aug 15 and retired permanently per Felix (friend-only
policy — never re-add). Live key handling (best_key ordering, KEYS
health gating, LiveRouter) is intentionally untouched — only the shadow
provider sets drop it.

Evidence this fixes: 4,779 rows/24h in routing_shadow_decisions with
reason 'cheapest viable provider: zai_ours ...' (tokens=0, pressure tap),
all disagreeing with the live 'friend' pick.

Run: python3 -m pytest tests/test_shadow_drop_ours.py -v
"""
from __future__ import annotations

import os
import sys
import unittest

# Bootstrap import path (zai_proxy lives in ~/.hermes/bot)
sys.path.insert(0, os.path.expanduser("~/.hermes/bot"))

import zai_proxy as z  # noqa: E402


class ShadowDropOursTests(unittest.TestCase):
    """S3b: dead 'ours' key must be absent from every SHADOW provider set."""

    def test_shadow_optimizer_excludes_zai_ours(self):
        """The _shadow_optimizer tap (source of the tokens=0
        routing_shadow_decisions rows) must not register zai_ours."""
        opt = z._shadow_optimizer
        self.assertIsNotNone(
            opt, "shadow optimizer tap missing — MRE import failed?"
        )
        self.assertNotIn("zai_ours", opt._providers)

    def test_shadow_hook_seeds_exclude_ours(self):
        """The ShadowHook tap (MRE src/shadow_hook.py on sys.path) must
        not carry 'ours' in its Kalman provider set."""
        hook = z._shadow_hook
        self.assertIsNotNone(hook, "shadow hook tap missing — import failed?")
        self.assertNotIn("ours", hook._price_kalmans)
        self.assertNotIn("ours", hook._consumption_kalmans)


if __name__ == "__main__":
    unittest.main()
