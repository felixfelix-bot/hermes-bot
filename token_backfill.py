#!/usr/bin/env python3
"""token_backfill.py — Backfill messages.token_count from api_calls (R6).

Hermes only started populating messages.token_count recently; older rows
have NULL. But the proxy logged completion_tokens per api_call, and
burn_attribution.py resolves calls → (profile, session_id). For every
attributed call we can find the first assistant message that follows the
call in that session and stamp it with the call's completion_tokens.

Safety rules (this writes to live profile state.db files):
  * NEVER overwrite an existing non-NULL token_count
  * only the FIRST following NULL assistant message within MATCH_WINDOW_S
  * skip messages newer than FRESH_CUTOFF_S (live sessions may still be
    writing — avoid racing the agent)
  * dry-run by default; --apply to write

Usage:
    python3 token_backfill.py --since 48h           # report only
    python3 token_backfill.py --since 48h --apply   # write
"""

from __future__ import annotations

import argparse
import glob
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import burn_attribution as ba  # noqa: E402  (parse_since, ATTR_DB)

HOME = Path.home()
PROFILES_DIR = HOME / ".hermes" / "profiles"
ZAI_DB = os.environ.get(
    "ZAI_USAGE_DB", str(HOME / ".hermes" / "bot" / "zai_usage.db")
)

MATCH_WINDOW_S = 120.0    # call → first assistant msg within this horizon
FRESH_CUTOFF_S = 300.0    # never touch messages newer than now-300s
WINDOW_TOLERANCE_S = 600.0  # window_since drift between processes


def backfill_session_tokens(
    conn: sqlite3.Connection,
    session_id: str,
    call: dict,
    now: float | None = None,
    match_window: float = MATCH_WINDOW_S,
    fresh_cutoff: float = FRESH_CUTOFF_S,
) -> int:
    """Stamp the call's completion_tokens onto its response message.

    Returns 1 if a row was updated, 0 otherwise. Never overwrites.
    """
    now = time.time() if now is None else now
    ts = float(call["ts"])
    comp = call.get("completion_tokens")
    if not comp or comp <= 0:
        return 0
    row = conn.execute(
        """
        SELECT id FROM messages
        WHERE session_id = ?
          AND role = 'assistant'
          AND token_count IS NULL
          AND timestamp >= ? AND timestamp <= ?
          AND timestamp <= ?
        ORDER BY timestamp ASC, id ASC
        LIMIT 1
        """,
        (session_id, ts, ts + match_window, now - fresh_cutoff),
    ).fetchone()
    if not row:
        return 0
    conn.execute(
        "UPDATE messages SET token_count = ? WHERE id = ? AND token_count IS NULL",
        (int(comp), row[0]),
    )
    conn.commit()
    return 1


def _state_db(profile: str) -> sqlite3.Connection | None:
    p = PROFILES_DIR / profile / "state.db"
    if not p.exists():
        return None
    return sqlite3.connect(str(p))


def load_attribution_targets(
    since: float, tolerance: float = WINDOW_TOLERANCE_S
) -> list[dict]:
    """(profile, session_id, call) tuples from the attribution output.

    attribution lives in ATTR_DB, call details (ts, completion_tokens) in
    ZAI_DB — two separate databases, so query each on its own connection.
    `since` is matched with ±tolerance: window_since was computed by
    time.time()-N in ANOTHER process (burn_attribution.py), so exact float
    equality would never match across runs.
    """
    conn = sqlite3.connect(f"file:{ba.ATTR_DB}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT profile, session_id, call_id
            FROM attribution
            WHERE ABS(window_since - ?) <= ? AND session_id IS NOT NULL
            """,
            (since, tolerance),
        ).fetchall()
    finally:
        conn.close()
    zai = sqlite3.connect(f"file:{ZAI_DB}?mode=ro", uri=True)
    out = []
    for prof, sid, cid in [(r[0], r[1], r[2]) for r in rows]:
        call = zai.execute(
            "SELECT id, ts, completion_tokens FROM api_calls WHERE id = ?",
            (cid,),
        ).fetchone()
        if call:
            out.append(
                {
                    "profile": prof,
                    "session_id": sid,
                    "call": {
                        "call_id": call[0],
                        "ts": call[1],
                        "completion_tokens": call[2],
                    },
                }
            )
    zai.close()
    return out


def run(since: float, apply: bool) -> dict:
    targets = load_attribution_targets(since)
    per_profile: dict[str, sqlite3.Connection | None] = {}
    stats = {"targets": len(targets), "written": 0, "skipped": 0}
    try:
        for t in targets:
            prof = t["profile"]
            if prof not in per_profile:
                per_profile[prof] = _state_db(prof)
            conn = per_profile[prof]
            if conn is None:
                stats["skipped"] += 1
                continue
            if apply:
                n = backfill_session_tokens(conn, t["session_id"], t["call"])
                stats["written" if n else "skipped"] += 1
            else:
                # dry-run: same query, no write
                ts = float(t["call"]["ts"])
                comp = t["call"].get("completion_tokens") or 0
                if comp <= 0:
                    stats["skipped"] += 1
                    continue
                row = conn.execute(
                    """
                    SELECT id FROM messages
                    WHERE session_id = ? AND role = 'assistant'
                      AND token_count IS NULL
                      AND timestamp >= ? AND timestamp <= ? AND timestamp <= ?
                    ORDER BY timestamp ASC, id ASC LIMIT 1
                    """,
                    (
                        t["session_id"], ts, ts + MATCH_WINDOW_S,
                        time.time() - FRESH_CUTOFF_S,
                    ),
                ).fetchone()
                stats["written" if row else "skipped"] += 1
    finally:
        for c in per_profile.values():
            if c is not None:
                c.close()
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill messages.token_count")
    ap.add_argument("--since", default="48h")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    since = ba.parse_since(args.since)
    stats = run(since, apply=args.apply)
    mode = "APPLIED" if args.apply else "DRY-RUN (pass --apply to write)"
    print(f"{mode}: {stats['written']} stampable, "
          f"{stats['skipped']} unmatched, {stats['targets']} targets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
