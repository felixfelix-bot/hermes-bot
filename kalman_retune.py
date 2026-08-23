#!/usr/bin/env python3
"""Auto-tune the Kalman filter's measurement noise (R) for burn_predictor.py.

The production filter (``burn_predictor.KalmanPredictor``) was shipped with a
fixed ``measurement_noise=50.0`` (R), which is catastrophically miscalibrated
for hourly token volumes that run ~1M–10M: it implies an observation noise of
σ≈7 tokens, drives the Kalman gain toward 1.0, collapses the covariance, and
turns the filter into a naive passthrough (0% 1σ coverage, 63%+ mean error).

This script fixes that by *grid-searching* R per key: it replays the real
hourly burn history through the exact production ``KalmanPredictor`` and picks
the R that minimises one-step-ahead MAPE. The chosen values are written to
``kalman_tuning.json``, which ``burn_predictor._load_tuning`` picks up on the
next proxy restart.

It never touches the running proxy process — it only reads the proxy's SQLite
DB and (in normal mode) writes the tuning file.

Data sufficiency is gauged from the ``kalman_samples`` table (populated by
``kalman_health.py --collect``): a key needs >= MIN_SAMPLES stored snapshots
AND >= MIN_SAMPLES hourly burn buckets before it is tuned. Keys with too
little data are skipped (no override written), so the adaptive fallback in
``burn_predictor`` still applies for them.

Usage:
    python3 kalman_retune.py             # retune and write kalman_tuning.json
    python3 kalman_retune.py --dry-run   # show what R it would set, write nothing
    python3 kalman_retune.py --report    # show current tuning + recent accuracy
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

# Import the production predictor + history helper from the same directory.
# (Don't reinvent the Kalman filter — reuse the real one so the backtest is
# faithful to production.)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from burn_predictor import (  # type: ignore
    KalmanPredictor,
    _get_burn_history,
    LOOKBACK_HOURS,
)

DB_PATH = os.path.expanduser("~/.hermes/bot/zai_usage.db")
TUNING_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kalman_tuning.json")
KEYS = ("ours", "friend")
MIN_SAMPLES = 5          # need >= this many hourly buckets / samples to tune
WARMUP = 2               # discard first N one-step errors (filter locking on)
# Grid of R multipliers applied to the sample variance. Spans under-trusting
# the data (×4) through heavily trusting it (×0.1).
R_MULTIPLIERS = (0.1, 0.25, 0.5, 1.0, 2.0, 4.0)
DEFAULT_Q = 1.0          # process noise — burn rate doesn't swing wildly hour-to-hour


def _utc(ts: float | None = None) -> str:
    if ts is None:
        ts = time.time()
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _samples_for_key(conn: sqlite3.Connection, key: str) -> int:
    """Count stored kalman_samples rows for a key (data-sufficiency proxy).

    The table is created by kalman_health.py; if it doesn't exist yet we
    treat the key as having zero samples.
    """
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM kalman_samples WHERE key = ?", (key,)
        ).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0


def _load_existing_tuning() -> dict:
    """Read the current tuning file, or {} if missing/unreadable."""
    try:
        if os.path.exists(TUNING_FILE):
            with open(TUNING_FILE) as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _data_variance(history: list[dict]) -> float:
    """Sample variance of the hourly token volumes, with the same 1e6 floor
    that burn_predictor's adaptive path applies."""
    vols = [float(h.get("tokens", 0)) for h in history]
    if not vols:
        return 1e6
    mean_v = sum(vols) / len(vols)
    var = sum((v - mean_v) ** 2 for v in vols) / max(len(vols) - 1, 1)
    return max(var, 1e6)


def _replay_mape(history: list[dict], R: float, Q: float = DEFAULT_Q) -> float:
    """Replay hourly volumes through KalmanPredictor(Q, R); return one-step MAPE.

    For each hour after the first, the filter's post-update state is used as the
    one-step-ahead prediction of the *next* hour, scored against the realised
    actual. MAPE is computed over post-warmup points where the actual is > 0
    (zero-volume hours can't yield a meaningful percentage and are skipped).
    Returns +inf if there is nothing to score.
    """
    kf = KalmanPredictor(process_noise=Q, measurement_noise=R)
    errs: list[float] = []
    for h in history:
        actual = float(h["tokens"])
        # one-step-ahead prediction = current post-update estimate, made BEFORE
        # we incorporate this measurement
        if kf._initialized:  # noqa: SLF001 (private but stable — used by kalman_health.py too)
            pred = float(kf.x[0, 0])
            if actual > 0:
                errs.append(abs(pred - actual) / actual * 100.0)
        kf.update(actual)
        kf.predict()

    scored = errs[WARMUP:]
    if not scored:
        return float("inf")
    return sum(scored) / len(scored)


def _tune_key(key: str, history: list[dict]) -> dict:
    """Grid-search R for one key. Returns a result dict with the best R and
    the full grid so callers can show diagnostics."""
    var = _data_variance(history)
    vols = [float(h.get("tokens", 0)) for h in history]
    mean_v = sum(vols) / len(vols) if vols else 0.0

    results = []
    for mult in R_MULTIPLIERS:
        R = var * mult
        mape = _replay_mape(history, R)
        results.append({"multiplier": mult, "R": R, "mape": mape})

    best = min(results, key=lambda r: r["mape"])
    return {
        "key": key,
        "samples": len(history),
        "mean_volume": round(mean_v),
        "variance": round(var),
        "best_R": best["R"],
        "best_multiplier": best["multiplier"],
        "best_mape": round(best["mape"], 2),
        "all_results": [
            {"multiplier": r["multiplier"], "R": round(r["R"]), "mape": round(r["mape"], 2)}
            for r in results
        ],
    }


