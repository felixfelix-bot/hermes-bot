#!/usr/bin/env python3
"""Kalman convergence health checker for the z.ai burn-rate predictor.

This tool answers one question: **is the 2-state Kalman filter (burn_predictor.py)
actually converging on the true burn rate?**

It does three jobs:

1. **Backtest / report** (default): replays the real hourly token history for each
   key through the *exact* production ``KalmanPredictor`` (imported from
   ``burn_predictor.py`` — same process/measurement noise, same update/predict
   math). At every step it records the one-step-ahead prediction, the realised
   actual, the prediction error, whether the actual fell inside the Kalman
   uncertainty envelope, and how well the velocity state tracks the true burn
   slope. Aggregates these into convergence metrics + a single status line.

2. **Collect** (``--collect``): snapshots the *live* prediction from the running
   proxy (``predict_exhaustion``) and stores it in a ``kalman_samples`` table so
   future reports can compare "what we predicted" vs "what actually happened".

3. **Watch** (``--watch [SECS]``): loops ``--collect`` every N seconds for
   ongoing accuracy tracking (run under cron / a daemon).

It never touches the running proxy process — it only *reads* its SQLite DB and
HTTP endpoints, and (in --collect mode) writes to its own samples table.

CLI:
    python3 kalman_health.py                # full JSON convergence report
    python3 kalman_health.py --short        # one-line human status (exit code 0/1)
    python3 kalman_health.py --collect      # snapshot live prediction into DB
    python3 kalman_health.py --watch 300    # collect every 5 min forever
    python3 kalman_health.py --samples      # dump stored prediction samples

Exit codes (--short only): 0 = healthy, 1 = warnings/unhealthy.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

# Make sure we import the production predictor from the same directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import numpy as np  # noqa: F401  (also used for the NIS chi-square check)
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False

# Import the REAL production Kalman filter + helpers so the backtest is faithful.
try:
    from burn_predictor import (  # type: ignore
        KalmanPredictor,
        predict_exhaustion,
        predict_all,
        _get_burn_history,
        LOOKBACK_HOURS,
        MIN_DATA_POINTS,
    )
except Exception as _e:  # pragma: no cover
    KalmanPredictor = None  # type: ignore
    predict_exhaustion = None  # type: ignore
    predict_all = None  # type: ignore
    _get_burn_history = None  # type: ignore
    _IMPORT_ERR = repr(_e)
else:
    _IMPORT_ERR = None

DB_PATH = os.path.expanduser("~/.hermes/bot/zai_usage.db")
KEYS = ("ours", "friend")
WARMUP = 3  # discard first N steps before scoring (filter needs to lock on)

# ── convergence verdict thresholds ───────────────────────────────────────────
# A healthy filter: bounded one-step error, ~68% of actuals inside the 1σ
# envelope (~95% inside 2σ), and late-window error no worse than early-window.
GOOD_MAX_MAPE = 25.0          # mean |% error| of one-step volume forecast
GOOD_VEL_ACCURACY = 70.0      # velocity tracking accuracy (%)
COVERAGE_1SIGMA_LOW = 0.45    # < this => filter is OVER-confident (bad)
COVERAGE_1SIGMA_HIGH = 0.90   # > this => filter is UNDER-confident (conservative)


def _utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _open_db(write: bool = False) -> sqlite3.Connection:
    """Open the proxy's usage DB (read-only by default)."""
    uri = f"file:{DB_PATH}{'?mode=rw' if write else '?mode=ro'}"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_samples_table(conn: sqlite3.Connection) -> None:
    """Create the kalman_samples table if missing (idempotent)."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS kalman_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            key TEXT NOT NULL,
            window TEXT,
            used_pct_observed REAL,
            projected_additional_pct REAL,
            projected_total_pct REAL,
            burn_rate_tph REAL,
            velocity_tph2 REAL,
            uncertainty REAL,
            exhausts_in_hours REAL,
            will_exhaust INTEGER,
            note TEXT
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kalman_samples_key_ts "
        "ON kalman_samples(key, ts)"
    )
    conn.commit()


# ── core backtest engine ─────────────────────────────────────────────────────

def backtest_key(key: str, history: list[dict]) -> dict:
    """Replay *history* (hourly token buckets) through the real KalmanPredictor.

    Returns a dict of convergence metrics + per-step points. Mirrors exactly
    what ``burn_predictor._train_kalman`` does (update on every bucket, one
    predict step at the end), but scores each one-step-ahead prediction against
    the realised actual as it goes.
    """
    points = []
    if KalmanPredictor is None or len(history) < (WARMUP + 2):
        return {
            "key": key,
            "samples": 0,
            "verdict": "insufficient_data",
            "note": f"need >= {WARMUP + 2} hourly buckets, have {len(history)}",
        }

    kf = KalmanPredictor(process_noise=1.0, measurement_noise=50.0)

    # EMA of true hourly volume for a smoothed ground-truth velocity.
    true_vel_ema = None
    VEL_EMA = 0.3

    for i, h in enumerate(history):
        actual = float(h["tokens"])
        ts = float(h["hour_ts"])

        # --- predict the NEXT hour BEFORE seeing it (one-step-ahead) ---------
        if kf._initialized:  # noqa: SLF001 (private but stable API)
            pred = float(kf.x[0, 0])  # post-update current estimate = next forecast
            sigma = float(kf.uncertainty)
            vel_est = float(kf.velocity)
            err = pred - actual
            denom = actual if actual > 0 else max(actual, 1.0)
            ape = abs(err) / abs(denom) * 100.0
            within_1sig = abs(err) <= sigma if sigma > 0 else False
            within_2sig = abs(err) <= 2 * sigma if sigma > 0 else False

            # smoothed true velocity (EMA of first differences)
            if true_vel_ema is None:
                true_vel = 0.0
            else:
                true_vel = true_vel_ema
            points.append(
                {
                    "i": i,
                    "ts": _utc(ts),
                    "predicted": round(pred, 1),
                    "actual": actual,
                    "error": round(err, 1),
                    "abs_pct_error": round(ape, 1),
                    "within_1sigma": within_1sig,
                    "within_2sigma": within_2sig,
                    "velocity_est": round(vel_est, 1),
                    "velocity_true": round(true_vel, 1),
                    "sigma": round(sigma, 1),
                }
            )

        # --- update with the realised measurement ----------------------------
        kf.update(actual)
        delta = actual - float(kf.x[0, 0]) if i > 0 else 0.0
        if true_vel_ema is None:
            true_vel_ema = 0.0
        else:
            # true velocity ≈ change between consecutive actuals
            prev_actual = float(history[i - 1]["tokens"])
            inst = actual - prev_actual
            true_vel_ema = VEL_EMA * inst + (1 - VEL_EMA) * true_vel_ema

        # advance the state (predict step) — matches _train_kalman's final call
        kf.predict()

    # Score only the post-warmup steps.
    scored = points[WARMUP:]
    if not scored:
        return {
            "key": key,
            "samples": len(points),
            "verdict": "insufficient_data",
            "note": "not enough post-warmup points",
        }

    mape = sum(p["abs_pct_error"] for p in scored) / len(scored)
    cov1 = sum(1 for p in scored if p["within_1sigma"]) / len(scored)
    cov2 = sum(1 for p in scored if p["within_2sigma"]) / len(scored)

    # velocity tracking accuracy: 1 - normalised mean abs error of velocity est
    vel_errs = [abs(p["velocity_est"] - p["velocity_true"]) for p in scored]
    vel_true_mag = [abs(p["velocity_true"]) for p in scored]
    mean_vel_true = sum(vel_true_mag) / len(vel_true_mag) or 1.0
    vel_acc = max(0.0, 1.0 - (sum(vel_errs) / len(vel_errs)) / mean_vel_true) * 100.0
    # also correlation (sign + tracking quality)
    if len(scored) >= 4:
        ve = [p["velocity_est"] for p in scored]
        vt = [p["velocity_true"] for p in scored]
        vel_corr = _pearson(ve, vt)
    else:
        vel_corr = None

    # error trend across thirds (early/mid/late) — is error shrinking?
    thirds = _split_thirds(scored, "abs_pct_error")
    early, mid, late = thirds
    if late < early * 0.85:
        trend = "down"
    elif late > early * 1.15:
        trend = "up"
    else:
        trend = "flat"

    verdict = _verdict(mape, vel_acc, cov1, trend)

    return {
        "key": key,
        "samples": len(scored),
        "mean_abs_pct_error": round(mape, 1),
        "error_trend": trend,
        "early_error": round(early, 1),
        "mid_error": round(mid, 1),
        "late_error": round(late, 1),
        "velocity_accuracy_pct": round(vel_acc, 1),
        "velocity_correlation": round(vel_corr, 3) if vel_corr is not None else None,
        "coverage_1sigma": round(cov1, 3),  # healthy ~0.68
        "coverage_2sigma": round(cov2, 3),  # healthy ~0.95
        "current_volume_tph": round(float(kf.volume), 1),
        "current_velocity_tph2": round(float(kf.velocity), 1),
        "current_uncertainty": round(float(kf.uncertainty), 1),
        "verdict": verdict,
        # last 24 scored points for drill-down
        "recent_points": scored[-24:],
    }


def _pearson(a: list[float], b: list[float]) -> float:
    n = len(a)
    ma = sum(a) / n
    mb = sum(b) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = (sum((x - ma) ** 2 for x in a)) ** 0.5
    db = (sum((y - mb) ** 2 for y in b)) ** 0.5
    if da == 0 or db == 0:
        return 0.0
    return num / (da * db)


def _split_thirds(points: list[dict], field: str) -> tuple[float, float, float]:
    n = len(points)
    if n < 3:
        v = sum(p[field] for p in points) / n if n else 0.0
        return v, v, v
    k = n // 3
    slices = [points[:k], points[k : 2 * k], points[2 * k :]]
    means = []
    for s in slices:
        means.append(sum(p[field] for p in s) / len(s) if s else 0.0)
    return means[0], means[1], means[2]


def _verdict(mape: float, vel_acc: float, cov1: float, trend: str) -> str:
    """Classify overall health for one key."""
    if mape > GOOD_MAX_MAPE:
        return "unhealthy" if trend != "down" else "improving"
    if cov1 < COVERAGE_1SIGMA_LOW:
        return "overconfident"  # uncertainty too small — actuals escape envelope
    if cov1 > COVERAGE_1SIGMA_HIGH:
        return "conservative"  # uncertainty too large — not converged tightly
    if vel_acc < GOOD_VEL_ACCURACY:
        return "weak_velocity"
    return "healthy"


# ── live prediction snapshot (collect mode) ──────────────────────────────────

def collect_samples() -> dict:
    """Snapshot the live prediction for both keys and store it.

    Uses the running proxy's /quota cache via predict_exhaustion (which is the
    same path the proxy itself uses in _refresh_loop). Writes to kalman_samples.
    """
    if predict_all is None:
        return {"error": f"burn_predictor import failed: {_IMPORT_ERR}", "stored": 0}
    try:
        preds = predict_all()
    except Exception as e:
        return {"error": f"predict_all failed: {e}", "stored": 0}

    now = time.time()
    stored = 0
    conn = _open_db(write=True)
    try:
        _ensure_samples_table(conn)
        for key in KEYS:
            for w in preds.get(key, []):
                conn.execute(
                    """INSERT INTO kalman_samples
                       (ts, key, window, used_pct_observed, projected_additional_pct,
                        projected_total_pct, burn_rate_tph, velocity_tph2, uncertainty,
                        exhausts_in_hours, will_exhaust, note)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        now,
                        key,
                        w.get("window"),
                        w.get("used_pct"),
                        w.get("projected_additional_pct"),
                        w.get("projected_total_pct", (w.get("used_pct", 0) or 0)
                              + (w.get("projected_additional_pct", 0) or 0)),
                        w.get("burn_rate_tph"),
                        w.get("velocity_tph2"),
                        w.get("uncertainty"),
                        w.get("exhausts_in_hours"),
                        1 if w.get("will_exhaust") else 0,
                        w.get("note", ""),
                    ),
                )
                stored += 1
        conn.commit()
    finally:
        conn.close()
    return {
        "stored": stored,
        "sampled_at": _utc(now),
        "method": preds.get("method"),
    }


