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
import time
import unittest
from pathlib import Path
from unittest import mock

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


@unittest.skipUnless(bp._HAS_NUMPY, "numpy not available")
class PredictAllSkipTests(unittest.TestCase):
    def setUp(self):
        now = time.time()
        self.friend_history = [
            {"hour_ts": now - 3600 * (6 - i), "tokens": t}
            for i, t in enumerate([2.0e6, 2.1e6, 1.9e6, 2.0e6, 2.2e6, 2.1e6])
        ]
        self.histories = {"ours": [], "friend": self.friend_history}
        self.windows = [{"name": "5h", "used_pct": 40, "resets_at": now + 7200,
                         "window_hours": 5}]

    def _predict_all(self):
        with mock.patch.object(bp, "_get_burn_history",
                               side_effect=lambda k, **kw: self.histories[k]), \
             mock.patch.object(bp, "_get_quota_windows",
                               side_effect=lambda k: self.windows), \
             mock.patch.object(bp, "TUNING_FILE",
                               Path("/nonexistent-kalman-tuning.json")):
            bp._predictors.clear()
            return bp.predict_all()

    def test_dead_key_omitted_entirely(self):
        """Zero-history key produces NO entry (no insufficient_data noise)."""
        out = self._predict_all()
        self.assertIn("friend", out)
        self.assertNotIn("ours", out)
        self.assertNotIn("insufficient", str(out).lower())
        self.assertIn("timestamp", out)
        self.assertIn("method", out)

    def test_friend_still_produces_predictions(self):
        out = self._predict_all()
        preds = out["friend"]
        self.assertIsInstance(preds, list)
        self.assertEqual(len(preds), 1)
        self.assertEqual(preds[0]["window"], "5h")
        self.assertIn("burn_rate_tph", preds[0])
        # real projection, not a fallback row (those note insufficient data)
        self.assertNotIn("Insufficient", preds[0].get("note", ""))

    def test_live_keys_all_present(self):
        """Both keys with history -> both present (no over-skipping)."""
        self.histories["ours"] = self.friend_history
        out = self._predict_all()
        self.assertIn("ours", out)
        self.assertIn("friend", out)


if __name__ == "__main__":
    unittest.main()
