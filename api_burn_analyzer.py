#!/usr/bin/env python3
"""api_burn_analyzer.py — Rolling stats + anomaly detection for API balances.

Runs every 15 minutes via systemd timer. Reads recent balance snapshots from
~/.hermes/bot/api_burn.db, computes rolling 6-hour statistics, and detects
three anomaly classes:

  1. LOW_BALANCE  — balance below $5.00
  2. BURN_BURST   — spend in this interval > 3σ above the rolling mean
  3. EXPENSIVE_REQ — single-interval spend exceeds $0.10

Anomalies are inserted into the unified anomaly_events table in
~/.hermes/bot/zai_usage.db, which the anomaly-notify.sh cron picks up for
delivery (no separate notification path needed).

CLI:
  python3 api_burn_analyzer.py             # analyze + insert anomalies
  python3 api_burn_analyzer.py --dry-run   # print analysis, don't insert
  python3 api_burn_analyzer.py --report    # show rolling stats summary
"""

from __future__ import annotations
import json
import math
import os
import sqlite3
import sys
import time
from pathlib import Path

BURN_DB = os.path.expanduser(
    os.environ.get("API_BURN_DB_PATH", "~/.hermes/bot/api_burn.db")
)
UNIFIED_DB = os.path.expanduser(
    os.environ.get("ZAI_USAGE_DB", "~/.hermes/bot/zai_usage.db")
)

ROLLING_WINDOW_HOURS = 6
BURST_SIGMA = 3.0
EXPENSIVE_REQ_THRESHOLD = 0.10

# Per-provider low-balance thresholds (USD).
# Prepaid providers (openrouter, routstr) use a flat $5 threshold.
# PPQ uses a pre-funded balance — same threshold applies.
LOW_BALANCE_THRESHOLDS = {
    "ppq": 5.0,
    "openrouter": 5.0,
    "routstr": 5.0,
}
# Default if provider not listed
LOW_BALANCE_DEFAULT = 5.0

PROVIDERS = ["ppq", "openrouter", "routstr"]


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _get_burn_conn():
    conn = sqlite3.connect(BURN_DB)
    conn.row_factory = sqlite3.Row
    return conn


def _get_unified_conn():
    conn = sqlite3.connect(UNIFIED_DB)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        conn.close()
        conn = sqlite3.connect(UNIFIED_DB)
        conn.row_factory = sqlite3.Row
    return conn


# ── Statistics ─────────────────────────────────────────────────────────────────

def mean(values):
    if not values:
        return 0.0
    return sum(values) / len(values)


def stdev(values):
    if len(values) < 2:
        return 0.0
    m = mean(values)
    variance = sum((v - m) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)


def z_score(value, m, s):
    if s == 0:
        return 0.0
    return (value - m) / s


# ── Data queries ───────────────────────────────────────────────────────────────

