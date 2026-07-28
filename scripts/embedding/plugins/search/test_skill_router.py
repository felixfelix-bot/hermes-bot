#!/usr/bin/env python3
"""
Test suite for skill_router.py - TDD implementation.
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock


ROUTER_DIR = Path("/home/c03rad0r/.hermes/profiles/manager/plugins/search")
ROUTER_PATH = ROUTER_DIR / "skill_router.py"


def _load_router_module(fresh=True):
    """Load skill_router.py from its target location."""
    import importlib.util
    if fresh and "skill_router" in sys.modules:
        del sys.modules["skill_router"]
    spec = importlib.util.spec_from_file_location("skill_router", ROUTER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["skill_router"] = module
    spec.loader.exec_module(module)
    return module


class TestSkillRouter(unittest.TestCase):
    """Tests for route_skills()."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        os.environ["HERMES_SKILL_DB_PATH"] = str(self.temp_dir / "skills.db")
        os.environ["HERMES_SKILL_EMBED_CACHE"] = str(self.temp_dir / "cache.db")

    def tearDown(self):
        shutil.rmtree(self.temp_dir)
        for key in ("HERMES_SKILL_DB_PATH", "HERMES_SKILL_EMBED_CACHE"):
            os.environ.pop(key, None)

    def _fake_vector(self, length=768):
        import random
        random.seed(0)
        return [random.uniform(-1, 1) for _ in range(length)]

    def _patcher(self, module):
        """Patch module-level constructors after the module has been loaded."""
        patch_embed = patch.object(module, "EmbedClient")
        patch_store = patch.object(module, "SkillVectorStore")
        patch_indexer = patch.object(module, "SkillIndexer")
        return patch_embed, patch_store, patch_indexer

    def test_import_and_signature(self):
        """Gate 1: skill_router module exists and exposes route_skills."""
        module = _load_router_module()
        self.assertTrue(hasattr(module, "route_skills"))
        result = module.route_skills("hello", top_k=3)
        self.assertIn("recommended_skills", result)
        self.assertIn("confidence", result)
        self.assertIn("fallback", result)
        self.assertIsInstance(result["recommended_skills"], list)

    def test_route_skills_returns_top_k(self):
        """Router should return top-k matching skill names and a confidence."""
        module = _load_router_module()
        pe, ps, pi = self._patcher(module)
        with pe as ME, ps as MS, pi as MI:
            embedder = MagicMock()
            embedder.embed.return_value = self._fake_vector()
            ME.return_value = embedder

            fake_results = [
                {"name": "esp-flash", "distance": 0.1},
                {"name": "rp2040-program", "distance": 0.2},
                {"name": "web-scrape", "distance": 0.3},
                {"name": "social-post", "distance": 0.4},
                {"name": "data-viz", "distance": 0.5},
            ]
            store = MagicMock()
            store.search.return_value = fake_results
            MS.return_value = store

            indexer = MagicMock()
            indexer.get_skills_count.return_value = 5
            MI.return_value = indexer

            result = module.route_skills("flash esp32 firmware", top_k=5)
            self.assertEqual(result["recommended_skills"], [
                "esp-flash", "rp2040-program", "web-scrape",
                "social-post", "data-viz",
            ])
            self.assertFalse(result["fallback"])
            self.assertGreaterEqual(result["confidence"], 0.0)
            self.assertLessEqual(result["confidence"], 1.0)
            self.assertEqual(embedder.embed.call_count, 1)
            store.search.assert_called_once()
            self.assertEqual(store.search.call_args.kwargs["limit"], 5)

    def test_route_skills_falls_back_when_db_empty(self):
        """Router should fall back gracefully if no skills are indexed."""
        module = _load_router_module()
        pe, ps, pi = self._patcher(module)
        with pe as ME, ps as MS, pi as MI:
            embedder = MagicMock()
            embedder.embed.return_value = self._fake_vector()
            ME.return_value = embedder

            store = MagicMock()
            store.search.return_value = []
            store.count.return_value = 0
            MS.return_value = store

            indexer = MagicMock()
            indexer.get_skills_count.return_value = 0
            indexer.index_skills.return_value = 0
            MI.return_value = indexer

            result = module.route_skills("any prompt", top_k=5)
            self.assertEqual(result["recommended_skills"], [])
            self.assertTrue(result["fallback"])
            self.assertEqual(result["confidence"], 0.0)

    def test_route_skills_falls_back_on_embed_error(self):
        """Router should fall back if the embed client raises an exception."""
        module = _load_router_module()
        pe, ps, pi = self._patcher(module)
        with pe as ME, ps as MS, pi as MI:
            embedder = MagicMock()
            embedder.embed.side_effect = RuntimeError("ollama unavailable")
            ME.return_value = embedder

            indexer = MagicMock()
            indexer.get_skills_count.return_value = 5
            MI.return_value = indexer

            result = module.route_skills("any prompt", top_k=5)
            self.assertEqual(result["recommended_skills"], [])
            self.assertTrue(result["fallback"])
            self.assertEqual(result["confidence"], 0.0)

    def test_route_skills_honours_top_k(self):
        """Router should return at most top_k results."""
        module = _load_router_module()
        pe, ps, pi = self._patcher(module)
        with pe as ME, ps as MS, pi as MI:
            embedder = MagicMock()
            embedder.embed.return_value = self._fake_vector()
            ME.return_value = embedder

            fake_results = [
                {"name": f"skill-{i}", "distance": 0.1 * i}
                for i in range(10)
            ]
            store = MagicMock()
            store.search.return_value = fake_results
            MS.return_value = store

            indexer = MagicMock()
            indexer.get_skills_count.return_value = 10
            MI.return_value = indexer

            result = module.route_skills("some prompt", top_k=3)
            self.assertEqual(len(result["recommended_skills"]), 3)
            self.assertFalse(result["fallback"])

    def test_route_skills_empty_prompt(self):
        """Empty prompt should immediately fall back."""
        module = _load_router_module()
        pe, ps, pi = self._patcher(module)
        with pe as ME, ps as MS, pi as MI:
            result = module.route_skills("   ", top_k=5)
            self.assertEqual(result["recommended_skills"], [])
            self.assertTrue(result["fallback"])
            embedder = ME.return_value
            self.assertEqual(embedder.embed.call_count, 0)


if __name__ == "__main__":
    unittest.main()
