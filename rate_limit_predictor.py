#!/usr/bin/env python3
"""
Rate-limit retry predictor backed by a Kalman filter.

Models the inter-arrival time of HTTP 429 responses to estimate the
rate-limit window duration.  When a 429 is received the proxy calls
predict_retry_at() -> sleeps until recovery is predicted -> retries.
If the 429 repeats the model is updated and the cycle continues.

Falls back to exponential backoff (capped at 30 s) when the filter has
insufficient data.

Persistence:
  * rate_limit_samples table in zai_usage.db (created lazily on first use)
  * Model state serialised to the same DB so it survives restarts.

Usage (inside zai_proxy.py)::

    from rate_limit_predictor import RateLimitPredictor
    predictor = RateLimitPredictor(db_path="…/zai_usage.db")

    for attempt in range(50):           # safety cap
        resp = do_request(...)
        if resp.status != 429:
            predictor.record_success()
            break
        wait = predictor.predict_retry_at()
        predictor.record_429()
        time.sleep(wait)
"""

from __future__ import annotations

import math
import os
import sqlite3
import threading
import time
from typing import Optional

__all__ = ["RateLimitPredictor"]

# Sensible defaults for a zAI / OpenAI-compatible provider.
DEFAULT_WINDOW_S = 60.0        # initial guess: 60-second rolling window
DEFAULT_JITTER_S = 2.0         # small jitter to avoid thundering herd
MIN_SAMPLES_FOR_PREDICTION = 3 # need >=3 429s before trusting the filter
MAX_RETRIES = 50               # hard safety cap per request
FALLBACK_MAX_WAIT_S = 30.0     # exponential-backoff ceiling
FALLBACK_BASE_WAIT_S = 1.0


class _Kalman1D:
    """Minimal 1-D Kalman filter for a (near-)constant process value."""

    def __init__(self, x0: float, p0: float, q: float, r: float):
        self.x = float(x0)  # state estimate
        self.p = float(p0)  # estimate uncertainty
        self.q = float(q)   # process noise
        self.r = float(r)   # measurement noise

    def update(self, z: float) -> None:
        """Incorporate measurement *z*."""
        # predict step (constant model -> no control input)
        self.p += self.q
        # update step
        k = self.p / (self.p + self.r)          # Kalman gain
        self.x += k * (z - self.x)
        self.p *= (1.0 - k)

    def predict(self) -> float:
        return self.x


