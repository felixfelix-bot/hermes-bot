#!/usr/bin/env python3
"""test_disable_meta.py — P1 (kanban t_6cf20181).

Self-healing disable policy: companion `.key_disabled_<name>.meta` marker
distinguishing auto-placed vs operator-placed disable flags, so auto-clear
never touches a deliberate operator disable.

Covers:
  1. _write_disable_meta / _read_disable_meta round-trip (auto + operator).
  2. Legacy flags with no .meta default to "operator" for safety, but are
     surfaced as "unclassified" for the digest.

Run: python3 -m pytest tests/test_disable_meta.py -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.expanduser("~/.hermes/bot"))

import zai_proxy as z  # noqa: E402


class DisableMetaTests(unittest.TestCase):
    """P1: .key_disabled_<name>.meta write/read + legacy classification."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._home = Path(self._tmp.name)
        # Point Path.home() at the temp dir so flag/meta paths are isolated.
        self._home_patch = patch.object(Path, "home", return_value=self._home)
        self._home_patch.start()

    def tearDown(self):
        self._home_patch.stop()
        self._tmp.cleanup()

    def _flag(self, name):
        p = z._disabled_flag_path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def _meta(self, name):
        return Path(str(self._flag(name)) + ".meta")

    # ── 1. Round-trip ──────────────────────────────────────────────────────

    def test_write_read_roundtrip_auto(self):
        """Auto-placed meta round-trips with placed_by=auto + fields."""
        z._write_disable_meta("friend", "auto", "429_storm", expiry=1234567890)
        meta = z._read_disable_meta("friend")
        self.assertIsNotNone(meta)
        self.assertEqual(meta["placed_by"], "auto")
        self.assertEqual(meta["reason"], "429_storm")
        self.assertEqual(meta["expiry"], 1234567890)
        self.assertIsInstance(meta["placed_at"], (int, float))
        # Meta file physically exists next to the flag path.
        self.assertTrue(self._meta("friend").exists())

    def test_write_read_roundtrip_operator(self):
        """Operator-placed meta round-trips with placed_by=operator."""
        z._write_disable_meta("ours", "operator", "manual", expiry=None)
        meta = z._read_disable_meta("ours")
        self.assertIsNotNone(meta)
        self.assertEqual(meta["placed_by"], "operator")
        self.assertEqual(meta["reason"], "manual")
        self.assertIsNone(meta["expiry"])

    def test_read_missing_meta_returns_none(self):
        """No meta file → _read_disable_meta returns None (legacy/unclassified)."""
        self.assertFalse(self._meta("friend").exists())
        self.assertIsNone(z._read_disable_meta("friend"))

    def test_write_meta_does_not_touch_flag_gate(self):
        """Writing meta must NOT create the authoritative flag (fail-open gate
        preserved — meta is a sidecar, never the gate)."""
        z._write_disable_meta("friend", "auto", "429_storm")
        self.assertFalse(self._flag("friend").exists())
        self.assertTrue(self._meta("friend").exists())

    # ── 2. Legacy classification ───────────────────────────────────────────

    def test_legacy_no_meta_classified_unclassified(self):
        """A flag with no .meta is 'unclassified' (treated as operator for
        auto-clear safety, but surfaced as suspect for the digest)."""
        # Simulate a legacy flag: flag exists, no meta.
        self._flag("friend").touch()
        self.assertFalse(self._meta("friend").exists())
        self.assertEqual(z._disable_placed_by("friend"), "unclassified")

    def test_auto_meta_classified_auto(self):
        """A flag with placed_by=auto meta is classified 'auto'."""
        self._flag("friend").touch()
        z._write_disable_meta("friend", "auto", "429_storm")
        self.assertEqual(z._disable_placed_by("friend"), "auto")

    def test_operator_meta_classified_operator(self):
        """A flag with placed_by=operator meta is classified 'operator'."""
        self._flag("friend").touch()
        z._write_disable_meta("friend", "operator", "manual")
        self.assertEqual(z._disable_placed_by("friend"), "operator")

    def test_no_flag_no_meta_classified_none(self):
        """No flag and no meta → no classification (nothing disabled)."""
        self.assertEqual(z._disable_placed_by("friend"), None)


if __name__ == "__main__":
    unittest.main()
