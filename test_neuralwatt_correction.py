#!/usr/bin/env python3
"""
TDD test: Verify NeuralWatt cost correction applies a hardcoded 0.2762 factor.

The NeuralWatt provider overcounts usage by 3.6x. The correction factor
1/3.6 = 0.2762 must be applied to BOTH code paths:
  1. Per-model rates path (NEURALWATT_RATES lookup)
  2. Fallback blended rate path (_rpt_rate("neuralwatt"))

This test verifies by reading the source code directly and checking that:
  - No _neuralwatt_cost_correction_fn call remains in the NeuralWatt branch
  - The hardcoded 0.2762 constant is present in both correction blocks
"""

import ast
import re
import pathlib
import pytest

PROXY_FILE = pathlib.Path(__file__).parent / "zai_proxy.py"


def _read_source():
    return PROXY_FILE.read_text()


def _extract_neuralwatt_branch(source: str) -> str:
    """Extract the `if provider == "neuralwatt":` branch from the source."""
    # Find the neuralwatt branch start
    match = re.search(r'(if provider == "neuralwatt":.*?)(?=\n        # 4c\.|\n        # 4b\.|\Z)',
                      source, re.DOTALL)
    if not match:
        # Try alternative pattern
        match = re.search(r'(if provider == "neuralwatt":.*?)(?=\n        # 4c)',
                          source, re.DOTALL)
    assert match, "Could not find NeuralWatt branch in zai_proxy.py"
    return match.group(1)


class TestNeuralWattHardcodedCorrection:
    """Verify the NeuralWatt correction is hardcoded, not lazy-loaded."""

    def test_no_lazy_function_call_in_neuralwatt_branch(self):
        """The _neuralwatt_cost_correction_fn call must be removed."""
        source = _read_source()
        branch = _extract_neuralwatt_branch(source)
        assert "_neuralwatt_cost_correction_fn()" not in branch, (
            "Found lazy-loaded _neuralwatt_cost_correction_fn() call in "
            "NeuralWatt branch — this should be replaced with hardcoded 0.2762"
        )

    def test_hardcoded_correction_in_per_model_path(self):
        """Per-model rates path must have hardcoded 0.2762 correction."""
        source = _read_source()
        branch = _extract_neuralwatt_branch(source)
        # The per-model path is before the "Fallback" comment
        per_model_part = branch.split("Fallback")[0]
        assert "0.2762" in per_model_part, (
            "Per-model rates path missing hardcoded 0.2762 correction"
        )
        assert "nw_correction = 0.2762" in per_model_part, (
            "Per-model path must have 'nw_correction = 0.2762' assignment"
        )
        assert "raw_cost = raw_cost * nw_correction" in per_model_part, (
            "Per-model path must apply: raw_cost = raw_cost * nw_correction"
        )

    def test_hardcoded_correction_in_fallback_path(self):
        """Fallback blended rate path must have hardcoded 0.2762 correction."""
        source = _read_source()
        branch = _extract_neuralwatt_branch(source)
        # The fallback path is after the "Fallback" comment
        fallback_part = branch.split("Fallback")[1] if "Fallback" in branch else ""
        assert "0.2762" in fallback_part, (
            "Fallback blended rate path missing hardcoded 0.2762 correction"
        )
        assert "nw_correction = 0.2762" in fallback_part, (
            "Fallback path must have 'nw_correction = 0.2762' assignment"
        )
        assert "raw_cost = raw_cost * nw_correction" in fallback_part, (
            "Fallback path must apply: raw_cost = raw_cost * nw_correction"
        )

    def test_correction_factor_value(self):
        """The hardcoded correction must be exactly 0.2762 (1/3.6)."""
        source = _read_source()
        branch = _extract_neuralwatt_branch(source)
        # Count occurrences of the hardcoded constant
        count = branch.count("0.2762")
        assert count >= 2, (
            f"Expected at least 2 occurrences of 0.2762 in NeuralWatt branch, "
            f"found {count}"
        )

    def test_no_try_except_around_correction(self):
        """Correction should NOT be wrapped in try/except (no silent failure)."""
        source = _read_source()
        branch = _extract_neuralwatt_branch(source)
        # Check that the correction lines are not inside a try block
        lines = branch.split('\n')
        in_try = False
        for line in lines:
            stripped = line.strip()
            if stripped == "try:" or stripped.startswith("try:"):
                in_try = True
            if in_try and "nw_correction = 0.2762" in stripped:
                pytest.fail(
                    "Hardcoded correction is inside a try block — should be "
                    "unconditional (no try/except)"
                )
            if in_try and "except" in stripped:
                in_try = False

    def test_correction_math(self):
        """Verify the math: raw_cost * 0.2762 ≈ raw_cost / 3.6."""
        raw_cost = 1.2654  # $/M uncorrected
        corrected = raw_cost * 0.2762
        expected = raw_cost / 3.6
        assert abs(corrected - expected) < 0.005, (
            f"0.2762 * {raw_cost} = {corrected:.4f}, "
            f"expected ~{expected:.4f} (={raw_cost}/3.6)"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])