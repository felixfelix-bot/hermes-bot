#!/usr/bin/env python3
"""exhaust_weight.py — soft cost preference for lanes predicted to exhaust.

Reads the latest ``kalman_samples`` row(s) per key from ``zai_usage.db`` and
returns a cost multiplier that de-preferences lanes predicted to exhaust their
quota soon. This is a SOFT preference (Felix policy: ALERTS-NOT-BLOCKS) — it
never removes a lane from the candidate set and never touches the pressure FSM;
it only inflates ``effective_cost`` so the lane sorts lower in the
cheapest-first ordering produced by ``flat_router.select_provider()``.

Formula (per key, latest sample):
    multiplier = 1.0
    if will_exhaust == 1 and exhausts_in_hours is not None:
        urgency   = 1.0 - min(exhausts_in_hours / HORIZON, 1.0)
        multiplier = 1.0 + ALPHA * urgency

    ALPHA   = 0.5   # max cost inflation when exhaustion is imminent
    HORIZON = 6.0   # hours — lanes predicted to exhaust within this window
                    # get progressively de-preferred (0h → ×1.5, ≥6h → ×1.0)

Graceful degradation: empty/stale (>2h old) samples, a missing table, or an
unreadable DB all yield multiplier 1.0 (no effect). This module never raises
into routing — every failure is caught and logged to stderr at most once.

The ``kalman_samples`` table is written by ``kalman_health.py --collect`` (and
the proxy's Kalman predictor) with one row per (key, window) per snapshot. The
latest snapshot per key may carry several windows (e.g. ``5-hour`` and
``weekly``); we treat the key as exhausting if ANY window at the latest
timestamp predicts exhaustion, and use the most urgent (smallest)
``exhausts_in_hours`` among those windows.

Author: Hermes Agent (manager profile)
Date: 2026-09-02
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

# ── Tuning constants (documented at top of module) ───────────────────────────
ALPHA = 0.5        # max cost inflation when exhaustion is imminent
HORIZON = 6.0      # hours — de-preference window for predicted exhaustion
MAX_SAMPLE_AGE_S = 2 * 3600   # samples older than this are treated as stale

DB_PATH = os.path.expanduser("~/.hermes/bot/zai_usage.db")

# Log-once guard: emit a single stderr line per process on the first failure,
# so a broken DB doesn't spam the proxy log on every routing decision.
_logged_error = False


def _log_once(msg: str) -> None:
    """Log a message to stderr at most once per process."""
    global _logged_error
    if not _logged_error:
        _logged_error = True
        print(f"[exhaust_weight] {msg}", file=sys.stderr)


def _latest_exhaust_state(
    key_name: str,
    db_path: str | None = None,
    now: float | None = None,
) -> tuple[bool, float | None, float] | None:
    """Return (will_exhaust, exhausts_in_hours, sample_ts) for a key's latest sample.

    Returns None if the key has no samples, the table is missing, or the DB is
    unreadable. ``exhausts_in_hours`` is the most urgent (smallest) value among
    the latest snapshot's ``will_exhaust=1`` windows; None if no window predicts
    exhaustion.
    """
    path = db_path or DB_PATH
    now = time.time() if now is None else now

    conn = sqlite3.connect(path, timeout=5)
    try:
        conn.row_factory = sqlite3.Row
        # Cheap single query: latest rows for this key (a snapshot has one row
        # per window, so fetch a handful and filter to the latest timestamp).
        rows = conn.execute(
            "SELECT ts, exhausts_in_hours, will_exhaust FROM kalman_samples "
            "WHERE key = ? ORDER BY ts DESC LIMIT 10",
            (key_name,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return None

    latest_ts = rows[0]["ts"]
    latest = [r for r in rows if r["ts"] == latest_ts]

    will = any(bool(r["will_exhaust"]) for r in latest)
    if not will:
        return (False, None, latest_ts)

    exh = [
        r["exhausts_in_hours"]
        for r in latest
        if r["will_exhaust"] and r["exhausts_in_hours"] is not None
    ]
    exhausts = min(exh) if exh else None
    return (True, exhausts, latest_ts)


def exhaust_multiplier(
    key_name: str,
    db_path: str | None = None,
    now: float | None = None,
) -> float:
    """Return the soft cost multiplier for a key based on its latest exhaust sample.

    Returns 1.0 (no effect) in every degraded case: no sample, stale sample
    (>2h old), missing table, unreadable DB, or ``will_exhaust=0``. Never raises.
    """
    try:
        state = _latest_exhaust_state(key_name, db_path, now)
        if state is None:
            return 1.0

        will, exhausts, sample_ts = state
        if not will or exhausts is None:
            return 1.0

        # Stale sample → no effect (the prediction is no longer actionable).
        if (time.time() if now is None else now) - sample_ts > MAX_SAMPLE_AGE_S:
            return 1.0

        urgency = 1.0 - min(exhausts / HORIZON, 1.0)
        return 1.0 + ALPHA * urgency
    except Exception as e:  # noqa: BLE001 — never raise into routing
        _log_once(f"multiplier lookup failed for {key_name!r}: {e!r}")
        return 1.0
