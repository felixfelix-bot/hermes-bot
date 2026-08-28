#!/usr/bin/env python3
"""exhaustion-gate.py — Kalman-driven dynamic pricing for routstr sell-side.

Computes per-pool exhaustion probability from burn_predictor predictions,
applies the dynamic pricing formula, and writes kalman_pricing.json in the
schema routstrd's kalman-pricing-bridge already watches.

Price formula:
    sell_price = max(
        $0.003/M (market floor),
        P_internal × 100,
        $0.003/M × (1/(1 - p_exhaust))^kappa
    )

    kappa = 3 normally; kappa = 5 + floor x20 when MAPE > 25%

    Hard cap: weekly_used >= 60% → delist (price = null)
    Session brake: session_used >= 50% → price × 4

State file: ~/.hermes/bot/kalman_pricing.json (the file routstrd watches)
MAPE log: ~/.hermes/bot/kalman_mape_log.jsonl (rolling accuracy tracker)
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import time
from pathlib import Path

HOME = Path.home()
BOT_DIR = HOME / ".hermes" / "bot"
VIZ_DIR = HOME / ".hermes" / "viz"
USAGE_DB = BOT_DIR / "zai_usage.db"
PRICING_FILE = BOT_DIR / "kalman_pricing.json"
MAPE_LOG = BOT_DIR / "kalman_mape_log.jsonl"
BURN_PREDICTOR_PATH = str(BOT_DIR)

BTC_PRICE_USD = 97500.0

MARKET_FLOOR_USD_PER_M = 0.003
P_INTERNAL_FLOOR = 0.001
QUOTA_PRESSURE_ASYMPTOTE = 1.5
WEEKLY_CAPS = {
    "ours":          14_000_000,
    "ollama_cloud":  3_500_000_000,
    "ollama_cloud_2": 3_500_000_000,
}
SESSION_CAPS = {
    "ours":          2_000_000,
    "ollama_cloud":  500_000_000,
    "ollama_cloud_2": 500_000_000,
}
WEEKLY_DELIST_THRESHOLD = 0.60
WEEKLY_RELIST_THRESHOLD = 0.58
SESSION_BRAKE_THRESHOLD = 0.50
SESSION_BRAKE_MULTIPLIER = 4.0
MAPE_THRESHOLD = 0.25
MAPE_WINDOW_HOURS = 72

KAPPA_NORMAL = 3
KAPPA_UNCERTAIN = 5
FLOOR_MULTIPLIER_UNCERTAIN = 20.0

STALE_THRESHOLD_S = 15 * 60


def _connect_db():
    return sqlite3.connect(f"file:{USAGE_DB}?mode=ro", uri=True, timeout=5)


def _weekly_used_pct(db, pool, now):
    cap = WEEKLY_CAPS.get(pool, 0)
    if cap == 0:
        return 0.0
    tok = db.execute(
        "SELECT COALESCE(SUM(total_tokens),0) FROM api_calls WHERE key_name=? AND ts > ?",
        (pool, now - 7 * 86400)
    ).fetchone()[0]
    return min(1.0, tok / cap) if cap > 0 else 0.0


def _session_used_pct(db, pool, now):
    cap = SESSION_CAPS.get(pool, 0)
    if cap == 0:
        return 0.0
    tok = db.execute(
        "SELECT COALESCE(SUM(total_tokens),0) FROM api_calls WHERE key_name=? AND ts > ?",
        (pool, now - 5 * 3600)
    ).fetchone()[0]
    return min(1.0, tok / cap) if cap > 0 else 0.0


def _compute_p_exhaust(projected_total_pct, uncertainty, burn_rate):
    if projected_total_pct is None:
        return 0.1
    p = projected_total_pct / 100.0
    if p <= 0:
        return 0.0
    if burn_rate > 0 and uncertainty > 0:
        z = (p - 1.0) / (uncertainty / max(burn_rate, 1))
        from math import erf, sqrt
        return 0.5 * (1 + erf(z / sqrt(2)))
    return min(1.0, max(0.0, p))


def _compute_mape():
    try:
        if not MAPE_LOG.exists():
            return 0.5
        lines = MAPE_LOG.read_text().strip().splitlines()
        if len(lines) < 4:
            return 0.5
        recent = [json.loads(l) for l in lines[-MAPE_WINDOW_HOURS:]]
        errors = []
        for e in recent:
            predicted = e.get("projected_total_pct", 0)
            actual = e.get("actual_pct", 0)
            if predicted > 0 and actual > 0:
                errors.append(abs(predicted - actual) / max(actual, 1))
        if not errors:
            return 0.5
        return sum(errors) / len(errors)
    except Exception:
        return 0.5


def _get_predictions(pool):
    try:
        sys.path.insert(0, BURN_PREDICTOR_PATH)
        from burn_predictor import predict_exhaustion
        results = predict_exhaustion(pool)
        for r in results:
            if r.get("window") == "weekly":
                return r
        return results[0] if results else {}
    except Exception:
        return {}


def _pools_to_sell():
    return ["ollama_cloud", "ollama_cloud_2", "ours"]


def compute_pricing():
    now = time.time()
    db = _connect_db()
    mape = _compute_mape()
    accuracy_ok = mape < MAPE_THRESHOLD
    kappa = KAPPA_NORMAL if accuracy_ok else KAPPA_UNCERTAIN
    floor_mult = 1.0 if accuracy_ok else FLOOR_MULTIPLIER_UNCERTAIN

    providers = {}
    sat_per_usd = 1e8 / BTC_PRICE_USD

    for pool in _pools_to_sell():
        weekly_pct = _weekly_used_pct(db, pool, now)
        session_pct = _session_used_pct(db, pool, now)

        pred = _get_predictions(pool)
        projected_total = pred.get("projected_total_pct", weekly_pct * 100)
        uncertainty = pred.get("uncertainty", 0)
        burn_rate = pred.get("burn_rate_tph", 0)

        p_exhaust = _compute_p_exhaust(projected_total, uncertainty, burn_rate)
        p_exhaust = min(0.99, max(0.01, p_exhaust))

        if weekly_pct >= WEEKLY_DELIST_THRESHOLD:
            providers[pool] = {
                "effective_rate_per_m": None,
                "base_rate_per_m": MARKET_FLOOR_USD_PER_M,
                "sat_per_token": None,
                "sat_per_m": None,
                "source": "delisted_weekly_cap_60",
                "is_measured": False,
                "confidence": 1 - p_exhaust,
                "velocity": 0,
                "kalman_updates": 0,
                "p_exhaust": p_exhaust,
                "weekly_used_pct": weekly_pct,
                "session_used_pct": session_pct,
                "delisted": True,
            }
            continue

        price = max(
            MARKET_FLOOR_USD_PER_M * floor_mult,
            P_INTERNAL_FLOOR * 100,
            MARKET_FLOOR_USD_PER_M * (1.0 / (1.0 - p_exhaust)) ** kappa,
        )

        if session_pct >= SESSION_BRAKE_THRESHOLD:
            price *= SESSION_BRAKE_MULTIPLIER

        sat_per_m = price * sat_per_usd
        sat_per_token = sat_per_m / 1e6

        providers[pool] = {
            "effective_rate_per_m": round(price, 6),
            "base_rate_per_m": MARKET_FLOOR_USD_PER_M,
            "sat_per_token": sat_per_token,
            "sat_per_m": sat_per_m,
            "source": "kalman_exhaustion_gate",
            "is_measured": True,
            "confidence": 1 - p_exhaust,
            "velocity": pred.get("velocity_tph2", 0),
            "kalman_updates": pred.get("kalman_updates", 0) if isinstance(pred.get("kalman_updates"), int) else 0,
            "p_exhaust": round(p_exhaust, 4),
            "weekly_used_pct": round(weekly_pct, 4),
            "session_used_pct": round(session_pct, 4),
            "mape": round(mape, 4),
            "kappa": kappa,
            "delisted": False,
        }

        _log_prediction(pool, projected_total, weekly_pct * 100, p_exhaust)

    db.close()

    pricing = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "btc_price_usd": BTC_PRICE_USD,
        "sat_per_usd": sat_per_usd,
        "providers": providers,
        "accuracy": {
            "mape_72h": round(mape, 4),
            "accuracy_ok": accuracy_ok,
            "kappa": kappa,
            "floor_multiplier": floor_mult,
        },
    }
    return pricing


def _log_prediction(pool, projected, actual, p_exhaust):
    try:
        entry = json.dumps({
            "ts": time.time(),
            "pool": pool,
            "projected_total_pct": projected,
            "actual_pct": actual,
            "p_exhaust": p_exhaust,
        }) + "\n"
        with open(MAPE_LOG, "a") as f:
            f.write(entry)
        if MAPE_LOG.exists() and MAPE_LOG.stat().st_size > 30_000:
            lines = MAPE_LOG.read_text().splitlines()
            MAPE_LOG.write_text("\n".join(lines[-500:]) + "\n")
    except Exception:
        pass


def _check_stale_input(pricing_file, max_age_s=STALE_THRESHOLD_S):
    try:
        if not pricing_file.exists():
            return True
        mtime = pricing_file.stat().st_mtime
        return (time.time() - mtime) > max_age_s
    except Exception:
        return True


def main():
    pricing = compute_pricing()

    PRICING_FILE.write_text(json.dumps(pricing, indent=2))
    print(f"[exhaustion-gate] Wrote {PRICING_FILE}")
    print(f"  BTC=${BTC_PRICE_USD}, sat/$={pricing['sat_per_usd']:.0f}")
    print(f"  MAPE={pricing['accuracy']['mape_72h']:.2%} ok={pricing['accuracy']['accuracy_ok']} kappa={pricing['accuracy']['kappa']}")
    for pool, data in pricing["providers"].items():
        if data.get("delisted"):
            print(f"  {pool}: DELISTED (weekly {data['weekly_used_pct']:.0%} >= 60%)")
        else:
            print(f"  {pool}: ${data['effective_rate_per_m']:.4f}/M ({data['sat_per_m']:.1f} sat/M) "
                  f"p_exhaust={data['p_exhaust']:.2f} wk={data['weekly_used_pct']:.0%} sess={data['session_used_pct']:.0%}")


if __name__ == "__main__":
    main()
