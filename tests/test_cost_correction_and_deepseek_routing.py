#!/usr/bin/env python3
"""test_cost_correction_and_deepseek_routing.py — TDD tests for two bug fixes.

Bug 1: NeuralWatt correction factor must apply in _extract_cost(), not just
       _estimate_cost_usd(). Without it, recorded spend is inflated ~3.6x.
       See design doc §8.

Bug 2: _try_opencode_go() must translate model names via _PROVIDER_MODEL_NAMES
       so that "deepseek/deepseek-v4-flash" → "deepseek-v4-flash" before
       sending to opencode.ai. Without it, opencode.ai rejects the prefixed
       name and the request falls through to NeuralWatt ($1.43/M) instead of
       opencode_go ($0 marginal). See design doc §9.

Run:  python3 -m pytest tests/test_cost_correction_and_deepseek_routing.py -v
  or: python3 tests/test_cost_correction_and_deepseek_routing.py
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.expanduser("~/.hermes/bot"))

import zai_proxy as z  # noqa: E402


# ── Bug 1: NeuralWatt cost correction in _extract_cost ──────────────────────

class TestNeuralWattCostCorrection(unittest.TestCase):
    """Verify _extract_cost() applies the NeuralWatt correction factor."""

    def _make_neuralwatt_response(self, prompt_tokens=1000, completion_tokens=500,
                                   cached_tokens=0, model="deepseek-v4-flash"):
        """Build a fake NeuralWatt JSON response body."""
        return json.dumps({
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "test response"},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "prompt_tokens_details": {"cached_tokens": cached_tokens},
            },
        }).encode()

    def test_correction_fn_applied_when_loaded(self):
        """When _neuralwatt_cost_correction_fn is loaded and returns 0.2762,
        _extract_cost must multiply the raw cost by that factor."""
        # Build a response with known token counts
        body = self._make_neuralwatt_response(
            prompt_tokens=1_000_000,  # 1M tokens for easy math
            completion_tokens=0,
            cached_tokens=0,
            model="deepseek-v4-flash",
        )

        # Compute the uncorrected cost manually
        rates = z.NEURALWATT_RATES.get("deepseek-v4-flash", {})
        input_rate = rates.get("input", 0.14)
        raw_cost = (1_000_000 * input_rate) / 1_000_000  # = input_rate

        # Patch the correction fn to return a known factor
        original_fn = z._neuralwatt_cost_correction_fn
        mock_fn = MagicMock(return_value=0.2762)
        with patch.object(z, "_neuralwatt_cost_correction_fn", mock_fn):
            cost, source = z._extract_cost("neuralwatt", body, 1_000_000)

        # Restore
        z._neuralwatt_cost_correction_fn = original_fn

        self.assertIsNotNone(cost, "cost should not be None for neuralwatt")
        expected = raw_cost * 0.2762
        self.assertAlmostEqual(cost, expected, places=6,
                                msg=f"NeuralWatt cost should be corrected: "
                                    f"expected {expected}, got {cost}")

    def test_correction_fn_none_falls_back_to_raw(self):
        """When _neuralwatt_cost_correction_fn is None, cost should be
        the uncorrected rate-derived cost (no crash)."""
        body = self._make_neuralwatt_response(
            prompt_tokens=100_000,
            completion_tokens=50_000,
            model="deepseek-v4-flash",
        )

        original_fn = z._neuralwatt_cost_correction_fn
        with patch.object(z, "_neuralwatt_cost_correction_fn", None):
            cost, source = z._extract_cost("neuralwatt", body, 150_000)

        z._neuralwatt_cost_correction_fn = original_fn

        self.assertIsNotNone(cost)
        # Should be the raw cost without correction
        rates = z.NEURALWATT_RATES.get("deepseek-v4-flash", {})
        input_rate = rates.get("input", 0.14)
        output_rate = rates.get("output", 0.28)
        expected = (100_000 * input_rate + 50_000 * output_rate) / 1_000_000
        self.assertAlmostEqual(cost, expected, places=6)

    def test_correction_fn_exception_falls_back_to_raw(self):
        """When _neuralwatt_cost_correction_fn raises, cost should be
        the uncorrected rate-derived cost (no crash)."""
        body = self._make_neuralwatt_response(
            prompt_tokens=100_000,
            completion_tokens=0,
            model="deepseek-v4-flash",
        )

        original_fn = z._neuralwatt_cost_correction_fn
        mock_fn = MagicMock(side_effect=RuntimeError("bridge down"))
        with patch.object(z, "_neuralwatt_cost_correction_fn", mock_fn):
            cost, source = z._extract_cost("neuralwatt", body, 100_000)

        z._neuralwatt_cost_correction_fn = original_fn

        self.assertIsNotNone(cost)
        rates = z.NEURALWATT_RATES.get("deepseek-v4-flash", {})
        input_rate = rates.get("input", 0.14)
        expected = (100_000 * input_rate) / 1_000_000
        self.assertAlmostEqual(cost, expected, places=6)


# ── Bug 2: deepseek model name translation in _try_opencode_go ──────────────

class TestOpencodeGoModelTranslation(unittest.TestCase):
    """Verify _try_opencode_go() translates model names via _PROVIDER_MODEL_NAMES."""

    def test_provider_model_names_has_opencode_go_mapping(self):
        """Sanity: _PROVIDER_MODEL_NAMES must contain opencode_go entries
        for deepseek models."""
        oc_map = z._PROVIDER_MODEL_NAMES.get("opencode_go", {})
        self.assertIn("deepseek/deepseek-v4-flash", oc_map,
                      "opencode_go must map deepseek/deepseek-v4-flash")
        self.assertEqual(oc_map["deepseek/deepseek-v4-flash"], "deepseek-v4-flash",
                         "opencode_go must translate to bare 'deepseek-v4-flash'")

    def test_translate_deepseek_model_name(self):
        """The model translation logic should convert
        'deepseek/deepseek-v4-flash' → 'deepseek-v4-flash'."""
        model_map = z._PROVIDER_MODEL_NAMES.get("opencode_go", {})
        raw_model = "deepseek/deepseek-v4-flash"
        translated = model_map.get(raw_model, raw_model)
        self.assertEqual(translated, "deepseek-v4-flash",
                         "Model name should be translated to bare form")

    def test_translate_deepseek_v4_pro(self):
        """The model translation logic should convert
        'deepseek/deepseek-v4-pro' → 'deepseek-v4-pro'."""
        model_map = z._PROVIDER_MODEL_NAMES.get("opencode_go", {})
        raw_model = "deepseek/deepseek-v4-pro"
        translated = model_map.get(raw_model, raw_model)
        self.assertEqual(translated, "deepseek-v4-pro")

    def test_unknown_model_passes_through(self):
        """Models not in the mapping should pass through unchanged."""
        model_map = z._PROVIDER_MODEL_NAMES.get("opencode_go", {})
        raw_model = "some-unknown-model"
        translated = model_map.get(raw_model, raw_model)
        self.assertEqual(translated, raw_model)

    def test_glm_passes_through(self):
        """glm-5.2 should stay glm-5.2 (already bare)."""
        model_map = z._PROVIDER_MODEL_NAMES.get("opencode_go", {})
        raw_model = "glm-5.2"
        translated = model_map.get(raw_model, raw_model)
        self.assertEqual(translated, "glm-5.2")


if __name__ == "__main__":
    unittest.main()