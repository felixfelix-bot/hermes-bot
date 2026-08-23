#!/usr/bin/env python3
"""Daily token-efficiency report — surfaces the key waste metrics.

Prints a one-block summary of:
  1. Empty-response signature (HTTP 200, >10K prompt, <50 completion) —
     the nudge-loop / empty-recovery waste. Should drop sharply after the
     max_empty_recovery_total cap.
  2. Cron session burn (tokens/day, top 5 jobs).
  3. Telnyx estimate vs real balance delta (should converge after the
     cached-aware pricing fix).

Run hourly via the burn_attribution cron (appended to its output) or
standalone: python3 cron_efficiency_report.py
"""
import sqlite3
import time
from pathlib import Path

ZAI_DB = Path.home() / ".hermes/bot/zai_usage.db"
ATTR_DB = Path.home() / ".hermes/bot/burn_attribution.db"


def _zai():
    conn = sqlite3.connect(f"file:{ZAI_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _attr():
    if not ATTR_DB.exists():
        return None
    conn = sqlite3.connect(f"file:{ATTR_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def empty_signature(conn, hours=24):
    """Empty-response waste: HTTP 200, big prompt, tiny completion."""
    row = conn.execute(
        "SELECT COUNT(*), SUM(prompt_tokens)/1e6 FROM api_calls "
        "WHERE status_code=200 AND prompt_tokens>10000 AND completion_tokens<50 "
        "AND ts >= ?", (time.time() - hours * 3600,)
    ).fetchone()
    calls, prompt_m = int(row[0] or 0), float(row[1] or 0)
    return calls, prompt_m


def cron_burn(attr_conn, hours=24):
    """Top cron jobs by tokens in the latest attribution window."""
    if attr_conn is None:
        return []
    row = attr_conn.execute(
        "SELECT MAX(window_since) FROM attribution").fetchone()
    max_window = row[0] if row else None
    if max_window is None:
        return []
    rows = attr_conn.execute(
        "SELECT profile, SUM(tokens_share)/1e6 AS tok, SUM(cost_share) AS cost "
        "FROM attribution WHERE window_since=? AND kind='session' "
        "AND session_id LIKE 'cron_%' "
        "GROUP BY profile ORDER BY tok DESC LIMIT 5",
        (max_window,)
    ).fetchall()
    return [(r["profile"], float(r["tok"] or 0), float(r["cost"] or 0)) for r in rows]


def telnyx_estimate_vs_balance(conn):
    """Today's recorded Telnyx spend (should track the real balance API)."""
    row = conn.execute(
        "SELECT ROUND(SUM(cost_usd),2), SUM(prompt_tokens), SUM(cache_hit) "
        "FROM api_calls WHERE tier='telnyx' "
        "AND date(ts,'unixepoch','localtime')=date('now','localtime')"
    ).fetchone()
    spend = float(row[0] or 0)
    prompt = int(row[1] or 0)
    cached = int(row[2] or 0)
    hit = (cached / prompt * 100) if prompt > 0 else 0
    return spend, prompt, hit


def main():
    zai = _zai()
    attr = _attr()
    now = time.strftime("%Y-%m-%d %H:%M %Z", time.localtime())
    calls, prompt_m = empty_signature(zai)
    cron = cron_burn(attr)
    t_spend, t_prompt, t_hit = telnyx_estimate_vs_balance(zai)

    print(f"\n=== Token Efficiency Report ({now}) ===")
    print(f"Empty-response waste (24h): {calls} calls, {prompt_m:.1f}M prompt tokens")
    print(f"  target: <5M/day after max_empty_recovery_total cap")
    if cron:
        print(f"Cron burn (latest attribution window, top 5):")
        for prof, tok, cost in cron:
            print(f"  {prof:40s} {tok:8.1f}M  ${cost:7.2f}")
    else:
        print("Cron burn: (attribution window not available)")
    print(f"Telnyx today: ${t_spend:.2f} recorded, {t_prompt/1e6:.1f}M prompt, {t_hit:.0f}% cache hit")
    print(f"  (calibration should converge to ~1.0 with cached-aware pricing)")
    zai.close()
    if attr:
        attr.close()


if __name__ == "__main__":
    main()