"""reasoning_handler.py — Inject reasoning content when model output is empty.

Reasoning models return responses in two fields:
- content: the actual response (may be empty)
- reasoning_content (z.ai) / reasoning (ollama, neuralwatt): the model's
  internal thinking (usually has data)

When content is empty but a reasoning field has value, inject reasoning
into content so the tokens aren't wasted. No external failover needed.

Extracted from ~/.hermes/bot/zai_proxy.py (Phase 1 — standalone copy).
FIX-1 (2026-09-02): accept BOTH field names — ollama/neuralwatt name the
field "reasoning"; z.ai names it "reasoning_content".
"""
from __future__ import annotations
import json


def _extract_reasoning(msg: dict) -> str:
    """Return the first non-empty reasoning field value (either naming)."""
    for key in ("reasoning_content", "reasoning"):
        val = msg.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return ""


def check_and_inject_reasoning(response_body: bytes) -> bytes:
    """Check if response has empty content but valid reasoning.
    If so, inject reasoning as content and return modified body.

    Returns:
        Modified response body if reasoning was injected,
        original body otherwise.
    """
    try:
        resp_text = response_body.decode("utf-8", errors="ignore").strip()
        if not resp_text:
            return response_body

        resp_json = json.loads(resp_text)
        choices = resp_json.get("choices", [])
        if not choices:
            return response_body

        msg = choices[0].get("message", {})
        content = msg.get("content", "")
        reasoning = _extract_reasoning(msg)

        if (not content or not content.strip()) and reasoning:
            msg["content"] = reasoning
            return json.dumps(resp_json).encode()

        return response_body
    except Exception:
        return response_body


def is_content_empty(response_body: bytes) -> tuple[bool, bool]:
    """Check if response content is empty.

    Returns:
        (is_empty, has_reasoning) — is_empty=True means no usable content.
        has_reasoning=True means a reasoning field has data (can be injected).
    """
    try:
        resp_text = response_body.decode("utf-8", errors="ignore").strip()
        if not resp_text or resp_text == "data: [DONE]":
            return True, False

        resp_json = json.loads(resp_text)
        if "error" in resp_json and "choices" not in resp_json:
            return True, False  # Error response — no content at all

        choices = resp_json.get("choices", [])
        if not choices:
            return True, False

        msg = choices[0].get("message", {})
        content = msg.get("content", "")
        reasoning = _extract_reasoning(msg)

        if content and content.strip():
            return False, bool(reasoning)
        return True, bool(reasoning)
    except Exception:
        return False, False
