#!/usr/bin/env python3
"""test_reasoning_injection.py — FIX-1 (2026-09-02).

Reasoning models return their thinking in a companion field that clients
often ignore, leaving message.content empty. The field name differs by
provider: z.ai returns "reasoning_content"; ollama and neuralwatt return
"reasoning". The rescue path must accept BOTH namings so ollama-routed glm
responses are not treated as empty/degenerate (empty assistant turns broke
long tool-loop sessions on 2026-09-02).

Covers src/reasoning_handler.py (canonical module) — the same logic is
inlined at two response sites in zai_proxy.py (see zai_proxy.py ~:4800 and
~:6210) and live-verified via the key-disabled ollama_cloud_3 probe.
"""
from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

# Load by file path, NOT via `import src.reasoning_handler`: binding the
# `src` package before zai_proxy imports would change which src/ tree
# (bot vs ~/merchant-routing-engine) the proxy resolves in the same test
# process — a known dual-`src` shadowing hazard. File-path loading keeps
# this test hermetic and order-independent.
_MODULE_PATH = Path(__file__).resolve().parent.parent / "src" / "reasoning_handler.py"
_spec = importlib.util.spec_from_file_location("reasoning_handler_under_test", _MODULE_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

check_and_inject_reasoning = _mod.check_and_inject_reasoning
is_content_empty = _mod.is_content_empty


def _body(content="", reasoning=None, reasoning_content=None):
    msg = {"role": "assistant", "content": content}
    if reasoning is not None:
        msg["reasoning"] = reasoning
    if reasoning_content is not None:
        msg["reasoning_content"] = reasoning_content
    return json.dumps(
        {"id": "x", "object": "chat.completion", "choices": [{"index": 0, "message": msg, "finish_reason": "length"}]}
    ).encode()


class CheckAndInjectTests(unittest.TestCase):
    def test_ollama_reasoning_field_injected(self):
        """ollama/neuralwatt naming: content empty, reasoning has data."""
        out = check_and_inject_reasoning(_body(content="", reasoning="The user wants a reply"))
        parsed = json.loads(out)
        self.assertEqual(parsed["choices"][0]["message"]["content"], "The user wants a reply")

    def test_zai_reasoning_content_field_injected(self):
        """z.ai naming: content empty, reasoning_content has data."""
        out = check_and_inject_reasoning(_body(content="", reasoning_content="internal thinking"))
        parsed = json.loads(out)
        self.assertEqual(parsed["choices"][0]["message"]["content"], "internal thinking")

    def test_zai_field_preferred_over_ollama_field(self):
        """Both fields present: reasoning_content (z.ai canonical) wins."""
        out = check_and_inject_reasoning(
            _body(content="", reasoning="b", reasoning_content="a")
        )
        parsed = json.loads(out)
        self.assertEqual(parsed["choices"][0]["message"]["content"], "a")

    def test_nonempty_content_passthrough(self):
        """Content present: body returned unmodified."""
        body = _body(content="actual answer", reasoning="thinking")
        self.assertEqual(check_and_inject_reasoning(body), body)

    def test_both_empty_unchanged(self):
        """No reasoning anywhere: body returned unmodified."""
        body = _body(content="")
        self.assertEqual(check_and_inject_reasoning(body), body)

    def test_whitespace_reasoning_not_injected(self):
        """Whitespace-only reasoning is not injected."""
        body = _body(content="", reasoning="   ")
        self.assertEqual(check_and_inject_reasoning(body), body)

    def test_non_string_reasoning_ignored(self):
        """Non-string reasoning values (dict/list/None) never injected."""
        body = _body(content="", reasoning={"steps": ["a"]})
        self.assertEqual(check_and_inject_reasoning(body), body)

    def test_invalid_json_unchanged(self):
        self.assertEqual(check_and_inject_reasoning(b"not json"), b"not json")

    def test_no_choices_unchanged(self):
        body = json.dumps({"object": "chat.completion"}).encode()
        self.assertEqual(check_and_inject_reasoning(body), body)


class IsContentEmptyTests(unittest.TestCase):
    def test_empty_content_with_ollama_reasoning(self):
        is_empty, has_reasoning = is_content_empty(_body(content="", reasoning="deep thought"))
        self.assertTrue(is_empty)
        self.assertTrue(has_reasoning)

    def test_empty_content_with_zai_reasoning(self):
        is_empty, has_reasoning = is_content_empty(_body(content="", reasoning_content="zai thought"))
        self.assertTrue(is_empty)
        self.assertTrue(has_reasoning)

    def test_nonempty_content(self):
        is_empty, has_reasoning = is_content_empty(_body(content="answer", reasoning="r"))
        self.assertFalse(is_empty)

    def test_error_response(self):
        is_empty, has_reasoning = is_content_empty(json.dumps({"error": "boom"}).encode())
        self.assertTrue(is_empty)
        self.assertFalse(has_reasoning)

    def test_stream_done_marker(self):
        is_empty, _ = is_content_empty(b"data: [DONE]")
        self.assertTrue(is_empty)


if __name__ == "__main__":
    unittest.main()
