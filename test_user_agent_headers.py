#!/usr/bin/env python3
"""Tests verifying User-Agent headers on all provider urllib requests.

Cloudflare blocks Python's default User-Agent ("Python-urllib/3.x") with
error 1010 (403 Forbidden).  The proxy was marking valid keys as dead
because a Cloudflare 403 was misinterpreted as an auth failure — see
the opencode_go incident where a perfectly valid key was disabled for
hours after 8 retries.

These tests verify:
  1. Every Cloudflare-protected provider headers dict includes a
     non-empty User-Agent that is NOT "Python-urllib/3.x".
  2. Static analysis: source-level inspection of every
     ``urllib.request.Request`` call and its associated headers dict.

Run:  python3 -m pytest test_user_agent_headers.py -v
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import time
import urllib.request
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock, Mock

# ── Import paths ─────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, os.path.expanduser("~/merchant-routing-engine"))

# ── Source text for static analysis ──────────────────────────────────────
_PROXY_SRC = (_HERE / "zai_proxy.py").read_text()
_COLLECTORS_SRC = (
    Path(os.path.expanduser("~/merchant-routing-engine/src/balance_collectors.py")).read_text()
)

# ── Module imports (runtime tests) ───────────────────────────────────────
import zai_proxy  # noqa: E402
import src.balance_collectors as bc  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════════
# Helper: FakeHandler stand-in for BaseHTTPRequestHandler
# ═══════════════════════════════════════════════════════════════════════════

def _make_fake_handler():
    """Create a minimal object that Handler methods can use as ``self``."""
    h = MagicMock()
    h.send_response = Mock()
    h.send_header = Mock()
    h.end_headers = Mock()
    h.wfile = MagicMock()
    h.wfile.flush = Mock()
    h._spend_recorded = False
    h._session_id = None
    h._task_type = None
    return h


def _capture_headers(fn, *args, **kwargs):
    """Call *fn* with mocked urllib and return captured Request headers.

    Returns the merged headers dict from the last ``urllib.request.Request``
    call made inside *fn*.  Exceptions from downstream code are swallowed —
    we only care about the headers.
    """
    captured: dict = {}

    real_Request = urllib.request.Request   # save original

    class _FakeResp:
        status = 200
        headers = {}

        def read(self, *a, **kw):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_Request(url, *a, **kw):
        captured.clear()
        captured.update(kw.get("headers", {}))
        return MagicMock()

    def _fake_urlopen(*a, **kw):
        return _FakeResp()

    with patch.object(zai_proxy.urllib.request, "Request", _fake_Request):
        with patch.object(zai_proxy.urllib.request, "urlopen", _fake_urlopen):
            try:
                fn(*args, **kwargs)
            except Exception:
                pass

    return dict(captured)


# ═══════════════════════════════════════════════════════════════════════════
# Runtime tests — zai_proxy.py Handler methods
# ═══════════════════════════════════════════════════════════════════════════

class TestOpencodeGoUserAgent(unittest.TestCase):
    """opencode_go — Cloudflare-protected, was the original bug."""

    def setUp(self):
        zai_proxy.OPENCODE_GO_KEY = "test-opencode-go-key"
        self._p = patch.object(zai_proxy, "_is_key_healthy", return_value=True)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_has_user_agent(self):
        handler = _make_fake_handler()
        body = json.dumps({"model": "glm-5.2", "messages": []}).encode()
        hdrs = _capture_headers(
            zai_proxy.Handler._try_opencode_go,
            handler, body, "glm-5.2", bytearray(), time.time()
        )
        self.assertIn("User-Agent", hdrs, "opencode_go headers must include User-Agent")
        self.assertTrue(hdrs["User-Agent"], "User-Agent must be non-empty")
        self.assertNotEqual(hdrs["User-Agent"], "Python-urllib/3.x")

    def test_ua_is_mozilla(self):
        handler = _make_fake_handler()
        body = json.dumps({"model": "glm-5.2", "messages": []}).encode()
        hdrs = _capture_headers(
            zai_proxy.Handler._try_opencode_go,
            handler, body, "glm-5.2", bytearray(), time.time()
        )
        self.assertEqual(hdrs.get("User-Agent"), "Mozilla/5.0")


class TestTelnyxProxyUserAgent(unittest.TestCase):
    """Telnyx production path — api.telnyx.com is Cloudflare-protected."""

    def setUp(self):
        zai_proxy.TELNYX_KEY = "test-telnyx-key"
        self._p = patch.object(zai_proxy, "_is_key_healthy", return_value=True)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_production_path_has_user_agent(self):
        """The authenticated (non-demo) Telnyx proxy path must set UA."""
        handler = _make_fake_handler()
        body = json.dumps({"model": "llama-3.1-8b", "messages": []}).encode()
        hdrs = _capture_headers(
            zai_proxy.Handler._try_telnyx,
            handler, body, "llama-3.1-8b", bytearray(), time.time()
        )
        self.assertIn("User-Agent", hdrs, "Telnyx production headers must include User-Agent")
        self.assertNotEqual(hdrs.get("User-Agent"), "Python-urllib/3.x")


class TestOllamaCloudUserAgent(unittest.TestCase):
    """Ollama Cloud (ollama.com) — likely Cloudflare-protected."""

    def setUp(self):
        zai_proxy.OLLAMA_CLOUD_KEY = "test-ollama-key"
        self._p = patch.object(zai_proxy, "_is_key_healthy", return_value=True)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_has_user_agent(self):
        handler = _make_fake_handler()
        body = json.dumps({"model": "glm-5.2", "messages": []}).encode()
        hdrs = _capture_headers(
            zai_proxy.Handler._try_ollama_cloud,
            handler, body, "glm-5.2", bytearray(), time.time()
        )
        self.assertIn("User-Agent", hdrs, "ollama_cloud headers must include User-Agent")
        self.assertNotEqual(hdrs.get("User-Agent"), "Python-urllib/3.x")


# ═══════════════════════════════════════════════════════════════════════════
# (TestOxalphaUserAgent removed 2026-09-01 — oxalpha routing/_serve_via_oxalpha
#  deleted from zai_proxy.py per dead-code sweep; UA coverage for remaining
#  paths lives in the ollama_cloud / quota / telnyx tests above.)
# ═══════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════
# Runtime tests — zai_proxy.py standalone functions
# ═══════════════════════════════════════════════════════════════════════════

class TestFetchQuotaWindowsUserAgent(unittest.TestCase):
    """_fetch_quota_windows — api.z.ai is Cloudflare-protected."""

    def test_has_user_agent(self):
        """z.ai quota fetch must include User-Agent."""
        hdrs = _capture_headers(
            zai_proxy._fetch_quota_windows, "test-key"
        )
        self.assertIn("User-Agent", hdrs, "quota fetch headers must include User-Agent")
        self.assertNotEqual(hdrs.get("User-Agent"), "Python-urllib/3.x")


class TestGetTelnyxBalanceUserAgent(unittest.TestCase):
    """_get_telnyx_balance — api.telnyx.com is Cloudflare-protected."""

    def setUp(self):
        zai_proxy.TELNYX_KEY = "test-telnyx-key"

    def test_has_user_agent(self):
        hdrs = _capture_headers(
            zai_proxy._get_telnyx_balance
        )
        self.assertIn("User-Agent", hdrs, "Telnyx balance headers must include User-Agent")
        self.assertNotEqual(hdrs.get("User-Agent"), "Python-urllib/3.x")


# ═══════════════════════════════════════════════════════════════════════════
# Static analysis — verify source-level User-Agent presence
# ═══════════════════════════════════════════════════════════════════════════

def _extract_headers_near(source: str, pattern: str, max_lines: int = 45) -> str:
    """Return the headers dict text near a matching line.

    Searches forward from *pattern* for ``hdrs = {`` or a multi-line dict,
    returns the raw source text.  Once a dict start is found, the function
    reads the *entire* dict (ignoring ``max_lines``) so the closing brace
    is always included.

    Note: f-string braces ``{var}`` inside header values inflate the brace
    depth but cancel themselves out (one ``{`` + one ``}``), so the net
    depth change is zero — the collection loop handles this correctly.
    """
    lines = source.splitlines()
    n = len(lines)
    for i, line in enumerate(lines):
        if re.search(pattern, line):
            search_end = min(i + max_lines, n)
            for j in range(i, search_end):
                # Match a declared dict: `hdrs = {` or `headers={`
                if re.search(r'(\bhdrs\b|\bheaders\b)\s*[:=]\s*\{', lines[j]):
                    # Collect full dict — scan to end of file if needed
                    buf = [lines[j]]
                    depth = lines[j].count('{') - lines[j].count('}')
                    k = j + 1
                    while depth > 0 and k < n:
                        buf.append(lines[k])
                        depth += lines[k].count('{') - lines[k].count('}')
                        k += 1
                    return '\n'.join(buf)
            # Also handle inline: headers={"Authorization": ..., "User-Agent": ...}
            for j in range(i, search_end):
                if 'headers=' in lines[j] and '{' in lines[j]:
                    hdr_start = lines[j].index('{')
                    # Full inline dict
                    if lines[j].count('{') == lines[j].count('}'):
                        return lines[j]
                    # Multi-line: collect until balanced braces
                    # Adjust for the portion before {
                    buf = [lines[j]]
                    depth = lines[j].count('{') - lines[j].count('}')
                    k = j + 1
                    while depth > 0 and k < n:
                        buf.append(lines[k])
                        depth += lines[k].count('{') - lines[k].count('}')
                        k += 1
                    return '\n'.join(buf)
    return ""


class TestSourceLevelUserAgent(unittest.TestCase):
    """Static analysis of zai_proxy.py source for User-Agent presence."""

    def _assert_ua_in_headers(self, headers_text: str, context: str):
        """Assert that the captured headers text contains a User-Agent key."""
        self.assertTrue(headers_text, f"No headers dict found near {context}")
        # Check for "User-Agent" in the headers text
        self.assertIn(
            "User-Agent", headers_text,
            f"User-Agent header missing from {context}.\nFound headers:\n{headers_text}"
        )
        # Make sure it's not the default Python-urllib
        self.assertNotIn(
            "Python-urllib", headers_text,
            f"Default Python-urllib User-Agent in {context} — Cloudflare will block this."
        )

    def test_opencode_go_source(self):
        """opencode_go headers must include User-Agent in source."""
        h = _extract_headers_near(_PROXY_SRC, r'_try_opencode_go')
        self._assert_ua_in_headers(h, "_try_opencode_go")

    def test_telnyx_production_source(self):
        """Telnyx production (authenticated) headers must include User-Agent."""
        # Find TELNYX_BASE + "/chat/completions" then the hdrs dict
        h = _extract_headers_near(_PROXY_SRC, r'TELNYX_BASE \+ "/chat/completions"')
        self._assert_ua_in_headers(h, "Telnyx production proxy")

    def test_ollama_cloud_source(self):
        """Ollama Cloud headers must include User-Agent."""
        h = _extract_headers_near(_PROXY_SRC, r'OLLAMA_CLOUD_BASE \+ "/chat/completions"')
        self._assert_ua_in_headers(h, "ollama_cloud _try_ollama_cloud")

    def test_fetch_quota_windows_source(self):
        """_fetch_quota_windows headers must include User-Agent."""
        h = _extract_headers_near(_PROXY_SRC, r'req = urllib\.request\.Request\(QUOTA_URL')
        self._assert_ua_in_headers(h, "_fetch_quota_windows")

    def test_get_telnyx_balance_source(self):
        """_get_telnyx_balance headers must include User-Agent."""
        h = _extract_headers_near(_PROXY_SRC, r'api\.telnyx\.com/v2/balance')
        self._assert_ua_in_headers(h, "_get_telnyx_balance")

    def test_generic_failover_source(self):
        """Generic failover headers must include User-Agent (source)."""
        # The generic failover is in _try_external_failover
        h = _extract_headers_near(_PROXY_SRC, r'prov\["base_url"\] \+ "/chat/completions"')
        self._assert_ua_in_headers(h, "generic failover")

    # test_openrouter_key_check_source removed 2026-09-01 — _oxalpha_usage_poller
    # deleted from zai_proxy.py per dead-code sweep (promo key 401-dead upstream).


# ═══════════════════════════════════════════════════════════════════════════
# Static analysis — balance_collectors.py
# ═══════════════════════════════════════════════════════════════════════════

class TestCollectorsSourceUserAgent(unittest.TestCase):
    """Verify balance_collectors.py source has User-Agent on all collectors."""

    def test_ppq_collector_source(self):
        """PPQ collector headers must include User-Agent."""
        h = _extract_headers_near(_COLLECTORS_SRC, r'credits/balance')
        self.assertTrue(h, "No headers found near PPQ balance endpoint")
        self.assertIn("User-Agent", h, "User-Agent missing from PPQ collector")

    def test_openrouter_collector_source(self):
        """OpenRouter collector headers must include User-Agent."""
        h = _extract_headers_near(_COLLECTORS_SRC, r'collect_openrouter_balance')
        self.assertTrue(h, "No headers found near OpenRouter collector")
        self.assertIn("User-Agent", h, "User-Agent missing from OpenRouter collector")

    def test_deepinfra_collector_source(self):
        """DeepInfra collector headers must include User-Agent."""
        # DeepInfra uses a _default_http_get seam — check the headers dict
        h = _extract_headers_near(_COLLECTORS_SRC, r'collect_deepinfra_balance')
        self.assertTrue(h, "No headers found near DeepInfra collector")
        self.assertIn("User-Agent", h, "User-Agent missing from DeepInfra collector")

    def test_neuralwatt_collector_source(self):
        """NeuralWatt collector already has User-Agent (regression check)."""
        h = _extract_headers_near(_COLLECTORS_SRC, r'_neuralwatt_http_get')
        self.assertTrue(h, "No headers found near NeuralWatt collector")
        self.assertIn("User-Agent", h, "User-Agent missing from NeuralWatt collector")


# ═══════════════════════════════════════════════════════════════════════════
# Comprehensive: no bare Python-urllib User-Agent anywhere
# ═══════════════════════════════════════════════════════════════════════════

class TestNoPythonUrllibUAAnywhere(unittest.TestCase):
    """Ensure no code path sends the default Python-urllib UA to Cloudflare."""

    def test_no_bare_request_without_headers(self):
        """Check that no urllib.request.Request call near Cloudflare URLs
        omits a headers argument entirely."""
        # Find all Request calls and check they have a headers kwarg
        pattern = r'urllib\.request\.Request\('
        for match in re.finditer(pattern, _PROXY_SRC):
            # Get the next ~5 lines
            pos = match.start()
            line_start = _PROXY_SRC.rfind('\n', 0, pos) + 1
            line_end = _PROXY_SRC.find('\n', pos)
            snippet = _PROXY_SRC[line_start:line_end + 200]
            # Skip if it's a line we already know has UA (the test checks above)
            # This is a broad sanity check, not a strict assertion
            # We just ensure no Request call is completely bare (no headers kwarg)
            # when the URL is a known Cloudflare domain
            cloudflare_domains = [
                'api.z.ai', 'api.telnyx.com', 'openrouter.ai',
                'ollama.com', 'opencode.ai',
            ]
            for domain in cloudflare_domains:
                if domain in snippet:
                    # Must have headers= or headers in the call
                    # (within 5 lines after the Request call)
                    self.assertIn(
                        'header', snippet.lower(),
                        f"urllib.request.Request near {domain} may lack headers: "
                        f"check that User-Agent is set.\nSnippet: {snippet[:200]}"
                    )
                    break


if __name__ == "__main__":
    unittest.main(verbosity=2)
