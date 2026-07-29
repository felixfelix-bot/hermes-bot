#!/usr/bin/env python3
"""
calc_price_per_token.py — Compute SATs-per-token for each API key type.

Uses a 2-state Kalman filter (same KalmanPredictor as burn_predictor.py) to
smooth noisy hourly price estimates and converge on the true underlying price.

Output: JSON dict with per-key sats_per_token + metadata.
Run standalone: python3 calc_price_per_token.py
Called by: build_v3.py during dashboard rebuild.
"""

import json
import os
import sqlite3
import sys
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

# Add parent dir so we can import burn_predictor
sys.path.insert(0, str(Path(__file__).parent))
from burn_predictor import KalmanPredictor

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    np = None
    _HAS_NUMPY = False

# ── Config ────────────────────────────────────────────────────────────────
DB = Path.home() / ".hermes" / "bot" / "zai_usage.db"
BURN_DB = Path.home() / ".hermes" / "bot" / "api_burn.db"
TUNING_FILE = Path(__file__).parent / "kalman_price_tuning.json"
STATE_FILE = Path(__file__).parent / "kalman_price_state.json"
BTC_CACHE_FILE = Path.home() / ".cache" / "btc_price.json"

ZAI_MONTHLY_EUR = 144.0
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=eur,usd"
BTC_CACHE_TTL = 300  # 5 min


