#!/usr/bin/env python3
"""burn_attribution.py — Attribute API burn to boards/tasks/profiles (R6).

Problem: api_calls in zai_usage.db have no session_id (the proxy layer never
populated it), so the 48h burn report could not say WHICH kanban task or
chat session spent the tokens.

Approach (timestamp-overlap heuristic, no live writes):
  1. Load task runs from every board DB (~/.hermes/kanban/boards/*/kanban.db,
     table task_runs) overlapping the window.
  2. Load per-profile sessions + message activity from
     ~/.hermes/profiles/*/state.db.
  3. For each api_call, find candidates active at call ts:
       - runs whose [start, end] interval covers ts
       - sessions (only for profiles with NO active run at ts) that are
         active at ts AND have ≥1 message within MSG_WINDOW_S
  4. Split the call's tokens/cost across candidates:
       - one candidate            → full share (method=unique_run)
       - several, unequal weights → proportional to message counts in the
                                    window (method=weighted)
       - several, all-zero weight → equal split (method=overlap_equal)
  5. Calls with no candidates → method=unattributed sink row.

Honesty guarantees:
  * shares per call always sum to 1.0 (unattributed rows carry share=1.0)
  * open runs are clamped: end = min(last_heartbeat + OPEN_RUN_GRACE, now)
  * idle sessions (no message in window) never absorb burn

Usage:
    python3 burn_attribution.py --since 48h            # analyze + write
    python3 burn_attribution.py --since 48h --dry-run  # print only
    python3 burn_attribution.py --since 24h --min-tokens 1000 --top 20

Output DB: ~/.hermes/bot/burn_attribution.db, table attribution
(one row per (call_id × candidate) share, plus unattributed sink rows).
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

HOME = Path.home()
ZAI_DB = os.environ.get(
    "ZAI_USAGE_DB", str(HOME / ".hermes" / "bot" / "zai_usage.db")
)
ATTR_DB = os.environ.get(
    "BURN_ATTRIBUTION_DB", str(HOME / ".hermes" / "bot" / "burn_attribution.db")
)
BOARDS_GLOB = str(HOME / ".hermes" / "kanban" / "boards" / "*" / "kanban.db")
PROFILES_GLOB = str(HOME / ".hermes" / "profiles" / "*" / "state.db")

MSG_WINDOW_S = 90.0   # ±s around a call in which messages count as activity
OPEN_RUN_GRACE = 900.0  # s after last heartbeat an open run still counts

ATTR_SCHEMA = """
CREATE TABLE IF NOT EXISTS attribution (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    attributed_at REAL NOT NULL,
    window_since REAL NOT NULL,
    call_id      INTEGER NOT NULL,
    ts           REAL NOT NULL,
    key_name     TEXT,
    model        TEXT,
    board        TEXT,
    task_id      TEXT,
    profile      TEXT,
    kind         TEXT,   -- run | session | unattributed
    session_id   TEXT,
    method       TEXT,   -- unique_run | weighted | overlap_equal | unattributed
    share        REAL NOT NULL,
    tokens_share REAL NOT NULL,
    cost_share   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attr_window ON attribution(window_since, call_id);
CREATE INDEX IF NOT EXISTS idx_attr_profile ON attribution(window_since, profile);
"""


# ── pure attribution core ──────────────────────────────────────────────────────

def run_interval(run: dict, now: float) -> dict:
    """Normalize a run dict to an interval, clamping open runs.

    end: ended_at when present; else min(last_heartbeat + GRACE, now)
    (heartbeat falls back to start). Never before start.
    """
    start = float(run["start"])
    end = run.get("end")
    if end is None:
        hb = run.get("heartbeat")
        base = start if hb is None else float(hb)
        end = min(base + OPEN_RUN_GRACE, now)
    end = max(float(end), start)
    return {
        "board": run.get("board"),
        "task_id": run.get("task_id"),
        "profile": run.get("profile"),
        "start": start,
        "end": end,
    }


def active_runs(intervals: list[dict], ts: float) -> list[dict]:
    return [iv for iv in intervals if iv["start"] <= ts <= iv["end"]]


def _session_active(sess: tuple, ts: float, now: float) -> bool:
    sid, started, ended = sess
    if started is None:
        return False
    if ts < float(started):
        return False
    if ended is None:
        return ts <= now
    return ts <= float(ended)


def _profile_msg_weight(
    msgs: list[tuple], ts: float, window: float
) -> tuple[int, str | None]:
    """Messages of a profile within ±window; returns (count, last_session)."""
    n = 0
    last_sid = None
    for mts, sid in msgs:
        if abs(mts - ts) <= window:
            n += 1
            last_sid = sid
    return n, last_sid


def attribute_call(
    call: dict,
    intervals: list[dict],
    sessions: dict,
    msgs: dict,
    now: float,
    msg_window: float = MSG_WINDOW_S,
) -> list[dict]:
    """Split one api_call across active candidates. Returns share rows.

    call: {id, ts, key_name, model, tokens, cost_usd}
    intervals: run_interval() outputs
    sessions: profile → [(session_id, started_at, ended_at|None)]
    msgs: profile → [(msg_ts, session_id)]
    """
    ts = float(call["ts"])
    tokens = float(call.get("tokens") or 0)
    cost = float(call.get("cost_usd") or 0)

    run_cands = active_runs(intervals, ts)
    busy_profiles = {iv["profile"] for iv in run_cands}

    cands: list[dict] = [
        {**iv, "kind": "run", "session_id": None} for iv in run_cands
    ]
    for profile, sess_list in sessions.items():
        if profile in busy_profiles:
            continue  # profile has an active run; run is the candidate
        weight, last_sid = _profile_msg_weight(msgs.get(profile, []), ts, msg_window)
        if weight == 0:
            continue  # idle session never absorbs burn
        for sess in sess_list:
            if _session_active(sess, ts, now):
                cands.append(
                    {
                        "board": None,
                        "task_id": None,
                        "profile": profile,
                        "kind": "session",
                        "session_id": sess[0],
                        "start": float(sess[1] or 0),
                        "end": float(sess[2]) if sess[2] is not None else now,
                        "_weight": weight,
                    }
                )
                break  # one (most recent active) session per profile

    if not cands:
        return []

    weights = []
    for c in cands:
        w = c.pop("_weight", None)
        if w is None:  # run candidate: weight from profile message activity
            w, _ = _profile_msg_weight(msgs.get(c["profile"], []), ts, msg_window)
        weights.append(float(w))

    total_w = sum(weights)
    if len(cands) == 1 and cands[0]["kind"] == "run":
        method = "unique_run"
    elif total_w > 0:
        method = "weighted"
    else:
        method = "overlap_equal"

    if method == "weighted":
        shares = [w / total_w for w in weights]
    else:
        shares = [1.0 / len(cands)] * len(cands)

    rows = []
    for c, s in zip(cands, shares):
        rows.append(
            {
                "call_id": call.get("id"),
                "ts": ts,
                "key_name": call.get("key_name"),
                "model": call.get("model"),
                "board": c["board"],
                "task_id": c["task_id"],
                "profile": c["profile"],
                "kind": c["kind"],
                "session_id": c.get("session_id"),
                "method": method,
                "share": s,
                "tokens_share": s * tokens,
                "cost_share": s * cost,
            }
        )
    return rows


def unattributed(call: dict) -> dict:
    tokens = float(call.get("tokens") or 0)
    cost = float(call.get("cost_usd") or 0)
    return {
        "call_id": call.get("id"),
        "ts": float(call["ts"]),
        "key_name": call.get("key_name"),
        "model": call.get("model"),
        "board": None,
        "task_id": None,
        "profile": None,
        "kind": "unattributed",
        "session_id": None,
        "method": "unattributed",
        "share": 1.0,
        "tokens_share": tokens,
        "cost_share": cost,
    }


def summarize(
    total_calls: int,
    total_tokens: int,
    attributed_tokens: int,
    method_counts: dict,
) -> dict:
    pct = 100.0 * attributed_tokens / total_tokens if total_tokens else 0.0
    return {
        "total_calls": total_calls,
        "total_tokens": total_tokens,
        "attributed_tokens": attributed_tokens,
        "pct_tokens_attributed": round(pct, 1),
        "method_counts": method_counts,
    }


# ── live loaders ───────────────────────────────────────────────────────────────

def parse_since(spec: str) -> float:
    m = re.fullmatch(r"(\d+)\s*([hdw])", spec.strip().lower())
    if not m:
        raise ValueError(f"bad --since spec: {spec!r} (e.g. 48h, 7d, 2w)")
    n, unit = int(m.group(1)), m.group(2)
    return time.time() - n * {"h": 3600, "d": 86400, "w": 604800}[unit]


def load_runs(since: float, now: float) -> list[dict]:
    """Runs from all board DBs that could overlap [since, now]."""
    runs = []
    margin = 6 * 3600  # runs started before window but still running
    for dbf in sorted(glob.glob(BOARDS_GLOB)):
        board = Path(dbf).parent.name
        try:
            conn = sqlite3.connect(f"file:{dbf}?mode=ro", uri=True)
            rows = conn.execute(
                """
                SELECT task_id, profile, started_at, ended_at,
                       last_heartbeat_at, max_runtime_seconds, status
                FROM task_runs
                WHERE started_at IS NOT NULL
                  AND started_at >= ?
                  AND (ended_at IS NULL OR ended_at >= ?)
                """,
                (since - margin, since),
            ).fetchall()
            conn.close()
        except sqlite3.Error:
            continue
        for (tid, prof, s, e, hb, mrt, status) in rows:
            if status in ("claimed",):
                continue
            runs.append(
                {
                    "board": board,
                    "task_id": tid,
                    "profile": prof,
                    "start": s,
                    "end": e,
                    "heartbeat": hb,
                    "max_runtime": mrt,
                }
            )
    return runs


def load_sessions_msgs(since: float, now: float):
    """Per-profile active sessions + message timestamps from state.db files."""
    sessions: dict[str, list] = {}
    msgs: dict[str, list] = {}
    for dbf in sorted(glob.glob(PROFILES_GLOB)):
        profile = Path(dbf).parent.name
        try:
            conn = sqlite3.connect(f"file:{dbf}?mode=ro", uri=True)
            sess = conn.execute(
                """
                SELECT id, started_at, ended_at FROM sessions
                WHERE started_at IS NOT NULL
                  AND started_at >= ?
                  AND (ended_at IS NULL OR ended_at >= ?)
                """,
                (since - 6 * 3600, since),
            ).fetchall()
            mrows = conn.execute(
                """
                SELECT timestamp, session_id FROM messages
                WHERE timestamp >= ? AND timestamp <= ?
                """,
                (since - 3600, now + 60),
            ).fetchall()
            conn.close()
        except sqlite3.Error:
            continue
        if sess:
            sessions[profile] = sess
        if mrows:
            msgs[profile] = mrows
    return sessions, msgs


def load_calls(since: float, now: float, min_tokens: int = 0) -> list[dict]:
    conn = sqlite3.connect(f"file:{ZAI_DB}?mode=ro", uri=True)
    rows = conn.execute(
        """
        SELECT id, ts, key_name, model, total_tokens, cost_usd
        FROM api_calls
        WHERE ts >= ? AND ts <= ? AND total_tokens >= ?
        ORDER BY ts
        """,
        (since, now, min_tokens),
    ).fetchall()
    conn.close()
    return [
        {
            "id": r[0],
            "ts": r[1],
            "key_name": r[2],
            "model": r[3],
            "tokens": r[4],
            "cost_usd": r[5],
        }
        for r in rows
    ]


# ── driver ─────────────────────────────────────────────────────────────────────

def attribute_window(since: float, min_tokens: int = 0):
    now = time.time()
    runs = load_runs(since, now)
    ivs = [run_interval(r, now=now) for r in runs]
    sessions, msgs = load_sessions_msgs(since, now)
    calls = load_calls(since, now, min_tokens=min_tokens)

    all_rows: list[dict] = []
    method_counts: dict[str, int] = {}
    total_tokens = 0
    attributed_tokens = 0
    for call in calls:
        rows = attribute_call(call, ivs, sessions, msgs, now=now)
        if not rows:
            row = unattributed(call)
            all_rows.append(row)
            method_counts["unattributed"] = method_counts.get("unattributed", 0) + 1
        else:
            all_rows.extend(rows)
            method_counts[rows[0]["method"]] = (
                method_counts.get(rows[0]["method"], 0) + 1
            )
            attributed_tokens += float(call.get("tokens") or 0)
        total_tokens += float(call.get("tokens") or 0)

    summary = summarize(
        total_calls=len(calls),
        total_tokens=int(total_tokens),
        attributed_tokens=int(attributed_tokens),
        method_counts=method_counts,
    )
    return summary, all_rows


def write_rows(since: float, rows: list[dict]) -> None:
    conn = sqlite3.connect(ATTR_DB)
    conn.executescript(ATTR_SCHEMA)
    # Retention fix (2026-08-20): each run rebuilds the FULL window from
    # zai_usage.db, so any older window is obsolete. `since` is a fresh
    # unique float every run — the old exact-match DELETE was a no-op and
    # overlapping windows accumulated forever (34 MB after 2 runs; any
    # whole-table report double-counted the overlap exactly 2x).
    conn.execute("DELETE FROM attribution WHERE window_since <= ?", (since,))
    conn.commit()
    # Reclaim space dropped by the retention purge (DB was 34 MB after
    # two overlapping windows). VACUUM must run outside a transaction.
    conn.execute("VACUUM")
    now = time.time()
    conn.executemany(
        """
        INSERT INTO attribution
        (attributed_at, window_since, call_id, ts, key_name, model, board,
         task_id, profile, kind, session_id, method, share, tokens_share,
         cost_share)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (
                now, since, r["call_id"], r["ts"], r["key_name"], r["model"],
                r["board"], r["task_id"], r["profile"], r["kind"],
                r["session_id"], r["method"], r["share"], r["tokens_share"],
                r["cost_share"],
            )
            for r in rows
        ],
    )
    conn.commit()
    conn.close()


