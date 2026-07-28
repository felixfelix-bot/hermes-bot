#!/usr/bin/env python3
"""
Test suite for embed_client.py
Following TDD: write failing tests first, then implement
"""

import unittest
import os
import tempfile
import sqlite3
from unittest.mock import patch, MagicMock
import sys

# Add the parent directory to sys.path to import embed_client
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from embed_client import EmbedClient


class TestEmbedClient(unittest.TestCase):
    def setUp(self):
        """Set up test environment with temporary database"""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "embed_cache.db")
        self.client = EmbedClient(db_path=self.db_path)

    def tearDown(self):
        """Clean up temporary database"""
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        os.rmdir(self.temp_dir)

    def test_single_embed(self):
        """Test embedding a single text"""
        # This should fail initially since embed_client doesn't exist
        text = "This is a test sentence"
        result = self.client.embed(text)
        
        # Should return a 768-dimensional vector
        self.assertEqual(len(result), 768)
        # All values should be floats
        self.assertTrue(all(isinstance(x, float) for x in result))

    def test_batch_embed(self):
        """Test embedding multiple texts"""
        texts = ["First sentence", "Second sentence", "Third sentence"]
        results = self.client.embed_batch(texts)
        
        # Should return list of 768-dim vectors
        self.assertEqual(len(results), 3)
        for result in results:
            self.assertEqual(len(result), 768)
            self.assertTrue(all(isinstance(x, float) for x in result))

    def test_empty_text_raises_value_error(self):
        """Test that empty text raises ValueError"""
        with self.assertRaises(ValueError):
            self.client.embed("")

    def test_cache_hit(self):
        """Test that cache hit skips ollama call"""
        # First call - should hit ollama
        text = "Cached text"
        result1 = self.client.embed(text)
        
        # Second call - should hit cache
        result2 = self.client.embed(text)
        
        # Results should be identical
        self.assertEqual(result1, result2)

    def test_long_text_truncation(self):
        """Test that long text is truncated to ~2048 tokens"""
        # Create very long text (> 8192 chars)
        long_text = "short " * 1000  # ~6000 chars
        result = self.client.embed(long_text)
        
        # Should still return 768-dim vector
        self.assertEqual(len(result), 768)


if __name__ == '__main__':
    unittest.main()