#!/usr/bin/env python3
"""
Rate-limit gate for dispatch-level worker spawning control.

Checks five conditions before allowing worker dispatch:
1. Is current UTC hour a known rate-limit hour? (historical 429 frequency by hour)
2. Any 429 in last 5 minutes? (active rate-limit burst)
3. Kalman prediction: will quota exhaust during next task duration?
4. recent_503: >=3 z.ai upstream 503/5xx within 10 min (anomaly_events
   key_backoff/error_type=server rows, api_calls status_code=503 rows, or the
   zai-proxy journald log) -> paused, reason zai-503-outage, resume_at =
   now + min(Retry-After, 20 min). Fail-closed on confirmed burst; the pause
   is re-evaluated on every 5-min cron run.
5. quota_windows: any 5-hour/weekly/monthly window >=85% used (from the
   localhost:9099 proxy /quota cache — same source the dq05 monitor reads)
   -> advisory pause with resume_at = next window reset. Fail-open on
   missing/stale data.

Outputs JSON state to ~/.hermes/state/rate_limit_gate.json:
  {paused: bool, resume_at: iso_ts|null, reason: str, ts: iso_ts,
   checked_at: iso_ts, checks: {...}}

Exit code 0 = clear (dispatch OK), exit code 1 = paused (skip dispatch).

Usage:
  python3 rate_limit_gate.py [--duration SECONDS] [--db PATH]
  --duration: estimated next task duration in seconds (default 300)
  --db: path to zai_usage.db (default ~/.hermes/bot/zai_usage.db)
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = os.path.expanduser("~/.hermes/bot/zai_usage.db")
STATE_PATH = os.environ.get(
    "HERMES_GATE_STATE",
    os.path.expanduser("~/.hermes/state/rate_limit_gate.json"),
)
DEFAULT_DURATION = 300  # 5 min estimated task duration

# --- Rate-limit hot hours (from historical analysis: peak 02-05 + 10-11 UTC) ---
# These are also computed dynamically, but we seed with known peaks.
KNOWN_PEAK_HOURS = {2, 3, 4, 5, 10, 11}

# Thresholds
RECENT_429_WINDOW = 300          # 5 min
RECENT_429_THRESHOLD = 1         # any 429 in window → pause
PEAK_HOUR_429_RATIO = 0.15       # if current hour historically has >15% of all 429s → cautious
KALMAN_EXHAUST_HOURS = 0.5       # if Kalman predicts exhaust in < 0.5h → pause
MIN_SAMPLES_FOR_PEAK = 5         # need at least this many 429s in an hour to call it peak

# --- T3.1: z.ai 503-outage awareness (fail-closed on confirmed burst) ---
RECENT_503_WINDOW = 600          # 10 min
RECENT_503_THRESHOLD = 3         # >=3 z.ai upstream 503/5xx in window → outage
MAX_503_RESUME_S = 20 * 60       # resume_at = now + min(Retry-After, 20 min)

# --- T3.1: quota-window awareness (advisory pause, fail-open on missing data) ---
QUOTA_URL = os.environ.get("HERMES_GATE_QUOTA_URL", "http://localhost:9099/quota")
QUOTA_HTTP_TIMEOUT = 4           # seconds; proxy is local
QUOTA_WINDOW_PCT = 85.0          # any window >= this pct used → pause
QUOTA_STALE_S = 1800             # /quota payload older than 30 min → treat as missing
QUOTA_FALLBACK_RESUME_S = 1800   # window hot but resets_at unknown → re-check in 30 min

PROXY_JOURNAL_UNIT = "zai-proxy.service"
_BACKOFF_RE = re.compile(r"backoff (\d+(?:\.\d+)?)s")
_JOURNAL_503_RE = re.compile(r"\b503\b")
# Error-context tokens for journal lines. NOTE: 'proxy' is deliberately NOT
# in this alternation — the syslog ident ('zai-proxy[pid]') contains it, and
# matching the raw line would let every line from this unit pass the filter.
# Both regexes are applied to the MESSAGE text only (after 'ident[pid]: ').
_JOURNAL_ERROR_CTX_RE = re.compile(r"error|exhaust|fail|upstream|timeout", re.I)


def utc_now():
    return datetime.now(timezone.utc)


def iso(ts=None):
    if ts is None:
        ts = time.time()
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def connect(db_path):
    if not os.path.exists(db_path):
        print(f"WARN: DB not found at {db_path}, gate defaults to CLEAR", file=sys.stderr)
        return None
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def check_peak_hour(conn):
    """Check if current UTC hour is a known rate-limit hot hour.

    Queries rate_limit_samples grouped by UTC hour to find hours with
    historically high 429 frequency. Falls back to KNOWN_PEAK_HOURS seed.
    """
    now = utc_now()
    current_hour = now.hour

    # Query: count 429s per UTC hour from historical data
    try:
        rows = conn.execute("""
            SELECT
                CAST(strftime('%H', datetime(ts, 'unixepoch')) AS INTEGER) AS hour,
                COUNT(*) AS cnt
            FROM rate_limit_samples
            GROUP BY hour
            ORDER BY cnt DESC
        """).fetchall()

        # Build dynamic peak set: hours with >= MIN_SAMPLES_FOR_PEAK 429s
        dynamic_peaks = set()
        total = sum(r["cnt"] for r in rows) if rows else 0
        for r in rows:
            if r["cnt"] >= MIN_SAMPLES_FOR_PEAK:
                dynamic_peaks.add(r["hour"])

        peak_hours = dynamic_peaks | KNOWN_PEAK_HOURS

        is_peak = current_hour in peak_hours

        # Find the count for current hour
        current_hour_count = 0
        for r in rows:
            if r["hour"] == current_hour:
                current_hour_count = r["cnt"]
                break

        # Compute ratio
        ratio = (current_hour_count / total) if total > 0 else 0.0

        return {
            "is_peak": is_peak,
            "current_hour": current_hour,
            "current_hour_429s": current_hour_count,
            "total_429s": total,
            "ratio": round(ratio, 3),
            "dynamic_peaks": sorted(dynamic_peaks),
            "reason": f"hour {current_hour:02d}Z is a known rate-limit peak hour ({current_hour_count} historical 429s)"
                      if is_peak else None,
        }
    except Exception as e:
        return {"is_peak": False, "error": str(e), "reason": None}


def check_recent_429(conn):
    """Check for any 429 (rate_limit_sample) in the last N seconds."""
    cutoff = time.time() - RECENT_429_WINDOW
    try:
        rows = conn.execute("""
            SELECT COUNT(*) AS cnt, MAX(ts) AS last_ts
            FROM rate_limit_samples
            WHERE ts >= ?
        """, (cutoff,)).fetchone()

        count = rows["cnt"] if rows else 0
        last_ts = rows["last_ts"] if rows and rows["last_ts"] else None

        # Also check api_calls for status_code=429
        api_rows = conn.execute("""
            SELECT COUNT(*) AS cnt, MAX(ts) AS last_ts
            FROM api_calls
            WHERE status_code = 429 AND ts >= ?
        """, (cutoff,)).fetchone()

        api_count = api_rows["cnt"] if api_rows else 0

        total = count + api_count
        triggered = total >= RECENT_429_THRESHOLD

        # Estimate resume time from last retry_after_estimate if available.
        # (Defensive: older schemas have no retry_after_estimate column — a
        # missing hint must never discard the already-computed trigger.)
        resume_offset = 60  # default 1 min backoff
        if last_ts:
            try:
                recent = conn.execute("""
                    SELECT retry_after_estimate FROM rate_limit_samples
                    WHERE ts >= ? ORDER BY ts DESC LIMIT 1
                """, (cutoff,)).fetchone()
                if recent and recent["retry_after_estimate"] and recent["retry_after_estimate"] > 0:
                    resume_offset = recent["retry_after_estimate"]
            except Exception:
                pass

        return {
            "triggered": triggered,
            "count": total,
            "rls_count": count,
            "api_count": api_count,
            "last_ts": last_ts,
            "resume_offset": resume_offset,
            "reason": f"{total} 429(s) in last {RECENT_429_WINDOW}s (rls={count}, api={api_count})"
                      if triggered else None,
        }
    except Exception as e:
        return {"triggered": False, "error": str(e), "resume_offset": 60, "reason": None}


def check_kalman(conn, task_duration_s):
    """Check Kalman prediction for quota exhaustion during task window.

    Looks at latest kalman_samples for 'ours' key to see if any window
    predicts exhaustion within the task duration window.
    """
    try:
        # Get latest sample per window
        rows = conn.execute("""
            SELECT k.*
            FROM kalman_samples k
            INNER JOIN (
                SELECT window, MAX(ts) AS max_ts
                FROM kalman_samples
                WHERE key = 'ours'
                GROUP BY window
            ) latest ON k.window = latest.window AND k.ts = latest.max_ts
            WHERE k.key = 'ours'
            ORDER BY k.projected_total_pct DESC
        """).fetchall()

        if not rows:
            return {"triggered": False, "reason": None, "windows": []}

        # Convert task duration to hours for comparison
        task_duration_h = task_duration_s / 3600.0

        windows_info = []
        worst = None
        for r in rows:
            info = {
                "window": r["window"],
                "used_pct": r["used_pct_observed"],
                "projected_total_pct": r["projected_total_pct"],
                "burn_rate_tph": r["burn_rate_tph"],
                "exhausts_in_hours": r["exhausts_in_hours"],
                "will_exhaust": bool(r["will_exhaust"]),
                "note": r["note"],
            }
            windows_info.append(info)

            # Trigger if: will_exhaust flag is set AND exhausts within our task window
            # OR projected_total_pct >= 95 (near ceiling)
            if r["will_exhaust"] and r["exhausts_in_hours"] is not None:
                if r["exhausts_in_hours"] <= KALMAN_EXHAUST_HOURS:
                    if worst is None or r["exhausts_in_hours"] < worst["exhausts_in_hours"]:
                        worst = info
            elif r["projected_total_pct"] is not None and r["projected_total_pct"] >= 95.0:
                if worst is None:
                    worst = info

        triggered = worst is not None
        reason = None
        if worst:
            if worst["will_exhaust"]:
                reason = (f"Kalman: {worst['window']} window predicts exhaustion in "
                          f"{worst['exhausts_in_hours']:.1f}h (projected {worst['projected_total_pct']:.1f}%)")
            else:
                reason = (f"Kalman: {worst['window']} window projected at "
                          f"{worst['projected_total_pct']:.1f}% (>= 95% threshold)")

        # Resume estimate: if we know exhausts_in_hours, resume after that window
        resume_offset = 600  # default 10 min
        if worst and worst["exhausts_in_hours"] and worst["exhausts_in_hours"] > 0:
            resume_offset = int(worst["exhausts_in_hours"] * 3600) + 60  # +1min buffer

        return {
            "triggered": triggered,
            "reason": reason,
            "resume_offset": resume_offset,
            "windows": windows_info,
        }
    except Exception as e:
        return {"triggered": False, "error": str(e), "resume_offset": 600, "reason": None, "windows": []}


# --- T3.1: recent z.ai 503/5xx burst (pure decision helpers + collectors) ---

def evaluate_recent_503(events, now):
    """Pure: decide whether a set of observed 503/5xx events is a burst.

    events: list of {ts: unix float, source: str, retry_after_s: float|None}.
    Triggers on >= RECENT_503_THRESHOLD events inside RECENT_503_WINDOW.
    resume_offset = min(max retry hint, MAX_503_RESUME_S); defaults to the
    20-min cap when no Retry-After hint is available.
    """
    recent = [e for e in events if now - e["ts"] <= RECENT_503_WINDOW]
    triggered = len(recent) >= RECENT_503_THRESHOLD
    result = {
        "triggered": triggered,
        "count": len(recent),
        "resume_offset": MAX_503_RESUME_S,
        "reason": None,
        "fail_open": len(events) == 0,
    }
    if triggered:
        hints = [e.get("retry_after_s") for e in recent if e.get("retry_after_s")]
        if hints:
            result["resume_offset"] = min(max(hints), MAX_503_RESUME_S)
        result["reason"] = (
            f"zai-503-outage: {len(recent)} upstream 503/5xx in last "
            f"{RECENT_503_WINDOW}s"
        )
    return result


def pick_503_decision(results):
    """Pure: pick the per-source result with the highest confirmed count.

    results: list of evaluate_recent_503 outputs (one per source), each
    annotated with "source". Sources observe the same underlying failures,
    so the max (not the sum) is the conservative confirmed count.
    """
    triggered = [r for r in results if r.get("triggered")]
    if triggered:
        return max(triggered, key=lambda r: r.get("count", 0))
    counts = [r.get("count", 0) for r in results] or [0]
    return {
        "triggered": False,
        "count": max(counts),
        "resume_offset": MAX_503_RESUME_S,
        "reason": None,
        "fail_open": not results,
    }


def collect_503_events_anomaly(conn, now):
    """Collect upstream server-error events from anomaly_events.

    The proxy records every upstream 500/502/503/504 as a key_backoff WARN
    anomaly with 'error_type=server' in the detail, including its own
    backoff hint ('backoff Ns'). Missing table → [] (fail-open).
    """
    try:
        rows = conn.execute(
            """SELECT ts, detail FROM anomaly_events
               WHERE category = 'key_backoff'
                 AND detail LIKE '%error_type=server%'
                 AND ts >= ?
               ORDER BY ts""",
            (now - RECENT_503_WINDOW,),
        ).fetchall()
    except Exception:
        return []
    events = []
    for r in rows:
        hint = None
        if r["detail"]:
            m = _BACKOFF_RE.search(r["detail"])
            if m:
                hint = float(m.group(1))
        events.append({"ts": r["ts"], "source": "anomaly", "retry_after_s": hint})
    return events


def collect_503_events_api(conn, now):
    """Collect upstream 5xx rows from api_calls (zai tier only).

    The proxy logs every request through the z.ai path with tier='zai':
    upstream 503s, 'all providers exhausted' 503s, and — the only outage
    class actually observed in production (2026-08-15 read timeouts) —
    generic-exception failures, which the proxy returns to clients as 502
    'proxy error: ...'. External failover attempts log their own rows with
    tier=<provider_name>; a failing external provider while z.ai is healthy
    is NOT a z.ai outage, so those rows are excluded. Missing tier (NULL or
    empty string — legacy pre-RP-1 rows or writer anomalies) stays visible —
    in that era only the zai path wrote 5xx rows at all. The query failing
    (e.g. schema without the tier column) fails open to [] like every DB
    collector here; the journal source's stderr WARN is the observability
    pattern for dead sources.
    """
    try:
        rows = conn.execute(
            """SELECT ts FROM api_calls
               WHERE status_code IN (502, 503, 504)
                 AND (tier = 'zai' OR tier IS NULL OR tier = '')
                 AND ts >= ?
               ORDER BY ts""",
            (now - RECENT_503_WINDOW,),
        ).fetchall()
    except Exception:
        return []
    return [{"ts": r["ts"], "source": "api_calls", "retry_after_s": None}
            for r in rows]


def collect_503_events_journal(now):
    """Best-effort: count 503 lines in the zai-proxy journald log.

    The proxy's stdout/stderr go to journald. Unreadable journal (common for
    unprivileged cron) → [] — the DB sources above are the primary signal.
    """
    try:
        proc = subprocess.run(
            ["journalctl", "-u", PROXY_JOURNAL_UNIT, "--no-pager", "-q",
             "-o", "short-unix", "--since", f"-{RECENT_503_WINDOW // 60} min"],
            capture_output=True, text=True, timeout=3,
        )
        if proc.returncode != 0:
            return []
        if not proc.stdout.strip():
            # rc=0 with zero lines: either journal-permission death under the
            # cron user (t_faa7c86f finding 2) or a genuinely quiet window.
            # Either way the journal source contributes nothing this run —
            # say so once on stderr so cron output shows which sources are
            # live; the DB sources remain primary.
            print(f"WARN: journalctl -u {PROXY_JOURNAL_UNIT} returned 0 "
                  f"lines (journal perms or quiet window) — journal 503 "
                  f"source contributed nothing this run; relying on DB "
                  f"sources", file=sys.stderr)
    except Exception:
        return []
    cutoff = now - RECENT_503_WINDOW
    events = []
    for line in proc.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        try:
            ts = float(parts[0])
        except ValueError:
            continue
        # short-unix format: 'ts host ident[pid]: message'. Match only the
        # MESSAGE text — the ident ('zai-proxy[503]') is where PID/counter
        # false positives live. A line without the 'ident[pid]: ' separator
        # is malformed for this unit — skip it rather than risk matching
        # the host/ident portion.
        msg = parts[1]
        bracket = msg.find("]: ")
        if bracket == -1:
            continue
        msg = msg[bracket + 3:]
        if ts < cutoff or not _JOURNAL_503_RE.search(msg):
            continue
        if not _JOURNAL_ERROR_CTX_RE.search(msg):
            continue  # bare '503' in a PID/counter is not an outage signal
        events.append({"ts": ts, "source": "journal", "retry_after_s": None})
    return events


# --- T3.1: quota-window awareness (pure helpers + collector) ---

def parse_quota_payload(payload, now):
    """Pure: extract per-key windows from the proxy /quota payload.

    Skips sections that are not dicts with a 'windows' list (e.g. 'active',
    'ollama_cloud', 'proactive_cooldown') and keys whose data is stale
    (age_s > QUOTA_STALE_S). Malformed payload → [] (fail-open).
    """
    if not isinstance(payload, dict):
        return []
    windows = []
    for key, section in payload.items():
        if not isinstance(section, dict):
            continue
        age = section.get("age_s")
        if isinstance(age, (int, float)) and age > QUOTA_STALE_S:
            continue
        raw = section.get("windows")
        if not isinstance(raw, list):
            continue
        for w in raw:
            if not isinstance(w, dict):
                continue
            used = w.get("used_pct")
            if not isinstance(used, (int, float)):
                continue
            resets = w.get("resets_at")
            if not isinstance(resets, (int, float)):
                resets = None
            windows.append({
                "key": key,
                "name": str(w.get("name", "unknown")),
                "used_pct": float(used),
                "resets_at": resets,
            })
    return windows


def evaluate_quota_windows(windows, now):
    """Pure: pause if any quota window is >= QUOTA_WINDOW_PCT used.

    resume_at = the hot window's resets_at; when resets_at is unknown, fall
    back to a 30-min re-check offset instead of pausing forever. Windows
    whose resets_at already passed are ignored (stale sample, next cron run
    refreshes the data).
    """
    result = {
        "triggered": False,
        "window": None,
        "key": None,
        "used_pct": None,
        "resume_at_ts": None,
        "fallback_resume": False,
        "reason": None,
        "fail_open": len(windows) == 0,
    }
    candidates = []
    for w in windows:
        if w["used_pct"] < QUOTA_WINDOW_PCT:
            continue
        if w["resets_at"] is not None and w["resets_at"] <= now:
            continue
        candidates.append(w)
    if not candidates:
        return result
    worst = max(candidates, key=lambda w: w["used_pct"])
    result["triggered"] = True
    result["window"] = worst["name"]
    result["key"] = worst["key"]
    result["used_pct"] = worst["used_pct"]
    if worst["resets_at"] is None:
        result["resume_at_ts"] = now + QUOTA_FALLBACK_RESUME_S
        result["fallback_resume"] = True
    else:
        result["resume_at_ts"] = worst["resets_at"]
    result["reason"] = (
        f"QUOTA-WINDOW: {worst['key']} {worst['name']} window at "
        f"{worst['used_pct']:.1f}% (>= {QUOTA_WINDOW_PCT:.0f}% threshold) — "
        f"advisory pause until window reset"
    )
    return result


def fetch_quota_payload():
    """GET the proxy /quota cache. Returns the parsed dict or None (fail-open)."""
    try:
        with urllib.request.urlopen(QUOTA_URL, timeout=QUOTA_HTTP_TIMEOUT) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


# --- Decision (priority: 503 outage > active 429 > quota window > Kalman > peak) ---

def decide(now, recent_503, recent_429, quota, kalman, peak):
    """Pure: combine check results into the gate decision.

    The 503-outage reason must keep its 'zai-*' prefix at the TOP level:
    T3.2 (dispatcher pause semantics) and T3.3 (worker taxonomy) match on
    that prefix, so no decorative prefix may be prepended here.
    """
    if recent_503.get("triggered"):
        return (True,
                recent_503.get("reason") or "zai-503-outage",
                iso(now + recent_503.get("resume_offset", MAX_503_RESUME_S)))
    if recent_429.get("triggered"):
        return (True,
                f"ACTIVE 429: {recent_429.get('reason')}",
                iso(now + recent_429.get("resume_offset", 60)))
    if quota.get("triggered"):
        return (True,
                quota.get("reason") or "QUOTA-WINDOW: window >= threshold",
                iso(quota.get("resume_at_ts", now + QUOTA_FALLBACK_RESUME_S)))
    if kalman.get("triggered"):
        return (True,
                f"KALMAN: {kalman.get('reason')}",
                iso(now + kalman.get("resume_offset", 600)))
    if peak.get("is_peak"):
        return (False,
                f"ADVISORY: {peak.get('reason')} — dispatch with caution",
                None)
    return (False, "clear", None)


def run_gate(db_path=DEFAULT_DB, task_duration=DEFAULT_DURATION, verbose=False):
    """Run all five checks and produce the gate decision."""
    now = time.time()
    conn = connect(db_path)

    # --- T3.1 checks run even without the DB (journal + proxy /quota) ---
    per_source = []
    journal_events = collect_503_events_journal(now)
    per_source.append(("journal", evaluate_recent_503(journal_events, now)))

    if conn is None:
        peak = {"is_peak": False, "reason": None, "error": "DB not found"}
        recent = {"triggered": False, "resume_offset": 60, "reason": None,
                  "error": "DB not found"}
        kalman = {"triggered": False, "reason": None, "windows": [],
                  "error": "DB not found"}
    else:
        try:
            per_source.insert(0, ("anomaly", evaluate_recent_503(
                collect_503_events_anomaly(conn, now), now)))
            per_source.insert(1, ("api_calls", evaluate_recent_503(
                collect_503_events_api(conn, now), now)))
            peak = check_peak_hour(conn)
            recent = check_recent_429(conn)
            kalman = check_kalman(conn, task_duration)
        finally:
            conn.close()

    annotated = []
    for source, res in per_source:
        res = dict(res)
        res["source"] = source
        annotated.append(res)
    burst = pick_503_decision(annotated)

    quota_payload = fetch_quota_payload()
    if quota_payload is None:
        quota = {"triggered": False, "reason": None, "fail_open": True,
                 "error": f"quota collector unreachable at {QUOTA_URL}"}
    else:
        quota = evaluate_quota_windows(parse_quota_payload(quota_payload, now), now)

    paused, reason, resume_at = decide(now, burst, recent, quota, kalman, peak)

    if not paused and conn is None:
        reason = "DB not found — gate defaults to clear"

    result = {
        "paused": paused,
        "resume_at": resume_at,
        "reason": reason,
        "ts": iso(now),
        "checked_at": iso(now),
        "checks": {
            "peak_hour": peak,
            "recent_429": {k: v for k, v in recent.items() if k != "reason"},
            "kalman": {k: v for k, v in kalman.items() if k != "reason"},
            "recent_503": {k: v for k, v in burst.items() if k != "reason"},
            "quota_windows": quota,
        },
    }

    if verbose:
        result["checks"]["recent_429"]["reason"] = recent.get("reason")
        result["checks"]["kalman"]["reason"] = kalman.get("reason")
        result["checks"]["recent_503"]["reason"] = burst.get("reason")

    return result


def write_state(result):
    """Write gate decision to state file (HERMES_GATE_STATE overrides path)."""
    state_path = os.environ.get("HERMES_GATE_STATE", STATE_PATH)
    Path(state_path).parent.mkdir(parents=True, exist_ok=True)
    with open(state_path, "w") as f:
        json.dump(result, f, indent=2, default=str)


def main():
    parser = argparse.ArgumentParser(description="Rate-limit gate for dispatch control")
    parser.add_argument("--duration", type=int, default=DEFAULT_DURATION,
                        help=f"Estimated next task duration in seconds (default {DEFAULT_DURATION})")
    parser.add_argument("--db", default=DEFAULT_DB,
                        help=f"Path to zai_usage.db (default {DEFAULT_DB})")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Include detailed check reasons in output")
    parser.add_argument("--stdout", action="store_true",
                        help="Also print result to stdout")
    args = parser.parse_args()

    result = run_gate(db_path=args.db, task_duration=args.duration, verbose=args.verbose)

    write_state(result)

    if args.stdout or args.verbose:
        print(json.dumps(result, indent=2, default=str))

    # Human-readable summary to stderr
    status = "PAUSED" if result["paused"] else "CLEAR"
    print(f"[rate_limit_gate] {status} — {result['reason']}", file=sys.stderr)
    if result["resume_at"]:
        print(f"  resume_at: {result['resume_at']}", file=sys.stderr)

    # Exit code: 0 = clear, 1 = paused
    sys.exit(1 if result["paused"] else 0)


if __name__ == "__main__":
    main()
