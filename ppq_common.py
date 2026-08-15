#!/usr/bin/env python3
"""ppq_common.py — Shared ppq_queries persistence layer (R6).

Single home for:
  * ensure_schema(conn)      — idempotent CREATE TABLE + dedup index
  * normalize_api_query(raw) — PPQ /queries/history item → ppq_queries row
  * record_ppq_queries(conn, recs) — dedup-safe INSERT

Both api_burn_collector.py (live path, every 5 min) and ppq_backfill.py
(history import) write through this module so dedup semantics stay in one
place.

Dedup key: (ts rounded to 1s, model, total_tokens). The collector fetches
the last 100 queries on every tick, so re-inserts are the common case —
a UNIQUE index makes the guarantee cheap and race-free:

    CREATE UNIQUE INDEX IF NOT EXISTS idx_ppq_queries_dedup
        ON ppq_queries (ts, model, total_tokens);

Legacy rows predating the index are tolerated: ensure_schema creates the
index with ``INSERT OR IGNORE`` semantics via the OR-conflict clause, and
record_ppq_queries uses INSERT OR IGNORE so pre-existing duplicates in an
old table never crash a write.

Run standalone to report table stats:
    python3 ppq_common.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = os.environ.get(
    "API_BURN_DB_PATH", str(Path.home() / ".hermes" / "bot" / "api_burn.db")
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS ppq_queries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    model       TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    total_tokens INTEGER,
    cost_usd    REAL,
    query_type  TEXT,
    api_key_id  TEXT
);
CREATE INDEX IF NOT EXISTS idx_ppq_queries_ts ON ppq_queries(ts);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create ppq_queries + indexes if missing. Idempotent.

    Adds idx_ppq_queries_dedup (UNIQUE on ts/model/total_tokens). On legacy
    tables that already contain exact duplicates the UNIQUE index creation
    itself would fail, so we dedupe in-place first (keep lowest rowid).
    """
    conn.executescript(SCHEMA)
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_ppq_queries_dedup"
            " ON ppq_queries (ts, model, total_tokens)"
        )
    except sqlite3.IntegrityError:
        # legacy duplicates: keep the first (lowest id) of each dedup key
        conn.execute(
            """
            DELETE FROM ppq_queries WHERE id NOT IN (
                SELECT MIN(id) FROM ppq_queries
                GROUP BY ts, model, total_tokens
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_ppq_queries_dedup"
            " ON ppq_queries (ts, model, total_tokens)"
        )
    conn.commit()


def normalize_api_query(q: dict) -> dict | None:
    """Map a raw PPQ /queries/history item to a ppq_queries row dict.

    Accepts the API field names (timestamp, input_count, output_count,
    price_in_usd, query_type, api_key_id) and coerces types defensively:
    string numerics, nulls, missing keys. Returns None when the record has
    no usable timestamp (unparseable or absent) — caller should skip.
    """
    ts_raw = q.get("timestamp")
    if isinstance(ts_raw, (int, float)):
        ts = float(ts_raw)
    else:
        try:
            ts = datetime.fromisoformat(
                str(ts_raw).replace("Z", "+00:00")
            ).timestamp()
        except Exception:
            return None

    def _int(v):
        try:
            return int(v) if v is not None else 0
        except (TypeError, ValueError):
            return 0

    def _float(v):
        try:
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    inp = _int(q.get("input_count"))
    out = _int(q.get("output_count"))
    model = q.get("model") or "unknown"
    qtype = q.get("query_type") or ""
    key_id = q.get("api_key_id")
    return {
        "ts": ts,
        "model": str(model),
        "input_tokens": inp,
        "output_tokens": out,
        "total_tokens": inp + out,
        "cost_usd": _float(q.get("price_in_usd")),
        "query_type": str(qtype),
        "api_key_id": "" if key_id is None else str(key_id),
    }


def record_ppq_queries(
    conn: sqlite3.Connection, records: list[dict]
) -> tuple[int, int]:
    """Insert normalized records with dedup. Returns (inserted, duplicates).

    Uses INSERT OR IGNORE against the UNIQUE dedup index, so records that
    already exist (same ts+model+total_tokens) are skipped atomically —
    safe under concurrent writers on WAL.
    """
    ensure_schema(conn)
    inserted = 0
    duplicates = 0
    for rec in records:
        if not rec or "ts" not in rec:
            continue
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO ppq_queries
            (ts, model, input_tokens, output_tokens, total_tokens,
             cost_usd, query_type, api_key_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                float(rec.get("ts", 0)),
                rec.get("model") or "unknown",
                int(rec.get("input_tokens", 0) or 0),
                int(rec.get("output_tokens", 0) or 0),
                int(rec.get("total_tokens", 0) or 0),
                # missing key → 0.0; explicit None (unknown cost, e.g. proxy
                # logs) preserved as NULL
                rec.get("cost_usd", 0.0),
                rec.get("query_type") or "",
                rec.get("api_key_id") or "",
            ),
        )
        if cur.rowcount > 0:
            inserted += 1
        else:
            duplicates += 1
    conn.commit()
    return inserted, duplicates


def main() -> int:
    db = sys.argv[1] if len(sys.argv) > 1 else DB_PATH
    if not os.path.exists(db):
        print(f"no db at {db}")
        return 1
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        n = conn.execute("SELECT COUNT(*) FROM ppq_queries").fetchone()[0]
        span = conn.execute("SELECT MIN(ts), MAX(ts) FROM ppq_queries").fetchone()
        tot = conn.execute(
            "SELECT SUM(total_tokens), SUM(cost_usd) FROM ppq_queries"
        ).fetchone()
        print(f"ppq_queries: {n} rows")
        if n:
            print(
                f"  span: {datetime.fromtimestamp(span[0], tz=timezone.utc)}"
                f" → {datetime.fromtimestamp(span[1], tz=timezone.utc)}"
            )
            print(f"  tokens: {tot[0]:,}  usd: ${tot[1] or 0:.4f}")
    except sqlite3.OperationalError as e:
        print(f"table missing: {e}")
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