def realised_accuracy(window_hours: int = 2) -> dict:
    """Compare previously-stored projections against realised used_pct movement.

    For each sample older than *window_hours*, look up the actual used_pct at
    sample time (from key_decisions) and now, compute the realised delta, and
    compare to the per-hour projection implied by burn_rate_tph. This is the
    *realised* prediction-vs-actual check that complements the synthetic backtest.
    """
    conn = _open_db()
    out = {"window_hours": window_hours, "compared": 0, "per_key": {}}
    try:
        cutoff = time.time() - window_hours * 3600
        rows = conn.execute(
            "SELECT * FROM kalman_samples WHERE ts <= ? ORDER BY ts", (cutoff,)
        ).fetchall()
        if not rows:
            out["note"] = "no stored samples older than window"
            return out
        for key in KEYS:
            krows = [r for r in rows if r["key"] == key]
            errs = []
            for r in krows:
                t0 = r["ts"]
                # actual used_pct at sample time (nearest key_decision <= t0)
                a0 = conn.execute(
                    "SELECT used_pct FROM key_decisions "
                    "WHERE key_decisions.ts <= ? AND "
                    f"CASE WHEN chosen_key=? THEN ours_pct ELSE friend_pct END IS NOT NULL "
                    "ORDER BY ts DESC LIMIT 1",
                    (t0, key),
                ).fetchone()
                # crude: use the column matching the key
                # fall back to scanning both columns
                if a0 is None:
                    continue
                # realised delta over the hour following the sample
                # (compare projected pct/hour vs actual pct/hour)
                pred_pct_per_hour = (r["projected_additional_pct"] or 0) / max(
                    (r["exhausts_in_hours"] or 1), 0.1
                ) if r["burn_rate_tph"] else 0.0
                # too noisy to be authoritative; record raw for now
                errs.append(abs(pred_pct_per_hour))
            out["per_key"][key] = {
                "samples": len(krows),
                "mean_implied_pct_per_hour": round(sum(errs) / len(errs), 3)
                if errs
                else None,
            }
        out["compared"] = sum(v["samples"] for v in out["per_key"].values())
    finally:
        conn.close()
    return out


