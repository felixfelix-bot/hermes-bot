#!/usr/bin/env python3
"""treasurer_ledger — sqlite bookkeeping for the treasurer agent.

CLI:
  treasurer_ledger.py init
  treasurer_ledger.py add <type> <amount_sats> <category> <counterparty> [note]
                           type = income|cost|dividend
  treasurer_ledger.py balance            # income - cost (sats, usd)
  treasurer_ledger.py export [out.csv]
  treasurer_ledger.py price              # current BTC/USD (non-KYC fetch)

Also importable: get_btc_usd(), add_row(...), profit(), DB_PATH.
No LLM tokens. usd_approx via a non-KYC price fetch at tx time.
"""
from __future__ import annotations
import csv, json, os, sqlite3, sys, time, urllib.request
from pathlib import Path

DB = Path.home() / ".hermes" / "bot" / "ledger.sqlite"


def get_btc_usd() -> float | None:
    """Non-KYC BTC/USD price. Tries free sources; returns first SANE value (>1000)."""
    sources = [
        ("https://api.coingecko.com/simple/price?ids=bitcoin&vs_currencies=usd", lambda d: d["bitcoin"]["usd"]),
        ("https://api.yadio.io/rate/BTC/USD", lambda d: float(d.get("USD") or d.get("rate") or 0)),
        ("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", lambda d: float(d["price"])),
    ]
    for url, pick in sources:
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                val = float(pick(json.loads(r.read().decode())))
            if val and val > 1000:  # sanity: BTC is > $1000
                return val
        except Exception:
            continue
    return None


def init_db() -> None:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
        type TEXT NOT NULL, amount_sats INTEGER NOT NULL, usd_approx REAL,
        category TEXT, counterparty TEXT, ref TEXT, note TEXT)""")
    con.commit(); con.close()


def add_row(typ: str, amount_sats: int, category="", counterparty="", ref="", note="") -> int:
    if typ not in ("income", "cost", "dividend"):
        raise SystemExit("type must be income|cost|dividend")
    init_db()
    usd = get_btc_usd()
    usd_approx = round(amount_sats / 1e8 * usd, 2) if usd else None
    con = sqlite3.connect(DB)
    cur = con.execute(
        "INSERT INTO ledger(ts,type,amount_sats,usd_approx,category,counterparty,ref,note) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (time.time(), typ, int(amount_sats), usd_approx, category, counterparty, ref, note))
    con.commit(); rid = cur.lastrowid; con.close()
    return rid


def profit() -> dict:
    init_db()
    con = sqlite3.connect(DB)
    inc = con.execute("SELECT COALESCE(SUM(amount_sats),0) FROM ledger WHERE type='income'").fetchone()[0]
    cost = con.execute("SELECT COALESCE(SUM(amount_sats),0) FROM ledger WHERE type='cost'").fetchone()[0]
    div = con.execute("SELECT COALESCE(SUM(amount_sats),0) FROM ledger WHERE type='dividend'").fetchone()[0]
    con.close()
    usd = get_btc_usd()
    sats = inc - cost
    return {"profit_sats": sats, "income_sats": inc, "cost_sats": cost, "dividends_sats": div,
            "profit_usd_approx": round(sats / 1e8 * usd, 2) if usd else None,
            "retained_sats": sats - div}


def export_csv(out: str) -> str:
    init_db()
    con = sqlite3.connect(DB)
    rows = con.execute("SELECT ts,type,amount_sats,usd_approx,category,counterparty,ref,note FROM ledger ORDER BY ts").fetchall()
    con.close()
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts_iso", "type", "amount_sats", "usd_approx", "category", "counterparty", "ref", "note"])
        for ts, t, a, u, c, cp, r, n in rows:
            w.writerow([time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)), t, a, u, c, cp, r, n])
    return out


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__); return 1
    cmd = args[0]
    if cmd == "init":
        init_db(); print(f"ledger ready: {DB}"); return 0
    if cmd == "price":
        print(get_btc_usd() or "unavailable"); return 0
    if cmd == "add":
        typ, amt, cat = args[1], int(args[2]), args[3]
        cp = args[4] if len(args) > 4 else ""
        note = args[5] if len(args) > 5 else ""
        rid = add_row(typ, amt, cat, cp, note=note)
        print(f"recorded id={rid} {typ} {amt} sats [{cat}] {cp}"); return 0
    if cmd == "balance":
        print(json.dumps(profit(), indent=2)); return 0
    if cmd == "export":
        out = args[1] if len(args) > 1 else str(Path.home() / ".hermes" / "bot" / "ledger.csv")
        print(f"wrote {export_csv(out)}"); return 0
    print(__doc__); return 1


if __name__ == "__main__":
    sys.exit(main())