def get_recent_snapshots(provider, hours=ROLLING_WINDOW_HOURS):
    """Return snapshots for a provider within the rolling window, newest first."""
    cutoff = time.time() - hours * 3600
    conn = _get_burn_conn()
    rows = conn.execute(
        """SELECT * FROM balance_snapshots
           WHERE provider = ? AND ts >= ?
             AND balance_usd IS NOT NULL
           ORDER BY ts ASC""",
        (provider, cutoff),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def compute_spend_intervals(snapshots):
    """Compute per-interval spend from consecutive balance readings.

    Each interval spend = previous_balance - current_balance (if positive).
    Negative deltas (refunds/top-ups) are clamped to 0 for burst detection
    but logged separately.
    """
    intervals = []
    for i in range(1, len(snapshots)):
        prev = snapshots[i - 1]
        curr = snapshots[i]
        delta = prev["balance_usd"] - curr["balance_usd"]
        dt = curr["ts"] - prev["ts"]
        if dt <= 0 or dt > 7200:
            continue
        intervals.append({
            "ts": curr["ts"],
            "spend": max(0, delta),
            "raw_delta": delta,
            "interval_seconds": dt,
        })
    return intervals


def analyze_provider(provider, now=None):
    """Analyze one provider and return anomaly list + stats summary."""
    if now is None:
        now = time.time()

    snapshots = get_recent_snapshots(provider)
    anomalies = []
    stats = {
        "provider": provider,
        "data_points": len(snapshots),
        "current_balance": None,
        "mean_spend_per_interval": 0.0,
        "stdev_spend": 0.0,
        "latest_spend": 0.0,
    }

    if not snapshots:
        return anomalies, stats

    latest = snapshots[-1]
    stats["current_balance"] = latest["balance_usd"]

    intervals = compute_spend_intervals(snapshots)
    spends = [iv["spend"] for iv in intervals]

    if spends:
        stats["mean_spend_per_interval"] = round(mean(spends), 4)
        stats["stdev_spend"] = round(stdev(spends), 4)
        stats["latest_spend"] = round(spends[-1], 4)

    # ── Condition 1: Low balance ──
    bal = latest["balance_usd"]
    threshold = LOW_BALANCE_THRESHOLDS.get(provider, LOW_BALANCE_DEFAULT)
    if bal is not None and bal < threshold:
        anomalies.append({
            "ts": now,
            "severity": "critical",
            "category": "api_low_balance",
            "title": f"{provider} balance low: ${bal:.2f}",
            "detail": (
                f"{provider} balance at ${bal:.2f}, below "
                f"${threshold:.2f} threshold. "
                f"Top up to avoid service interruption."
            ),
        })

    # ── Condition 2: Burn burst (> 3σ above mean) ──
    if len(spends) >= 5:
        m = mean(spends[:-1])
        s = stdev(spends[:-1])
        latest_spend = spends[-1]
        z = z_score(latest_spend, m, s)
        if z > BURST_SIGMA:
            anomalies.append({
                "ts": now,
                "severity": "warning",
                "category": "api_burn_burst",
                "title": f"{provider} burn burst: ${latest_spend:.4f} (z={z:.1f}σ)",
                "detail": (
                    f"{provider} spend this interval (${latest_spend:.4f}) is "
                    f"{z:.1f}σ above the rolling mean (${m:.4f}, σ=${s:.4f}). "
                    f"Possible runaway agent or expensive request."
                ),
            })

    # ── Condition 3: Expensive single request ──
    if spends:
        latest_spend = spends[-1]
        if latest_spend > EXPENSIVE_REQ_THRESHOLD:
            anomalies.append({
                "ts": now,
                "severity": "warning",
                "category": "api_expensive_request",
                "title": f"{provider} expensive interval: ${latest_spend:.2f}",
                "detail": (
                    f"{provider} spend in last interval "
                    f"(${latest_spend:.2f}) exceeds "
                    f"${EXPENSIVE_REQ_THRESHOLD:.2f} threshold."
                ),
            })

    return anomalies, stats


def insert_anomalies(anomalies):
    """Insert anomalies into the unified anomaly_events table."""
    if not anomalies:
        return 0

    Path(UNIFIED_DB).parent.mkdir(parents=True, exist_ok=True)
    conn = _get_unified_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS anomaly_events (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            ts        REAL NOT NULL,
            severity  TEXT NOT NULL,
            category  TEXT NOT NULL,
            title     TEXT,
            detail    TEXT,
            alerted   INTEGER DEFAULT 0,
            resolved  INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_anomaly_unresolved
            ON anomaly_events(resolved, alerted);
    """)
    conn.executemany(
        """INSERT INTO anomaly_events
           (ts, severity, category, title, detail)
           VALUES (:ts, :severity, :category, :title, :detail)""",
        anomalies,
    )
    conn.commit()
    conn.close()
    return len(anomalies)


# ── Main ───────────────────────────────────────────────────────────────────────

def run(dry_run=False, report_only=False):
    all_anomalies = []
    all_stats = []

    for provider in PROVIDERS:
        anomalies, stats = analyze_provider(provider)
        all_anomalies.extend(anomalies)
        all_stats.append(stats)

    if report_only:
        print("API Burn Monitor — Rolling Stats (6h window)")
        print("=" * 60)
        for s in all_stats:
            bal = f"${s['current_balance']:.2f}" if s["current_balance"] is not None else "N/A"
            print(
                f"  {s['provider']:14s} bal={bal:10s}  "
                f"mean/interval=${s['mean_spend_per_interval']:.4f}  "
                f"σ=${s['stdev_spend']:.4f}  "
                f"last=${s['latest_spend']:.4f}  "
                f"({s['data_points']} pts)"
            )
        if all_anomalies:
            print(f"\n  ⚠ {len(all_anomalies)} anomaly(ies) detected:")
            for a in all_anomalies:
                print(f"    [{a['severity'].upper()}] {a['title']}")
        else:
            print("\n  ✓ No anomalies.")
        return

    if dry_run:
        print("DRY RUN — anomalies that would be inserted:")
        if not all_anomalies:
            print("  (none)")
        for a in all_anomalies:
            print(f"  [{a['severity'].upper()}] {a['category']}: {a['title']}")
        return

    inserted = insert_anomalies(all_anomalies)
    ts_str = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    print(f"Analyzed {len(PROVIDERS)} providers at {ts_str}. {inserted} anomaly(ies) inserted.")


def main():
    if "--report" in sys.argv:
        run(report_only=True)
    elif "--dry-run" in sys.argv:
        run(dry_run=True)
    else:
        run()


if __name__ == "__main__":
    main()