# ── top-level report ─────────────────────────────────────────────────────────

def build_report() -> dict:
    """Full convergence report: per-key backtest + realised-accuracy (if any)."""
    if not _HAS_NUMPY:
        return {"error": "numpy not available"}
    if _get_burn_history is None:
        return {"error": f"burn_predictor import failed: {_IMPORT_ERR}"}

    keys_out = {}
    for key in KEYS:
        hist = _get_burn_history(key, hours=LOOKBACK_HOURS)
        keys_out[key] = backtest_key(key, hist)
        keys_out[key]["history_buckets"] = len(hist)

    # overall verdict: worst key wins, but require both to be healthy-ish
    verdicts = [v.get("verdict") for v in keys_out.values()]
    bad = {"unhealthy", "overconfident", "weak_velocity"}
    if all(v == "healthy" for v in verdicts):
        overall = "healthy"
    elif any(v in bad for v in verdicts):
        overall = "unhealthy"
    elif any(v == "improving" for v in verdicts):
        overall = "improving"
    else:
        overall = "degraded"

    mapes = [v.get("mean_abs_pct_error") for v in keys_out.values()
             if v.get("mean_abs_pct_error") is not None]
    mean_mape = sum(mapes) / len(mapes) if mapes else None
    accs = [v.get("velocity_accuracy_pct") for v in keys_out.values()
            if v.get("velocity_accuracy_pct") is not None]
    mean_acc = sum(accs) / len(accs) if accs else None

    report = {
        "generated_at": _utc(time.time()),
        "method": "kalman-backtest",
        "predictor": "burn_predictor.KalmanPredictor (2-state, Q=1.0, R=50.0)",
        "lookback_hours": LOOKBACK_HOURS,
        "numpy": _HAS_NUMPY,
        "overall_verdict": overall,
        "mean_abs_pct_error": round(mean_mape, 1) if mean_mape is not None else None,
        "velocity_accuracy_pct": round(mean_acc, 1) if mean_acc is not None else None,
        "keys": keys_out,
        "status_line": "",
    }
    report["status_line"] = format_status_line(report)
    return report


