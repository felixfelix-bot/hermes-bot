#!/usr/bin/env python3
"""routstr-revenue-digest.py — Daily revenue report for hermes-admin-setup.

Queries both routstr nodes on testserver2 via SSH for:
  - Cashu transactions (buyer payments)
  - Accumulated fees
  - Lifetime stats
Outputs a compact summary suitable for Signal digest.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone

ROUTSTR_SSH = "root@23.182.128.51"
ROUTSTR_PUBLIC_DB = "/var/lib/docker/volumes/routstr-public_routstr_public_data/_data/keys.db"
ROUTSTR_PROXY_DB = "/var/lib/docker/volumes/routstr_routstr_data/_data/keys.db"

BTC_PRICE_USD = 97500.0
SAT_PER_USD = 1e8 / BTC_PRICE_USD


def _ssh_query(db_path, sql):
    remote_cmd = f'sqlite3 {db_path} "{sql}" 2>/dev/null'
    cmd = [
        "ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new",
        ROUTSTR_SSH, remote_cmd
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def _query_node(name, db_path):
    now = int(time.time())
    day_ago = now - 86400
    week_ago = now - 7 * 86400

    total_in = _ssh_query(db_path, "SELECT COALESCE(SUM(amount),0) FROM cashu_transactions WHERE type='in'")
    total_out = _ssh_query(db_path, "SELECT COALESCE(SUM(amount),0) FROM cashu_transactions WHERE type='out'")
    tx_count = _ssh_query(db_path, "SELECT COUNT(*) FROM cashu_transactions WHERE type='in'")
    day_in = _ssh_query(db_path, f"SELECT COALESCE(SUM(amount),0) FROM cashu_transactions WHERE type='in' AND created_at > {day_ago}")
    day_count = _ssh_query(db_path, f"SELECT COUNT(*) FROM cashu_transactions WHERE type='in' AND created_at > {day_ago}")
    week_in = _ssh_query(db_path, f"SELECT COALESCE(SUM(amount),0) FROM cashu_transactions WHERE type='in' AND created_at > {week_ago}")
    fees = _ssh_query(db_path, "SELECT accumulated_msats, total_paid_msats FROM routstr_fees")

    wallet = int(total_in) - int(total_out) if total_in and total_out else 0
    fee_msats = 0
    paid_msats = 0
    if fees and "|" in fees:
        parts = fees.split("|")
        fee_msats = int(parts[0]) if parts[0] else 0
        paid_msats = int(parts[1]) if len(parts) > 1 and parts[1] else 0

    return {
        "name": name,
        "lifetime_sats_in": int(total_in) if total_in else 0,
        "lifetime_sats_out": int(total_out) if total_out else 0,
        "wallet_balance_sats": wallet,
        "tx_count": int(tx_count) if tx_count else 0,
        "day_sats": int(day_in) if day_in else 0,
        "day_txs": int(day_count) if day_count else 0,
        "week_sats": int(week_in) if week_in else 0,
        "fees_msats": fee_msats,
        "paid_out_msats": paid_msats,
    }


def main():
    nodes = [
        _query_node("routstr-public", ROUTSTR_PUBLIC_DB),
        _query_node("routstr-proxy", ROUTSTR_PROXY_DB),
    ]

    lines = [f"📈 ROUTSTR REVENUE DIGEST — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", ""]

    total_day = 0
    total_week = 0
    total_wallet = 0
    total_fees = 0

    for n in nodes:
        day_usd = n["day_sats"] / SAT_PER_USD
        week_usd = n["week_sats"] / SAT_PER_USD
        wallet_usd = n["wallet_balance_sats"] / SAT_PER_USD
        fees_sats = n["fees_msats"] / 1000
        paid_sats = n["paid_out_msats"] / 1000

        lines.append(f"{n['name']}:")
        lines.append(f"  24h: {n['day_sats']} sats ({n['day_txs']} txs) ≈ ${day_usd:.2f}")
        lines.append(f"  7d:  {n['week_sats']} sats ≈ ${week_usd:.2f}")
        lines.append(f"  Wallet: {n['wallet_balance_sats']} sats ≈ ${wallet_usd:.2f}")
        lines.append(f"  Fees: {fees_sats:.1f} sats (paid out: {paid_sats:.1f})")
        lines.append(f"  Lifetime: {n['lifetime_sats_in']} sats in / {n['lifetime_sats_out']} sats out / {n['tx_count']} txs")
        lines.append("")

        total_day += n["day_sats"]
        total_week += n["week_sats"]
        total_wallet += n["wallet_balance_sats"]
        total_fees += fees_sats

    lines.append(f"TOTAL: 24h={total_day} sats (${total_day/SAT_PER_USD:.2f}) | "
                 f"7d={total_week} sats (${total_week/SAT_PER_USD:.2f}) | "
                 f"wallet={total_wallet} sats (${total_wallet/SAT_PER_USD:.2f}) | "
                 f"fees={total_fees:.1f} sats")

    report = "\n".join(lines)
    print(report)


if __name__ == "__main__":
    main()