def print_report(summary: dict, rows: list[dict], top: int = 15) -> None:
    print(json.dumps(summary, indent=2))
    # top consumers by tokens_share
    agg: dict[tuple, float] = {}
    for r in rows:
        if r["kind"] == "unattributed":
            key = ("(unattributed)",)
        elif r["kind"] == "run":
            key = (r["board"], r["task_id"], r["profile"])
        else:
            key = ("session:" + (r["profile"] or "?"), r["session_id"])
        agg[key] = agg.get(key, 0.0) + r["tokens_share"]
    ranked = sorted(agg.items(), key=lambda kv: -kv[1])[:top]
    total = summary["total_tokens"] or 1
    print(f"\nTop {len(ranked)} consumers (tokens, % of window):")
    for key, toks in ranked:
        label = "/".join(str(k) for k in key)
        print(f"  {toks:>13,.0f}  {100 * toks / total:5.1f}%  {label}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Attribute API burn to tasks")
    ap.add_argument("--since", default="48h")
    ap.add_argument("--min-tokens", type=int, default=0)
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    since = parse_since(args.since)
    summary, rows = attribute_window(since, min_tokens=args.min_tokens)
    print_report(summary, rows, top=args.top)
    if args.dry_run:
        print("\n(dry run — nothing written)")
    else:
        write_rows(since, rows)
        print(f"\nwrote {len(rows)} rows → {ATTR_DB}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