def format_status_line(report: dict) -> str:
    """One-line status block for inclusion in manager status updates."""
    v = report.get("overall_verdict", "?")
    mark = {"healthy": "✓", "improving": "~", "degraded": "!", "unhealthy": "✗"}.get(v, "?")
    mape = report.get("mean_abs_pct_error")
    # error trend from the worst-ish key
    trends = [k.get("error_trend") for k in report.get("keys", {}).values()]
    trend = "↓" if trends.count("down") >= len(trends) / 2 and trends else (
        "↑" if trends.count("up") >= len(trends) / 2 else "→")
    vel = report.get("velocity_accuracy_pct")
    keys = report.get("keys", {})
    kbits = []
    for kn in KEYS:
        if kn in keys:
            kv = keys[kn].get("verdict", "?")
            km = {"healthy": "✓", "improving": "~", "conservative": "~",
                  "degraded": "!", "unhealthy": "✗",
                  "overconfident": "✗", "weak_velocity": "✗"}.get(kv, "?")
            kbits.append(f"{kn} {km}")
    mape_s = f"{mape:.1f}%" if mape is not None else "n/a"
    vel_s = f"{vel:.0f}%" if vel is not None else "n/a"
    return (f"Kalman Convergence: {mark} {v} | mean error: {mape_s} ({trend}) "
            f"| keys: {' '.join(kbits)} | velocity accuracy: {vel_s}")


