#!/usr/bin/env python3
"""api_burn_collector.py — Poll pay-per-request API provider balances.

Runs every 5 minutes via systemd timer. Polls balance/credit endpoints for:
  - PPQ (api.ppq.ai)
  - OpenRouter (openrouter.ai)
  - Routstr (self-hosted Cashu inference node)

Stores snapshots in SQLite at ~/.hermes/bot/api_burn.db.

Each provider has a fetcher function. If a provider isn't configured (no API key
in env) or its endpoint returns an error, the collector logs the failure and
continues — one broken provider never blocks collection of the others.

CLI:
  python3 api_burn_collector.py            # collect all providers
  python3 api_burn_collector.py --once      # single pass (default)
  python3 api_burn_collector.py --dry-run   # print what would be collected, don't write

Env vars (all optional — missing providers are silently skipped):
  PPQ_API_KEY              PPQ bearer token
  OPENROUTER_API_KEY       OpenRouter bearer token
  ROUTSTR_URL              Routstr base URL (e.g. http://localhost:3338)
  ROUTSTR_ADMIN_TOKEN      Routstr admin API bearer token
  ROUTSTR_MINT_URL         Cashu mint URL for balance query
  API_BURN_DB_PATH         override DB path (default ~/.hermes/bot/api_burn.db)
"""

from __future__ import annotations
import json
import os
import sqlite3
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

DB_PATH = os.path.expanduser(
    os.environ.get("API_BURN_DB_PATH", "~/.hermes/bot/api_burn.db")
)

REQUEST_TIMEOUT = 15

PROVIDERS = ["ppq", "openrouter", "routstr"]


# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS balance_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    provider    TEXT    NOT NULL,
    balance_usd REAL,
    total_credits REAL,
    total_usage REAL,
    currency    TEXT,
    raw         TEXT,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_snap_ts       ON balance_snapshots(ts);
