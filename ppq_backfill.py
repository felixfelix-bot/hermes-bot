#!/usr/bin/env python3
"""ppq_backfill.py — Populate ppq_queries history (R6).

Two sources, best of both:

  1. PPQ /queries/history API (authoritative costs + token counts, but only
     a limited window is reachable via pagination) → --from-api
  2. Local proxy logs in zai_usage.db api_calls rows with ppq_hit=1
     (authoritative timestamps + models, costs unknown → NULL) → --from-proxy

Both write through ppq_common.record_ppq_queries (UNIQUE dedup index), so
running both backfills is safe and idempotent: proxy rows that match an
existing API row (same ts±2s + model + total_tokens) are skipped.

Usage:
    python3 ppq_backfill.py --from-api [--days 14] [--max-pages 40]
    python3 ppq_backfill.py --from-proxy [--since 48h]
    python3 ppq_backfill.py --from-api --from-proxy   (both)
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ppq_common  # noqa: E402

BURN_DB = os.environ.get(
    "API_BURN_DB_PATH", str(Path.home() / ".hermes" / "bot" / "api_burn.db")
)
ZAI_DB = os.environ.get(
    "ZAI_USAGE_DB", str(Path.home() / ".hermes" / "bot" / "zai_usage.db")
)
HISTORY_URL = "https://api.ppq.ai/queries/history"
PAGE_COUNT = 100
API_TIMEOUT = 15

# a proxy row is "covered" by an API row when within this many seconds
COVER_WINDOW_S = 2.0


def load_ppq_key() -> str:
    key = os.environ.get("PPQ_API_KEY", "").strip()
    if len(key) > 20:
        return key
    env_file = Path.home() / ".hermes" / "profiles" / "manager" / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("PPQ_API_KEY="):
                k = line.split("=", 1)[1].strip().strip('"').strip("'")
                if len(k) > 20:
                    return k
    raise RuntimeError("No valid PPQ_API_KEY found (env or manager .env)")


# ── API history ────────────────────────────────────────────────────────────────

def default_fetch_page(page: int) -> dict | None:
    url = f"{HISTORY_URL}?page={page}&page_count={PAGE_COUNT}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {load_ppq_key()}")
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
            import json

            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"  page {page} failed: {e}", file=sys.stderr)
        return None


def fetch_history_pages(
    fetch_page=default_fetch_page,
    max_pages: int = 0,
    since: float | None = None,
) -> list[dict]:
    """Walk /queries/history pages collecting raw query items.

    Stops on: non-success status, empty page, max_pages (0 = unlimited),
    or when a page is <10% newer than `since` (reached old data, same
    heuristic as pull_ppq_full_history.py).
    """
    out: list[dict] = []
    page = 1
    while True:
        if max_pages and page > max_pages:
            break
        data = fetch_page(page)
        if not data or data.get("status") != "success":
            break
        queries = data.get("data") or []
        if not queries:
            break
        if since is not None:
            recent = []
            for q in queries:
                ts_raw = q.get("timestamp")
                try:
                    qts = datetime.fromisoformat(
                        str(ts_raw).replace("Z", "+00:00")
                    ).timestamp()
                except Exception:
                    continue
                if qts >= since:
                    recent.append(q)
            out.extend(recent)
            if len(recent) < len(queries) * 0.1:
                break
        else:
            out.extend(queries)
        page += 1
    return out


def backfill_from_api(days: float = 14, max_pages: int = 0) -> tuple[int, int]:
    since = time.time() - days * 86400
    raw = fetch_history_pages(max_pages=max_pages, since=since)
    recs = [r for r in (ppq_common.normalize_api_query(q) for q in raw) if r]
    conn = sqlite3.connect(BURN_DB)
    try:
        ins, dup = ppq_common.record_ppq_queries(conn, recs)
    finally:
        conn.close()
    print(f"API history: {len(raw)} fetched → {ins} inserted, {dup} dups")
    return ins, dup


# ── Proxy logs (zai_usage.db api_calls ppq_hit=1) ──────────────────────────────

def parse_since(spec: str) -> float:
    m = re.fullmatch(r"(\d+)\s*([hdw])", spec.strip().lower())
    if not m:
        raise ValueError(f"bad --since spec: {spec!r} (e.g. 48h, 7d, 2w)")
    n, unit = int(m.group(1)), m.group(2)
    return time.time() - n * {"h": 3600, "d": 86400, "w": 604800}[unit]


def backfill_from_proxy_logs(
    burn_conn: sqlite3.Connection,
    zai_conn: sqlite3.Connection,
    since_ts: float,
) -> int:
    """Import ppq-hit proxy calls as ppq_queries rows (query_type='proxy_log').

    Skips rows already covered by API-history rows: same model, same
    total_tokens, |Δts| <= COVER_WINDOW_S. Returns count inserted.
    """
    existing = burn_conn.execute(
        "SELECT ts, model, total_tokens FROM ppq_queries"
    ).fetchall()
    by_key: dict[tuple, list[float]] = {}
    for ts, model, tot in existing:
        by_key.setdefault((model, tot), []).append(ts)

    rows = zai_conn.execute(
        """
        SELECT ts, model, prompt_tokens, completion_tokens, total_tokens,
               cost_usd
        FROM api_calls
        WHERE ppq_hit = 1 AND ts >= ? AND total_tokens > 0
        ORDER BY ts
        """,
        (since_ts,),
    ).fetchall()

    recs = []
    for ts, model, pt, ct, tot, cost in rows:
        covered = any(
            abs(ts - ets) <= COVER_WINDOW_S for ets in by_key.get((model, tot), ())
        )
        if covered:
            continue
        recs.append(
            {
                "ts": ts,
                "model": model or "unknown",
                "input_tokens": pt or 0,
                "output_tokens": ct or 0,
                "total_tokens": tot,
                "cost_usd": cost,  # NULL stays NULL (unknown, not zero)
                "query_type": "proxy_log",
                "api_key_id": "",
            }
        )
    if not recs:
        return 0
    ins, _dup = ppq_common.record_ppq_queries(burn_conn, recs)
    return ins


def backfill_from_proxy(since_spec: str = "48h") -> int:
    since_ts = parse_since(since_spec)
    burn = sqlite3.connect(BURN_DB)
    zai = sqlite3.connect(f"file:{ZAI_DB}?mode=ro", uri=True)
    try:
        n = backfill_from_proxy_logs(burn, zai, since_ts)
    finally:
        burn.close()
        zai.close()
    print(f"Proxy logs ({since_spec}): {n} inserted")
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--from-api", action="store_true")
    ap.add_argument("--from-proxy", action="store_true")
    ap.add_argument("--days", type=float, default=14)
    ap.add_argument("--max-pages", type=int, default=0)
    ap.add_argument("--since", default="48h")
    args = ap.parse_args()

    if not (args.from_api or args.from_proxy):
        ap.error("nothing to do: pass --from-api and/or --from-proxy")

    if args.from_api:
        backfill_from_api(days=args.days, max_pages=args.max_pages)
    if args.from_proxy:
        backfill_from_proxy(since_spec=args.since)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
