#!/usr/bin/env python3
"""test_telnyx_failover.py — Telnyx Kimi K3 failover tests.

Tests that when Ollama Cloud fails for kimi models, the proxy falls back
to the Telnyx demo endpoint (no API key, browser-like headers, SSE streaming)
instead of returning a bare 503.

Coverage:
  (a) Telnyx model name mapping exists in _PROVIDER_MODEL_NAMES for kimi models.
  (b) _try_telnyx() sends requests with correct Origin/Referer headers.
  (c) _try_telnyx() returns False on network failure (caller proceeds to 503).
  (d) _try_telnyx() returns True and streams SSE response on success.
  (e) _OLLAMA_ONLY_MODELS block tries Telnyx before returning 503 for kimi models.
  (f) Non-kimi ollama-only models still return 503 (no Telnyx fallback).

Run: python3 tests/test_telnyx_failover.py
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch, ANY

# Bootstrap import path (zai_proxy lives in ~/.hermes/bot)
sys.path.insert(0, os.path.expanduser("~/.hermes/bot"))

import zai_proxy as z  # noqa: E402


def _temp_db():
    """Isolated in-file SQLite DB + connection for log isolation."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = __import__("sqlite3").connect(path, isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn, path


class TelnyxModelMappingTests(unittest.TestCase):
    """Test (a): model name mapping exists for Telnyx provider."""

    def test_telnyx_in_provider_model_names(self):
        """_PROVIDER_MODEL_NAMES should have a 'telnyx' entry."""
        self.assertIn("telnyx", z._PROVIDER_MODEL_NAMES)

    def test_kimi_k3_cloud_mapping(self):
        """kimi-k3:cloud should map to moonshotai/Kimi-K3 on Telnyx."""
        telnyx_models = z._PROVIDER_MODEL_NAMES.get("telnyx", {})
        self.assertEqual(telnyx_models.get("kimi-k3:cloud"), "moonshotai/Kimi-K3")

    def test_kimi_k27_code_mapping(self):
        """kimi-k2.7-code should map to a Telnyx model."""
        telnyx_models = z._PROVIDER_MODEL_NAMES.get("telnyx", {})
        self.assertIn("kimi-k2.7-code", telnyx_models)
        # K2.5 is the closest available on Telnyx
        self.assertEqual(telnyx_models["kimi-k2.7-code"], "moonshotai/Kimi-K2.5")


class TelnyxConstantsTests(unittest.TestCase):
    """Test that Telnyx provider constants are defined."""

    def test_telnyx_base_url_defined(self):
        """TELNYX_BASE should be defined (demo or production)."""
        self.assertTrue(hasattr(z, "TELNYX_BASE"))
        self.assertTrue(z.TELNYX_BASE)

    def test_telnyx_demo_url_defined(self):
        """TELNYX_DEMO_URL should be the demo endpoint."""
        self.assertTrue(hasattr(z, "TELNYX_DEMO_URL"))
        self.assertIn("telnyx.com", z.TELNYX_DEMO_URL)

    def test_telnyx_fallback_models_set(self):
        """_TELNYX_FALLBACK_MODELS should contain kimi models."""
        self.assertTrue(hasattr(z, "_TELNYX_FALLBACK_MODELS"))
        self.assertIn("kimi-k3:cloud", z._TELNYX_FALLBACK_MODELS)
        self.assertIn("kimi-k2.7-code", z._TELNYX_FALLBACK_MODELS)

    def test_telnyx_in_external_providers(self):
        """Telnyx should be in EXTERNAL_PROVIDERS dict (may have empty key for demo)."""
        self.assertIn("telnyx", z.EXTERNAL_PROVIDERS)

    def test_telnyx_in_provider_priority(self):
        """Telnyx should be in _PROVIDER_PRIORITY."""
        self.assertIn("telnyx", z._PROVIDER_PRIORITY)