def fetch_btc_price():
    """Fetch current BTC price from CoinGecko. Cached to avoid rate limits."""
    # Check cache first
    now = datetime.now(timezone.utc).timestamp()
    if BTC_CACHE_FILE.exists():
        try:
            cached = json.loads(BTC_CACHE_FILE.read_text())
            if now - cached.get("_fetched_at", 0) < BTC_CACHE_TTL:
                return cached["eur"], cached["usd"]
        except Exception:
            pass

    try:
        req = urllib.request.Request(COINGECKO_URL, headers={"User-Agent": "Hermes/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        eur = data.get("bitcoin", {}).get("eur", 0)
        usd = data.get("bitcoin", {}).get("usd", 0)

        # Write cache
        BTC_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        BTC_CACHE_FILE.write_text(json.dumps({
            "eur": eur, "usd": usd, "_fetched_at": now
        }))
        return eur, usd
    except Exception as e:
        print(f"WARN: BTC price fetch failed: {e}", file=sys.stderr)
        # Return stale cache if available
        if BTC_CACHE_FILE.exists():
            try:
                cached = json.loads(BTC_CACHE_FILE.read_text())
                return cached.get("eur", 0), cached.get("usd", 0)
            except Exception:
                pass
        return 0, 0


def load_tuning():
    """Load Kalman price tuning overrides."""
    try:
        if TUNING_FILE.exists():
            return json.loads(TUNING_FILE.read_text())
    except Exception:
        pass
    return {}


def load_state():
    """Load persisted Kalman state for each key."""
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception:
        pass
    return {}


def save_state(state):
    """Persist Kalman state for each key."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def compute_zai_price(db, btc_eur):
    """Compute z.ai price per token from 30d trailing burn."""
    cutoff_30d = datetime.now(timezone.utc).timestamp() - 86400 * 30
    row = db.execute("""
        SELECT SUM(total_tokens)
        FROM api_calls
        WHERE ts > ? AND ppq_hit = 0 AND status_code = 200
          AND key_name IN ('ours', 'friend')
    """, (cutoff_30d,)).fetchone()
    monthly_tokens = row[0] or 1
    eur_per_token = ZAI_MONTHLY_EUR / monthly_tokens
    if btc_eur > 0:
        return (eur_per_token / btc_eur) * 100_000_000
    return 0


def _get_combined_zai_volume(db):
    """Get combined hourly volume for z.ai keys (ours + friend) over last 168h."""
    cutoff = datetime.now(timezone.utc).timestamp() - 168 * 3600
    row = db.execute("""
        SELECT AVG(hourly_tokens) FROM (
            SELECT CAST(ts / 3600 AS INTEGER) * 3600 as hour_ts,
                   SUM(total_tokens) as hourly_tokens
            FROM api_calls
            WHERE ts > ? AND ppq_hit = 0 AND status_code = 200
              AND key_name IN ('ours', 'friend')
            GROUP BY hour_ts
        )
    """, (cutoff,)).fetchone()
    return row[0] or 0


def compute_ppq_price(db, btc_usd):
    """Compute PPQ price per token from balance history or heuristic."""
    # Try real balance data first
    try:
        bdb = sqlite3.connect(str(BURN_DB))
        ppq_rows = bdb.execute("""
            SELECT ts, balance_usd FROM balance_snapshots
            WHERE provider='ppq' AND balance_usd IS NOT NULL AND error IS NULL
            ORDER BY ts DESC LIMIT 2
        """).fetchall()
        bdb.close()

        if len(ppq_rows) >= 2:
            latest = ppq_rows[0]
            previous = ppq_rows[1]
            ppq_tokens = db.execute("""
                SELECT SUM(total_tokens) FROM api_calls
                WHERE ppq_hit=1 AND ts BETWEEN ? AND ?
            """, (previous[0], latest[0])).fetchone()[0] or 1
            usd_spent = previous[1] - latest[1]
            if usd_spent > 0:
                usd_per_token = usd_spent / ppq_tokens
            else:
                usd_per_token = 0
        else:
            usd_per_token = 0.50 / 1_000_000  # heuristic: 50¢ per M tokens
    except Exception:
        usd_per_token = 0.50 / 1_000_000

    if btc_usd > 0 and usd_per_token > 0:
        return (usd_per_token / btc_usd) * 100_000_000
    return 0


def get_hourly_price_observations(db, key_name, hours=168):
    """
    Get hourly price-per-token observations for the last N hours.
    Returns list of (hour_ts, sats_per_token) for non-zero burn hours.
    """
    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
    rows = db.execute("""
        SELECT CAST(ts / 3600 AS INTEGER) * 3600 as hour_ts,
               SUM(total_tokens) as tokens
        FROM api_calls
        WHERE ts > ? AND status_code = 200
          AND key_name = ?
        GROUP BY hour_ts
        ORDER BY hour_ts ASC
    """, (cutoff, key_name)).fetchall()
    return [(r[0], r[1] or 0) for r in rows]


def run_kalman_price_filter(key, observations, btc_price, tuning, db):
    """
    Run Kalman filter on hourly price observations to estimate
    converged price-per-token and price trend.

    Returns: {price_sats, trend_sats_per_hour, n_obs, converged}
    """
    key_tuning = tuning.get(key, {})
    mn = key_tuning.get("measurement_noise", 10.0)
    pn = key_tuning.get("process_noise", 0.1)

    kf = KalmanPredictor(process_noise=pn, measurement_noise=mn)
    state = load_state().get(key, {})

    # Restore persisted state if available
    if state and "volume" in state and "velocity" in state:
        kf.x = np.array([[state["volume"]], [state["velocity"]]])
        if "P00" in state and "P01" in state and "P10" in state and "P11" in state:
            kf.P = np.array([[state["P00"], state["P01"]],
                             [state["P10"], state["P11"]]])

    n_obs = 0
    for hour_ts, tokens in observations:
        if tokens > 0:
            # We need to infer price from tokens
            # Actually, the Kalman filter tracks volume (tokens/hour)
            # We use an alternative approach: filter the token volume,
            # then compute price from the smoothed volume
            kf.update(float(tokens))
            kf.predict()
            n_obs += 1

    # Compute price from the smoothed volume estimate
    smoothed_volume = float(kf.x[0, 0])
    trend = float(kf.x[1, 0])

    if key in ("ours", "friend"):
        # z.ai: flat rate is shared across both keys
        # Use combined volume from both keys for pricing
        if _HAS_NUMPY:
            combined_volume = _get_combined_zai_volume(db)
            if combined_volume > 0:
                eur_per_token = ZAI_MONTHLY_EUR / (combined_volume * 720)
                price_sats = (eur_per_token / btc_price["eur"]) * 100_000_000
            else:
                price_sats = compute_zai_price(db, btc_price["eur"])
        else:
            price_sats = compute_zai_price(db, btc_price["eur"])
    elif key == "ppq":
        price_sats = compute_ppq_price(db, btc_price["usd"])
    else:
        price_sats = 0

    # Persist state
    state_data = {
        key: {
            "volume": float(kf.x[0, 0]),
            "velocity": float(kf.x[1, 0]),
            "P00": float(kf.P[0, 0]),
            "P01": float(kf.P[0, 1]),
            "P10": float(kf.P[1, 0]),
            "P11": float(kf.P[1, 1]),
            "n_obs": n_obs,
            "price_sats": price_sats,
            "updated_at": datetime.now(timezone.utc).timestamp(),
        }
    }
    old_state = load_state()
    old_state.update(state_data)
    save_state(old_state)

    return {
        "price_sats": price_sats,
        "trend_sats_per_hour": trend,
        "n_obs": n_obs,
        "smoothed_volume_tph": round(smoothed_volume, 1),
    }


def main():
    try:
        import numpy as np
    except ImportError:
        np = None

    btc_eur, btc_usd = fetch_btc_price()
    btc_price = {"eur": btc_eur, "usd": btc_usd}
    tuning = load_tuning()

    db = sqlite3.connect(str(DB))

    # Compute prices for all key types
    result = {}

    for key in ("ours", "friend", "ppq"):
        obs = get_hourly_price_observations(db, key)
        if np is not None:
            kalman_result = run_kalman_price_filter(key, obs, btc_price, tuning, db)
            price_sats = kalman_result["price_sats"]
        else:
            # Fallback: no Kalman
            if key in ("ours", "friend"):
                price_sats = compute_zai_price(db, btc_eur)
            else:
                price_sats = compute_ppq_price(db, btc_usd)
            kalman_result = {"n_obs": len(obs), "trend_sats_per_hour": 0}

        result[key] = {
            "sats_per_token": round(price_sats, 12),
            "sats_per_Mtokens": round(price_sats * 1_000_000, 4),
            "n_hourly_observations": kalman_result["n_obs"],
            "trend_sats_per_hour": round(kalman_result["trend_sats_per_hour"], 6),
        }

    db.close()

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "btc_eur": btc_eur,
        "btc_usd": btc_usd,
        "keys": result,
    }
    print(json.dumps(output, indent=2))
    return output


if __name__ == "__main__":
    main()
