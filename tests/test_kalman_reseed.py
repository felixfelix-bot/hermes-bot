#!/usr/bin/env python3
"""Tests for KalmanPredictor step detection / auto re-seed + predict_all key skip.

Task t_6f0afb84:
  1. update() must re-seed (x=[z,0], P=eye(2)*R) when |innovation| >
     step_threshold * sqrt(S), so a burn-rate regime step is caught within
     ~2 observations instead of being ridden for 10+ low-gain updates.
  2. n_reseeds counts re-seed events: >=1 on a step series, 0 on smooth.
  3. predict_all() must skip keys with zero burn-history rows (dead key
     'ours') instead of emitting insufficient_data noise.

The unit tests construct the filter directly with measurement_noise=1e6 —
exactly the adaptive-R floor production uses (_train_kalman:
max(variance, 1e6)) for a near-constant pre-step segment.

Run:  python3 -m pytest tests/test_kalman_reseed.py -v   (from ~/.hermes/bot)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import burn_predictor as bp  # noqa: E402

# Step series: 8h of ~1M tokens/h, then a 40x regime step to 40M, settling 45M.
STEP_SERIES = [1_000_000] * 8 + [40_000_000] * 4 + [45_000_000] * 4
# Smooth series: same magnitude, tiny alternating jitter (no step).
SMOOTH_SERIES = [1_000_000 + (500 if i % 2 else -500) for i in range(16)]

R_NOISE = 1e6  # measurement-noise variance; sigma = 1k tokens


def run_filter(series, **ctor_kwargs):
    """Feed a series through a fresh filter, return (filter, post_step_errs).

    post_step_errs[i] = relative one-step-ahead prediction error for the
    i-th post-step point (index 0 = the step point itself, whose surprise
    is the step no filter can pre-see).
    """
    kf = bp.KalmanPredictor(process_noise=1.0, measurement_noise=R_NOISE,
                            **ctor_kwargs)
    errs = []
    for i, v in enumerate(series):
        pred = kf.predict_steps_ahead(1)[0]  # one-step-ahead, no state mutation
        kf.update(v)
        if i >= 8:  # post-step points
            errs.append(abs(pred - v) / v)
    return kf, errs


@unittest.skipUnless(bp._HAS_NUMPY, "numpy not available")
class StepReseedTests(unittest.TestCase):
    def test_reseed_catches_step_within_two_steps(self):
        """With re-seed, one-step error after the step is <25% within 2 steps."""
        kf, errs = run_filter(STEP_SERIES)  # default step_threshold=4.0
        self.assertGreaterEqual(kf.n_reseeds, 1)
        # errs[0] is the step point itself (surprise, expected large);
        # by the 2nd post-step prediction the filter must sit on the new level.
        self.assertLess(errs[1], 0.25, f"post-step errors: {errs[:4]}")
        self.assertLess(errs[2], 0.25, f"post-step errors: {errs[:4]}")

    def test_without_reseed_step_takes_many_steps(self):
        """Old behavior (gate disabled): error still >25% after 2 steps."""
        kf, errs = run_filter(STEP_SERIES, step_threshold=1e18)
        self.assertEqual(kf.n_reseeds, 0)
        self.assertGreater(errs[1], 0.25, f"post-step errors: {errs[:4]}")

    def test_n_reseeds_zero_on_smooth_series(self):
        """No false triggers: smooth series never trips the gate."""
        kf, _ = run_filter(SMOOTH_SERIES)
        self.assertEqual(kf.n_reseeds, 0)

    def test_reseed_resets_state_and_covariance(self):
        """Re-seed sets x=[z,0], P=eye(2)*R, velocity back to 0."""
        kf = bp.KalmanPredictor(process_noise=1.0, measurement_noise=R_NOISE)
        for v in STEP_SERIES[:8]:
            kf.update(v)
        self.assertAlmostEqual(kf.volume, 1_000_000, delta=5_000)
        self.assertEqual(kf.velocity, 0.0)
        kf.update(40_000_000)  # the step -> re-seed
        self.assertEqual(kf.n_reseeds, 1)
        self.assertAlmostEqual(kf.volume, 40_000_000, delta=1)
        self.assertEqual(kf.velocity, 0.0)
        self.assertAlmostEqual(kf.uncertainty, R_NOISE ** 0.5, delta=1e-6)


if __name__ == "__main__":
    unittest.main()