CREATE INDEX IF NOT EXISTS idx_snap_provider ON balance_snapshots(provider, ts);
"""


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        conn.close()
        conn = sqlite3.connect(DB_PATH)
    return conn


def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = _get_conn()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


# ── HTTP helper ────────────────────────────────────────────────────────────────

def _http_get_json(url, headers=None, timeout=REQUEST_TIMEOUT):
    req = urllib.request.Request(url)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


# ── Provider fetchers ──────────────────────────────────────────────────────────

def fetch_ppq():
    """Poll PPQ balance via the real API.

    PPQ exposes POST /credits/balance — accepts API key via Bearer auth.
    Also fetches GET /queries/history for real per-query spend tracking and
    GET /keys for per-key usage limits (requires credit_id).

    Falls back to local ppq_budget.py only if the API is unreachable.
    """
    key = os.environ.get("PPQ_API_KEY", "").strip()
    if not key:
        return {"provider": "ppq", "skipped": "no PPQ_API_KEY"}

    base = "https://api.ppq.ai"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    # ── 1. POST /credits/balance — real remaining balance ──
    try:
        req = urllib.request.Request(
            f"{base}/credits/balance",
            data=b'{}',
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
            balance = float(data.get("balance", 0))

            # ── 2. GET /queries/history — recent spend breakdown ──
            spend_24h = 0.0
            query_count_24h = 0
            try:
                hreq = urllib.request.Request(
                    f"{base}/queries/history?page=1&page_count=100",
                    headers={"Authorization": f"Bearer {key}"},
                )
                with urllib.request.urlopen(hreq, timeout=REQUEST_TIMEOUT) as hresp:
                    hist = json.loads(hresp.read().decode())
                    now = time.time()
                    for q in hist.get("data", []):
                        # Parse ISO timestamp
                        ts_str = q.get("timestamp", "")
                        try:
                            from datetime import datetime, timezone
                            qts = datetime.fromisoformat(
                                ts_str.replace("Z", "+00:00")
                            ).timestamp()
                            if now - qts < 86400:  # last 24h
                                spend_24h += float(q.get("price_in_usd", 0))
                                query_count_24h += 1
                        except Exception:
                            pass
                    total_queries = hist.get("pagination", {}).get("total", 0)
            except Exception:
                total_queries = 0

            return {
                "provider": "ppq",
                "balance_usd": round(balance, 6),
                "total_credits": None,  # PPQ uses pre-funded, not credit grants
                "total_usage": round(spend_24h, 6),
                "spend_24h_usd": round(spend_24h, 6),
                "queries_24h": query_count_24h,
                "total_queries_all_time": total_queries,
                "raw": json.dumps(data)[:500],
            }
    except Exception as e:
        pass

    return _fetch_ppq_local_budget()


def fetch_ppq_keys():
    """Fetch per-key usage stats from PPQ.

    GET /keys requires x-credit-id header. Returns spending limits,
    current period usage, and total all-time usage per API key.
    """
    key = os.environ.get("PPQ_API_KEY", "").strip()
    credit_id = os.environ.get("PPQ_CREDIT_ID", "").strip()
    if not key or not credit_id:
        return {"skipped": "need both PPQ_API_KEY and PPQ_CREDIT_ID"}

    base = "https://api.ppq.ai"
    try:
        req = urllib.request.Request(
            f"{base}/keys",
            headers={"x-credit-id": credit_id},
        )
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
            return data
    except Exception as e:
        return {"error": str(e)}


def _fetch_ppq_local_budget():
    """Fall back to local ppq_budget.py tracking for PPQ balance.

    ppq_budget tracks daily_spend against daily_limit ($1.00). The 'balance'
    we report is remaining = daily_limit - daily_spend. This is a daily
    allowance, not a prepaid credit balance, but it's the closest analogue
    available since PPQ has no remote balance API.
    """
    try:
        sys.path.insert(0, str(Path.home() / ".hermes" / "bot"))
        import ppq_budget
        status = ppq_budget.get_budget_status()
        return {
            "provider": "ppq",
            "balance_usd": round(status.get("remaining", 0.0), 6),
            "total_credits": status.get("daily_limit", 1.0),
            "total_usage": status.get("daily_spend", 0.0),
            "raw": json.dumps({"source": "local_budget", **status})[:500],
        }
    except Exception as e:
        return {"provider": "ppq", "error": f"local budget fallback failed: {e}"}


def fetch_openrouter():
    """Poll OpenRouter credits endpoint.

    GET /api/v1/credits → {data: {total_credits: float, total_usage: float}}
    """
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        return {"provider": "openrouter", "skipped": "no OPENROUTER_API_KEY"}

    try:
        data = _http_get_json(
            "https://openrouter.ai/api/v1/credits",
            headers={"Authorization": f"Bearer {key}"},
        )
        d = data.get("data", data)
        total = float(d.get("total_credits", 0))
        used = float(d.get("total_usage", 0))
        return {
            "provider": "openrouter",
            "balance_usd": round(total - used, 4),
            "total_credits": total,
            "total_usage": used,
            "raw": json.dumps(data)[:500],
        }
    except Exception as e:
        return {"provider": "openrouter", "error": str(e)[:200]}


def fetch_routstr():
    """Poll Routstr (self-hosted Cashu inference node) balance.

    Routstr nodes expose an admin API for balance. We query:
      1. /admin/api/balance (if implemented)
      2. /admin/api/settings (has node stats)

    The Cashu balance is in satoshis; we convert to USD at a configurable rate.
    """
    base = os.environ.get("ROUTSTR_URL", "").strip().rstrip("/")
    if not base:
        return {"provider": "routstr", "skipped": "no ROUTSTR_URL"}

    token = os.environ.get("ROUTSTR_ADMIN_TOKEN", "").strip()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    for path in ["/admin/api/balance", "/admin/api/stats"]:
        try:
            data = _http_get_json(f"{base}{path}", headers=headers, timeout=10)
            sat_balance = (
                data.get("balance_sats")
                or data.get("total_sats")
                or data.get("balance")
            )
            if sat_balance is not None:
                rate = float(os.environ.get("BTC_USD_RATE", "100000"))
                usd = float(sat_balance) / 1e8 * rate
                return {
                    "provider": "routstr",
                    "balance_usd": round(usd, 4),
                    "raw": json.dumps(data)[:500],
                }
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            return {"provider": "routstr", "error": f"HTTP {e.code}: {e.reason}"}
        except Exception as e:
            return {"provider": "routstr", "error": str(e)[:200]}

    return {"provider": "routstr", "error": "no balance endpoint found"}


FETCHERS = {
    "ppq": fetch_ppq,
    "openrouter": fetch_openrouter,
    "routstr": fetch_routstr,
}


# ── Collection ─────────────────────────────────────────────────────────────────

def collect_all(dry_run=False):
    """Poll all providers and persist results."""
    init_db()
    now = time.time()
    results = []

    for provider in PROVIDERS:
        fetcher = FETCHERS[provider]
        try:
            result = fetcher()
        except Exception as e:
            result = {"provider": provider, "error": f"fetcher crashed: {e}"}

        result["ts"] = now
        results.append(result)

        if dry_run:
            status = "OK"
            if result.get("error"):
                status = f"ERROR: {result['error']}"
            elif result.get("skipped"):
                status = f"SKIPPED: {result['skipped']}"
            elif result.get("balance_usd") is not None:
                status = f"balance=${result['balance_usd']:.2f}"
            print(f"  {provider:14s} {status}")
            continue

        conn = _get_conn()
        conn.execute(
            """INSERT INTO balance_snapshots
               (ts, provider, balance_usd, total_credits, total_usage, raw, error)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                now,
                result.get("provider"),
                result.get("balance_usd"),
                result.get("total_credits"),
                result.get("total_usage"),
                result.get("raw"),
                result.get("error") or result.get("skipped"),
            ),
        )
        conn.commit()
        conn.close()

    return results


def main():
    dry_run = "--dry-run" in sys.argv
    results = collect_all(dry_run=dry_run)

    if dry_run:
        print("\nDry run — no data written.")
        return

    ok = sum(1 for r in results if r.get("balance_usd") is not None)
    errs = sum(1 for r in results if r.get("error"))
    skipped = sum(1 for r in results if r.get("skipped"))
    print(
        f"Collected {ok} balances, {skipped} skipped, {errs} errors "
        f"at {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}"
    )


if __name__ == "__main__":
    main()