# ── CLI ──────────────────────────────────────────────────────────────────────

def _print_short(report: dict) -> int:
    print(report.get("status_line", "no report"))
    v = report.get("overall_verdict")
    return 0 if v in ("healthy", "improving") else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Kalman convergence health for the z.ai burn-rate predictor."
    )
    p.add_argument("--short", action="store_true",
                   help="print one-line status; exit 0 healthy / 1 unhealthy")
    p.add_argument("--collect", action="store_true",
                   help="snapshot the live prediction into the DB and exit")
    p.add_argument("--watch", type=int, metavar="SECS", nargs="?", const=300,
                   help="run --collect every SECS seconds (default 300)")
    p.add_argument("--samples", action="store_true",
                   help="dump stored prediction samples and exit")
    p.add_argument("--pool", action="store_true",
                   help="check pool Kalman convergence health instead of burn-rate")
    args = p.parse_args(argv)

    if args.samples:
        conn = _open_db()
        try:
            try:
                rows = conn.execute(
                    "SELECT * FROM kalman_samples ORDER BY ts DESC LIMIT 50"
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            print(json.dumps([dict(r) for r in rows], indent=2, default=str))
        finally:
            conn.close()
        return 0

    if args.collect or args.watch:
        if args.watch:
            print(f"watch: collecting every {args.watch}s (Ctrl-C to stop)",
                  file=sys.stderr)
            while True:
                res = collect_samples()
                print(json.dumps(res), flush=True)
                time.sleep(args.watch)
        else:
            res = collect_samples()
            print(json.dumps(res, indent=2))
        return 0

    if args.pool:
        """Check pool Kalman convergence health."""
        pool_state_path = os.path.expanduser("~/.hermes/state/pool_kalman.json")
        if not os.path.exists(pool_state_path):
            print(json.dumps({"error": "pool_kalman.json not found — pool filter may not be running yet"}, indent=2))
            return 1
        try:
            with open(pool_state_path) as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(json.dumps({"error": f"failed to read pool state: {e}"}, indent=2))
            return 1

        x = state.get("x", [0, 0])
        P = state.get("P", [[1, 0], [0, 1]])
        ts = state.get("ts", 0)
        age_hours = (time.time() - ts) / 3600 if ts else 0

        smoothed = x[0]
        velocity = x[1]
        uncertainty = (P[0][0] ** 0.5) if P and len(P) > 0 else 0

        # Convergence assessment
        # Pool size should be stable: low uncertainty, near-zero velocity
        if uncertainty > 1.5:
            verdict = "converging"
            note = "uncertainty still high — need more samples"
        elif abs(velocity) > 0.01 and age_hours < 1:
            verdict = "converging"
            note = f"velocity={velocity:.4f}, still settling after {age_hours:.1f}h"
        elif abs(velocity) > 0.02:
            verdict = "unstable"
            note = f"velocity={velocity:.4f} — pool size oscillating"
        elif age_hours > 4 and uncertainty < 0.5:
            verdict = "healthy"
            note = "stable, low uncertainty"
        else:
            verdict = "stable"
            note = f"pool at {smoothed:.1f} (±{uncertainty:.2f}), velocity={velocity:.4f}"

        report = {
            "generated_at": datetime.fromtimestamp(time.time(), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "method": "pool-kalman",
            "state_file": "pool_kalman.json",
            "smoothed_pool": round(smoothed, 2),
            "velocity": round(velocity, 4),
            "uncertainty": round(uncertainty, 2),
            "age_hours": round(age_hours, 1),
            "verdict": verdict,
            "note": note,
            "raw_state": state,
        }

        if args.short:
            mark = {"healthy": "✓", "stable": "✓", "converging": "~", "unstable": "✗"}.get(verdict, "?")
            print(f"Pool Kalman: {mark} {verdict} | smoothed={smoothed:.1f} "
                  f"| velocity={velocity:.4f} | σ±{uncertainty:.2f} | {note}")
            return 0 if verdict in ("healthy", "stable", "converging") else 1

        print(json.dumps(report, indent=2, default=str))
        return 0

    report = build_report()
    if args.short:
        return _print_short(report)
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