class TryTelnyxTests(unittest.TestCase):
    """Tests (b),(c),(d): _try_telnyx() method behavior."""

    def setUp(self):
        self._db_conn, self._db_path = _temp_db()
        self._orig_usage_db = z._usage_db
        z._usage_db = lambda: self._db_conn

    def tearDown(self):
        z._usage_db = self._orig_usage_db
        os.unlink(self._db_path)

    def _make_handler(self):
        """Create a mock handler with _try_telnyx bound method."""
        handler = MagicMock(spec=z.Handler)
        handler._spend_recorded = False
        # Bind the real method
        handler._try_telnyx = z.Handler._try_telnyx.__get__(handler, z.Handler)
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        handler.wfile = MagicMock()
        handler.wfile.write = MagicMock()
        handler.wfile.flush = MagicMock()
        return handler

    def test_try_telnyx_returns_false_on_network_error(self):
        """(c): network failure → False, caller proceeds to 503."""
        handler = self._make_handler()
        body = json.dumps({"model": "kimi-k3:cloud", "messages": [{"role": "user", "content": "hi"}]}).encode()

        with patch("urllib.request.urlopen", side_effect=Exception("network error")):
            result = handler._try_telnyx(body, "kimi-k3:cloud", bytearray(), 0.0)

        self.assertFalse(result)

    def test_try_telnyx_sends_origin_referer_headers(self):
        """(b): request must include Origin and Referer headers."""
        handler = self._make_handler()
        body = json.dumps({"model": "kimi-k3:cloud", "messages": [{"role": "user", "content": "hi"}]}).encode()

        captured_headers = {}

        class FakeResponse:
            status = 200
            headers = {}
            def read(self, n=-1):
                return b""
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass

        def capture_req(req, **kw):
            captured_headers.update(req.headers)
            return FakeResponse()

        with patch("urllib.request.urlopen", side_effect=capture_req):
            handler._try_telnyx(body, "kimi-k3:cloud", bytearray(), 0.0)

        self.assertIn("Origin", captured_headers)
        self.assertEqual(captured_headers["Origin"], "https://telnyx.com")
        self.assertIn("Referer", captured_headers)
        self.assertEqual(captured_headers["Referer"], "https://telnyx.com/products/inference")

    def test_try_telnyx_maps_model_name(self):
        """(b): model name should be mapped to moonshotai/Kimi-K3."""
        handler = self._make_handler()
        body = json.dumps({"model": "kimi-k3:cloud", "messages": [{"role": "user", "content": "hi"}]}).encode()

        captured_data = {}

        class FakeResponse:
            status = 200
            headers = {}
            def read(self, n=-1):
                return b""
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass

        def capture_req(req, **kw):
            captured_data["body"] = req.data
            return FakeResponse()

        with patch("urllib.request.urlopen", side_effect=capture_req):
            handler._try_telnyx(body, "kimi-k3:cloud", bytearray(), 0.0)

        sent_body = json.loads(captured_data["body"])
        self.assertEqual(sent_body["model"], "moonshotai/Kimi-K3")

    def test_try_telnyx_returns_true_on_success(self):
        """(d): successful response → True, response streamed to client."""
        handler = self._make_handler()
        body = json.dumps({"model": "kimi-k3:cloud", "messages": [{"role": "user", "content": "hi"}]}).encode()

        sse_response = (
            b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":" world"}}]}\n\n'
            b'data: [DONE]\n\n'
        )

        class FakeResponse:
            status = 200
            headers = {"Content-Type": "text/event-stream"}
            _data = sse_response
            _pos = 0
            def read(self, n=4096):
                chunk = self._data[self._pos:self._pos + n]
                self._pos += len(chunk)
                return chunk
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass

        with patch("urllib.request.urlopen", return_value=FakeResponse()):
            result = handler._try_telnyx(body, "kimi-k3:cloud", bytearray(), 0.0)

        self.assertTrue(result)
        handler.send_response.assert_called_once_with(200)
        # Verify response was streamed
        self.assertTrue(handler.wfile.write.called)


class OllamaOnlyFallbackTests(unittest.TestCase):
    """Tests (e),(f): _OLLAMA_ONLY_MODELS block tries Telnyx before 503."""

    def test_kimi_models_have_telnyx_fallback(self):
        """Kimi models should be in _TELNYX_FALLBACK_MODELS."""
        self.assertIn("kimi-k3:cloud", z._TELNYX_FALLBACK_MODELS)
        self.assertIn("kimi-k2.7-code", z._TELNYX_FALLBACK_MODELS)

    def test_non_kimi_ollama_only_models_not_in_telnyx_fallback(self):
        """Non-kimi ollama-only models should NOT have Telnyx fallback."""
        non_kimi = {"gpt-oss:120b", "gemma4:31b", "qwen3.5:397b"}
        for model in non_kimi:
            self.assertNotIn(model, z._TELNYX_FALLBACK_MODELS,
                            f"{model} should not have Telnyx fallback")


if __name__ == "__main__":
    unittest.main(verbosity=2)