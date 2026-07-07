#!/usr/bin/env python3
"""cashu_balance_watch — watchdog: nudge when the openrouter Cashu wallet
balance falls below N months of JMP cost.

Watchdog pattern (hermes cron --no-agent): EMPTY stdout = silent; emits an alert
only when low. Reads `cashu balance` (wallet openrouter). Writes state to
~/.hermes/bot/cashu_state.json. Deliver: Signal primary (once live), Nostr-DM
fallback; until then logs locally. No LLM tokens.

Env: JMP_MONTHLY_USD (default 2.99), BALANCE_NUDGE_MONTHS (default 3),
     CASHU_BIN (default ~/.cashu-venv/bin/cashu).
"""
from __future__ import annotations
import json, os, subprocess, sys, time
from pathlib import Path

STATE = Path.home() / ".hermes" / "bot" / "cashu_state.json"
CASHU = os.environ.get("CASHU_BIN", str(Path.home() / ".cashu-venv/bin/cashu"))
JMP_USD = float(os.environ.get("JMP_MONTHLY_USD", "2.99"))
MONTHS = int(os.environ.get("BALANCE_NUDGE_MONTHS", "3"))


def btc_usd():
    """Non-KYC BTC/USD; first sane value (>1000). Self-contained (no import)."""
    import urllib.request
    sources = [
        ("https://api.coingecko.com/simple/price?ids=bitcoin&vs_currencies=usd", lambda d: d["bitcoin"]["usd"]),
        ("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", lambda d: float(d["price"])),
    ]
    for url, pick in sources:
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                val = float(pick(json.loads(r.read().decode())))
            if val and val > 1000:
                return val
        except Exception:
            continue
    return None


def balance_sats():
    """Query cashu balance (openrouter wallet). Returns sats or None."""
    try:
        r = subprocess.run([CASHU, "balance"], capture_output=True, text=True, timeout=30)
        for line in r.stdout.splitlines():
            if line.strip().lower().startswith("balance"):
                # 'Balance: 1234 sat' or 'Balance: 0 sat'
                t = line.split(":", 1)[1].replace(",", "").strip()
                digits = "".join(ch for ch in t if ch.isdigit())
                return int(digits) if digits else 0
        return None
    except Exception:
        return None


def main() -> int:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    sats = balance_sats()
    price = btc_usd()
    if sats is None or price is None:
        STATE.write_text(json.dumps({"ts": int(time.time()), "ok": False, "error": "balance/price unavailable"}, indent=2))
        return 0  # transient: silent
    bal_usd = sats / 1e8 * price
    threshold_usd = JMP_USD * MONTHS
    low = bal_usd < threshold_usd
    STATE.write_text(json.dumps({"ts": int(time.time()), "ok": True, "balance_sats": sats,
                                 "balance_usd_approx": round(bal_usd, 2), "threshold_usd": round(threshold_usd, 2),
                                 "low": low}, indent=2))
    if low:
        print(f"💸 TREASURER: wallet low — {sats} sats (~${bal_usd:.2f}), "
              f"below {MONTHS}× JMP (~${threshold_usd:.2f}). Top up the openrouter wallet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