def tune_all() -> dict:
    """Tune every key with sufficient data.

    Returns ``{"tuning": <dict to write>, "detail": {key: {...}}}``. Keys
    without enough data are omitted from the tuning dict (so burn_predictor's
    adaptive variance fallback still governs them) and recorded in ``detail``
    with a ``skipped`` reason.
    """
    if not os.path.exists(DB_PATH):
        return {
            "tuning": {"measurement_noise": {}, "process_noise": {},
                       "mape_at_tune": {}, "retuned_at": _utc()},
            "detail": {k: {"skipped": True, "reason": f"DB not found: {DB_PATH}"}
                       for k in KEYS},
        }

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    tuning: dict = {
        "measurement_noise": {},
        "process_noise": {},
        "mape_at_tune": {},
    }
    detail: dict = {}
    try:
        for key in KEYS:
            sample_count = _samples_for_key(conn, key)
            history = _get_burn_history(key, hours=LOOKBACK_HOURS)
            if sample_count < MIN_SAMPLES or len(history) < MIN_SAMPLES:
                detail[key] = {
                    "skipped": True,
                    "reason": (f"insufficient data "
                               f"(kalman_samples={sample_count}, "
                               f"history_buckets={len(history)}, "
                               f"need {MIN_SAMPLES})"),
                }
                continue
            res = _tune_key(key, history)
            res["kalman_samples"] = sample_count
            detail[key] = res
            tuning["measurement_noise"][key] = res["best_R"]
            tuning["process_noise"][key] = DEFAULT_Q
            tuning["mape_at_tune"][key] = res["best_mape"]
    finally:
        conn.close()

    tuning["retuned_at"] = _utc()
    return {"tuning": tuning, "detail": detail}


def write_tuning(tuning: dict) -> None:
    """Atomically write the tuning file (write tmp + os.replace)."""
    tmp = TUNING_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(tuning, f, indent=2, sort_keys=True)
    os.replace(tmp, TUNING_FILE)


def _effective_R(existing: dict, key: str, history: list[dict]) -> tuple[float, str]:
    """Return (R, source) currently effective for a key: tuning-file override
    if present, else the adaptive variance estimate."""
    R = existing.get("measurement_noise", {}).get(key)
    if R is not None:
        return float(R), "tuning-file"
    return _data_variance(history), "adaptive(variance)"


# ── CLI subcommands ──────────────────────────────────────────────────────────

def _cmd_report() -> int:
    existing = _load_existing_tuning()
    print(f"=== Current tuning ({TUNING_FILE}) ===")
    if not existing:
        print("  (no tuning file — burn_predictor uses adaptive variance R)")
    else:
        print(json.dumps(existing, indent=2))
    print()
    print(f"=== Recent one-step accuracy (replay, last {LOOKBACK_HOURS}h) ===")
    for key in KEYS:
        history = _get_burn_history(key, hours=LOOKBACK_HOURS)
        if len(history) < 2:
            print(f"  {key}: insufficient history ({len(history)} buckets)")
            continue
        R, src = _effective_R(existing, key, history)
        mape = _replay_mape(history, R)
        print(f"  {key}: R={R:,.0f} ({src}) -> MAPE={mape:.1f}%  "
              f"({len(history)} buckets)")
    return 0


def _cmd_dry_run() -> int:
    out = tune_all()
    print("=== Kalman R retune (DRY RUN — nothing written) ===")
    print(f"tuning file: {TUNING_FILE}\n")
    for key in KEYS:
        d = out["detail"].get(key, {})
        if d.get("skipped"):
            print(f"[{key}] SKIPPED: {d['reason']}\n")
            continue
        print(f"[{key}] samples={d['samples']}  kalman_samples={d.get('kalman_samples')}  "
              f"mean_vol={d['mean_volume']:,}  variance={d['variance']:,}")
        print(f"      best R = {d['best_R']:,.0f}  (x{d['best_multiplier']} var)  "
              f"MAPE={d['best_mape']}%")
        print("      grid:")
        for r in d["all_results"]:
            star = " <-- best" if abs(r["multiplier"] - d["best_multiplier"]) < 1e-9 else ""
            print(f"        x{r['multiplier']:<4}  R={r['R']:>14,}  MAPE={r['mape']:>7}%{star}")
        print()
    meas = out["tuning"]["measurement_noise"]
    if not meas:
        print("No keys had enough data to tune.")
    else:
        print("Would write measurement_noise:")
        print(json.dumps(meas, indent=2))
    return 0


def _cmd_retune() -> int:
    out = tune_all()
    meas = out["tuning"]["measurement_noise"]
    if not meas:
        print("No keys had enough data to tune — not writing a tuning file.")
        for key in KEYS:
            d = out["detail"].get(key, {})
            if d.get("skipped"):
                print(f"[{key}] {d['reason']}")
        return 1
    write_tuning(out["tuning"])
    print(f"Wrote {TUNING_FILE}")
    print(json.dumps(out["tuning"], indent=2))
    print()
    for key in KEYS:
        d = out["detail"].get(key, {})
        if d.get("skipped"):
            print(f"[{key}] skipped: {d['reason']}")
        else:
            print(f"[{key}] R={d['best_R']:,.0f}  (x{d['best_multiplier']} var)  "
                  f"MAPE={d['best_mape']}%")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true",
                   help="show what R values would be set; write nothing")
    g.add_argument("--report", action="store_true",
                   help="show current tuning file + recent one-step accuracy")
    args = p.parse_args(argv)

    if args.report:
        return _cmd_report()
    if args.dry_run:
        return _cmd_dry_run()
    return _cmd_retune()


if __name__ == "__main__":
    sys.exit(main())