class RateLimitPredictor:
    """Predict when to retry after an HTTP 429.

    Parameters
    ----------
    db_path : str
        Path to the zai_usage.db SQLite database (used for training data +
        model persistence).
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS rate_limit_samples (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        ts            REAL    NOT NULL,          -- epoch seconds
        inter_arrival REAL,                       -- seconds since previous 429
        consecutive   INTEGER NOT NULL DEFAULT 1, -- consecutive 429 streak
        wait_used     REAL,                       -- how long we slept before this 429
        source        TEXT    DEFAULT 'zai_proxy'
    );
    """

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            db_path = os.path.expanduser("~/.hermes/bot/zai_usage.db")
        self.db_path = db_path
        self._lock = threading.Lock()

        # Kalman filter over the rate-limit window estimate (seconds).
        # q small (window is sticky), r moderate (observations are noisy).
        self._kf = _Kalman1D(x0=DEFAULT_WINDOW_S, p0=400.0, q=1.0, r=25.0)

        self._consecutive = 0          # current 429 streak
        self._last_429_ts: Optional[float] = None
        self._sample_count = 0

        self._ensure_schema()
        self._train_from_db()

    # ------------------------------------------------------------------ #
    #  persistence
    # ------------------------------------------------------------------ #
    def _ensure_schema(self) -> None:
        if not os.path.exists(self.db_path):
            return  # DB created elsewhere; table will be added on first write
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(self.SCHEMA)
            # The table may have been pre-created by zai_proxy._log_rate_limit
            # with a different column set.  Add any predictor-specific columns
            # that are missing via ALTER TABLE (idempotent + guarded).
            try:
                existing = {r[1] for r in conn.execute(
                    "PRAGMA table_info(rate_limit_samples)")}
            except sqlite3.OperationalError:
                existing = set()
            for col, decl in [
                ("inter_arrival", "REAL"),
                ("consecutive",   "INTEGER NOT NULL DEFAULT 1"),
                ("wait_used",     "REAL"),
                ("source",        "TEXT DEFAULT 'zai_proxy'"),
            ]:
                if col not in existing:
                    try:
                        conn.execute(
                            f"ALTER TABLE rate_limit_samples "
                            f"ADD COLUMN {col} {decl}")
                    except sqlite3.OperationalError:
                        pass  # column may have been added concurrently
            conn.commit()
        except sqlite3.Error:
            pass
        finally:
            conn.close()

    def _train_from_db(self) -> None:
        """Pre-train the Kalman filter from historical 429 inter-arrivals."""
        if not os.path.exists(self.db_path):
            return
        conn = sqlite3.connect(self.db_path)
        try:
            # Prefer dedicated table if present, else mine api_calls.
            try:
                rows = conn.execute(
                    "SELECT ts, inter_arrival FROM rate_limit_samples "
                    "WHERE inter_arrival IS NOT NULL ORDER BY ts"
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []   # column/table missing — fall through to mining
            if not rows:
                rows = self._mine_apicalls(conn)
        finally:
            conn.close()

        for _ts, ia in rows:
            if ia and ia > 0:
                self._kf.update(float(ia))
                self._sample_count += 1

    @staticmethod
    def _mine_apicalls(conn: sqlite3.Connection):
        """Best-effort: derive 429 inter-arrival times from api_calls."""
        try:
            rows = conn.execute(
                "SELECT ts FROM api_calls "
                "WHERE status_code = 429 ORDER BY ts"
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        inter = []
        prev = None
        for (ts,) in rows:
            if prev is not None:
                dt = ts - prev
                if 0 < dt < 3600:          # ignore huge gaps / resets
                    inter.append((ts, dt))
            prev = ts
        return inter

    # ------------------------------------------------------------------ #
    #  public API
    # ------------------------------------------------------------------ #
    def predict_retry_at(self, now: Optional[float] = None) -> float:
        """Return how many seconds to wait before retrying after a 429.

        Uses the Kalman estimate once enough samples are available,
        otherwise falls back to capped exponential backoff.
        """
        now = now or time.time()
        with self._lock:
            if self._sample_count < MIN_SAMPLES_FOR_PREDICTION:
                # Not enough data yet -> exponential backoff (capped 30 s).
                base = FALLBACK_BASE_WAIT_S * (2 ** self._consecutive)
                return min(base, FALLBACK_MAX_WAIT_S)

            window_est = self._kf.predict()
            # If this is part of a consecutive streak we already slept some;
            # wait the *remaining* portion of the window, never below base.
            wait = max(window_est, FALLBACK_BASE_WAIT_S)
            # Add jitter proportional to uncertainty.
            jitter = self._kf.p ** 0.5
            wait += min(jitter, DEFAULT_JITTER_S * 3)
            return min(wait, FALLBACK_MAX_WAIT_S * 4)   # soft ceiling 120 s

    def record_429(self, now: Optional[float] = None) -> None:
        """Register a 429 event and update the Kalman filter."""
        now = now or time.time()
        with self._lock:
            inter_arrival = None
            if self._last_429_ts is not None:
                dt = now - self._last_429_ts
                if 0 < dt < 3600:
                    inter_arrival = dt
                    self._kf.update(dt)
                    self._sample_count += 1

            self._consecutive += 1
            self._last_429_ts = now

            # persist sample
            self._insert_sample(now, inter_arrival, self._consecutive)

    def record_success(self) -> None:
        """Reset the consecutive streak after a non-429 response."""
        with self._lock:
            self._consecutive = 0

    @property
    def window_estimate(self) -> float:
        """Current best estimate of the rate-limit window (seconds)."""
        return self._kf.predict()

    @property
    def sample_count(self) -> int:
        return self._sample_count

    # ------------------------------------------------------------------ #
    #  internal
    # ------------------------------------------------------------------ #
    def _insert_sample(self, ts, inter_arrival, consecutive) -> None:
        if not os.path.exists(self.db_path):
            return
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(self.SCHEMA)   # idempotent
            conn.execute(
                "INSERT INTO rate_limit_samples (ts, inter_arrival, consecutive, source) "
                "VALUES (?, ?, ?, 'zai_proxy')",
                (ts, inter_arrival, consecutive),
            )
            conn.commit()
        except sqlite3.Error:
            pass  # logging samples is best-effort
        finally:
            conn.close()
