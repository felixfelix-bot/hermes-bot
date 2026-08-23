#!/usr/bin/env python3
"""
z.ai Usage Report CLI -- query and export usage data from zai_usage.db.

The proxy (~/.hermes/bot/zai_proxy.py) writes two tables:
  api_calls     - one row per proxied request (tokens, key, model, hits, status)
  key_decisions - one row per key-selection decision (chosen key + reason + quotas)

The live proxy emits rich reason strings that embed quota percentages, e.g.
  prefer_ours_both_unlocked_ours_29_friend_13
  only_available_ours_locked_weekly_83pct
  fallback_both_locked_ours_weekly_100pct_friend_5-hour_100pct
This tool normalizes them into a small set of clean categories so the
"decisions" report is actually readable. Pass --raw-reasons to see the
unnormalized dump.

Subcommands:
  summary     Total tokens per model, per key + peak/off-peak split
  timeseries  Tokens per hour/day, grouped by model or key
  decisions   Key selection distribution, normalized reason categories,
              and average quota at decision time
  export      CSV or JSON export of api_calls for external plotting
  hit-rates   Cache / ollama / ppq hit rates + tier distribution

Usage:
  python3 zai_usage_report.py summary
  python3 zai_usage_report.py timeseries --interval day --group-by model
  python3 zai_usage_report.py decisions
  python3 zai_usage_report.py decisions --raw-reasons
  python3 zai_usage_report.py export --format csv --output usage.csv
  python3 zai_usage_report.py hit-rates --from 2026-07-04 --to 2026-07-04
  python3 zai_usage_report.py summary --db /tmp/test.db

All date filters are inclusive day ranges interpreted in UTC:
  --from 2026-06-27 --to 2026-06-28
"""

import argparse
import csv
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

DB_PATH = os.path.expanduser("~/.hermes/bot/zai_usage.db")

# Peak window (task spec: 06:00-10:00 UTC)
PEAK_START = 6
PEAK_END = 10


# ---------------------------------------------------------------------------
# Connection / query helpers
# ---------------------------------------------------------------------------
def get_connection(db_path):
    """Open a read-only URI connection. Never locks the live WAL database."""
    path = os.path.expanduser(db_path)
    if not os.path.exists(path):
        print(f"ERROR: database not found at {path}", file=sys.stderr)
        sys.exit(1)
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def build_where(args, extra=None):
    """Build a WHERE clause + params from --from / --to (inclusive UTC day range)."""
    parts = []
    params = []
    if getattr(args, "from_date", None):
        from_ts = datetime.strptime(args.from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        parts.append("ts >= ?")
        params.append(from_ts)
    if getattr(args, "to_date", None):
        to_ts = datetime.strptime(args.to_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        to_ts = to_ts.replace(hour=23, minute=59, second=59).timestamp()
        parts.append("ts <= ?")
        params.append(to_ts)
    if extra:
        # Parenthesize each extra so any OR/AND inside it stays scoped to
        # this filter and cannot escape the date range via SQL precedence.
        parts.extend(f"({e})" for e in extra)
    clause = ("WHERE " + " AND ".join(parts)) if parts else ""
    return clause, params


def fmt_table(rows, headers):
    """Pretty-print rows as an aligned table."""
    if not rows:
        print("  (no data)")
        return
    widths = [len(h) for h in headers]
    str_rows = []
    for row in rows:
        srow = [str(v) for v in row]
        for i, val in enumerate(srow):
            widths[i] = max(widths[i], len(val))
        str_rows.append(srow)
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*["-" * w for w in widths]))
    for srow in str_rows:
        print(fmt.format(*srow))


def fmt_int(n):
    """Thousands-separated integer, tolerant of None."""
    if n is None:
        return "0"
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


def count_range(cur, table, args, extra=None):
    """Total rows in `table` within the date range (before any column filter)."""
    where, params = build_where(args, extra)
    cur.execute(f"SELECT COUNT(*) FROM {table} {where}", params)
    return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Reason normalization
# ---------------------------------------------------------------------------
# The live proxy emits reasons like:
#   prefer_ours_both_unlocked_ours_29_friend_13
#   only_available_ours_locked_weekly_83pct
#   fallback_both_locked_ours_weekly_100pct_friend_5-hour_100pct
# We collapse these into a small readable taxonomy. The spec's three canonical
# reasons (lowest_quota / only_available / fallback) map in too.
REASON_CATEGORIES = [
    ("prefer_lower_quota", "Both unlocked, lower-quota key preferred"),
    ("only_friend_available", "Only friend available (ours locked/error)"),
    ("only_ours_available", "Only ours available (friend locked/error)"),
    ("fallback_both_locked", "Fallback - both locked/errored"),
    ("lowest_quota", "Lowest quota (legacy reason)"),
    ("default_preferred", "Default preference"),
    ("other", "Uncategorized"),
]


def normalize_reason(reason):
    """Collapse a raw proxy reason string into a clean category."""
    if not reason:
        return "other"
    r = reason.lower()
    # Both-unlocked, explicit preference by quota
    if r.startswith("prefer_ours_both_unlocked") or r.startswith("prefer_ours_"):
        return "prefer_lower_quota"
    if r.startswith("prefer_friend_both_unlocked") or r.startswith("prefer_friend_"):
        return "prefer_lower_quota"
    # Only-one-available families
    if r.startswith("only_available_ours_locked") or r in ("ours_blocked",):
        return "only_friend_available"
    if r.startswith("only_available_friend_locked") or r in ("friend_blocked",):
        return "only_ours_available"
    # Both locked / errored
    if r.startswith("fallback_both_locked") or r.startswith("fallback"):
        return "fallback_both_locked"
    # Quota-tiebreak variants seen in earlier prototypes
    if r == "lowest_quota":
        return "lowest_quota"
    if "ours_unlocked_higher_quota" in r or "friend_unlocked_higher_quota" in r:
        return "prefer_lower_quota"
    if r == "default_preferred":
        return "default_preferred"
    return "other"


def category_label(cat):
    for c, label in REASON_CATEGORIES:
        if c == cat:
            return label
    return cat


# ---------------------------------------------------------------------------
# Subcommand: summary
# ---------------------------------------------------------------------------
def cmd_summary(args):
    conn = get_connection(args.db)
    cur = conn.cursor()

    total_rows = count_range(cur, "api_calls", args)
    tokenless = count_range(cur, "api_calls", args, extra=["total_tokens IS NULL OR total_tokens = 0"])
    print(f"Range: {total_rows:,} calls  ({tokenless:,} tokenless 404/polling rows excluded from token sums)")

    # Tokens per model
    print("\n=== Tokens per Model ===")
    where, params = build_where(args, extra=["model IS NOT NULL", "total_tokens > 0"])
    q = f"""
        SELECT model, COUNT(*) as calls,
               SUM(prompt_tokens) as prompt,
               SUM(completion_tokens) as completion,
               SUM(total_tokens) as total,
               ROUND(AVG(total_tokens), 1) as avg_total
        FROM api_calls {where}
        GROUP BY model ORDER BY total DESC
    """
    cur.execute(q, params)
    rows = [(m, c, fmt_int(p), fmt_int(comp), fmt_int(t), a)
            for (m, c, p, comp, t, a) in cur.fetchall()]
    fmt_table(rows, ["Model", "Calls", "Prompt", "Completion", "Total", "Avg/Req"])

    # Tokens per key
    print("\n=== Tokens per Key ===")
    where, params = build_where(args, extra=["key_name IS NOT NULL", "total_tokens > 0"])
    q = f"""
        SELECT key_name, COUNT(*) as calls, SUM(total_tokens) as total,
               ROUND(AVG(total_tokens), 1) as avg_total
        FROM api_calls {where}
        GROUP BY key_name ORDER BY total DESC
    """
    cur.execute(q, params)
    rows = [(k, c, fmt_int(t), a) for (k, c, t, a) in cur.fetchall()]
    fmt_table(rows, ["Key", "Calls", "Total Tokens", "Avg/Req"])

    # Peak vs off-peak
    print(f"\n=== Peak vs Off-Peak ({PEAK_START:02d}:00-{PEAK_END:02d}:00 UTC) ===")
    where, params = build_where(args, extra=["total_tokens > 0"])
    q = f"""
        SELECT
            CASE
                WHEN CAST(strftime('%H', ts, 'unixepoch') AS INTEGER) >= {PEAK_START}
                 AND CAST(strftime('%H', ts, 'unixepoch') AS INTEGER) < {PEAK_END}
                THEN 'peak' ELSE 'off-peak'
            END as period,
            COUNT(*) as calls, SUM(total_tokens) as total
        FROM api_calls {where}
        GROUP BY period ORDER BY period
    """
    cur.execute(q, params)
    rows = [(p, c, fmt_int(t)) for (p, c, t) in cur.fetchall()]
    fmt_table(rows, ["Period", "Calls", "Total Tokens"])

    conn.close()


# ---------------------------------------------------------------------------
# Subcommand: timeseries
# ---------------------------------------------------------------------------
def cmd_timeseries(args):
    conn = get_connection(args.db)
    cur = conn.cursor()

    interval = args.interval
    group_by = args.group_by

    # Hour buckets include the date so multi-day queries don't conflate hours.
    if interval == "hour":
        strftime_fmt = "%Y-%m-%d %H:00"
    else:
        strftime_fmt = "%Y-%m-%d"
    group_col = "model" if group_by == "model" else "key_name"

    where, params = build_where(args, extra=["total_tokens > 0"])
    q = f"""
        SELECT
            strftime('{strftime_fmt}', ts, 'unixepoch') as time_bucket,
            {group_col} as grp,
            COUNT(*) as calls,
            SUM(total_tokens) as total
        FROM api_calls {where}
        GROUP BY time_bucket, grp
        ORDER BY time_bucket, grp
    """
    cur.execute(q, params)
    rows = cur.fetchall()

    print(f"=== Timeseries ({interval}, grouped by {group_by}) ===")
    headers = ["Time", group_by.title(), "Calls", "Total Tokens", "Peak?"]
    data = []
    for time_bucket, grp, calls, total in rows:
        is_peak = ""
        if interval == "hour":
            try:
                hr = int(time_bucket.split(" ")[1].split(":")[0])
                if PEAK_START <= hr < PEAK_END:
                    is_peak = "** PEAK"
            except (ValueError, IndexError):
                pass
        data.append((time_bucket, grp or "N/A", calls, fmt_int(total), is_peak))
    fmt_table(data, headers)

    conn.close()


# ---------------------------------------------------------------------------
# Subcommand: decisions
# ---------------------------------------------------------------------------
def cmd_decisions(args):
    conn = get_connection(args.db)
    cur = conn.cursor()

    total = count_range(cur, "key_decisions", args)
    if total == 0:
        print("No decision data in range.")
        conn.close()
        return

    print(f"=== Key Decisions ({total:,} total) ===")

    # Key choice distribution
    print("\n--- Key Selection Distribution ---")
    where, params = build_where(args)
    q = f"""
        SELECT chosen_key, COUNT(*) as count,
               ROUND(100.0 * COUNT(*) / {total}, 1) as pct
        FROM key_decisions {where}
        GROUP BY chosen_key ORDER BY count DESC
    """
    cur.execute(q, params)
    fmt_table(cur.fetchall(), ["Chosen Key", "Count", "%"])

    if args.raw_reasons:
        # Unnormalized dump (can be hundreds of rows; respect --limit)
        print("\n--- Raw Decision Reasons (unnormalized) ---")
        q = f"""
            SELECT reason, COUNT(*) as count,
                   ROUND(100.0 * COUNT(*) / {total}, 1) as pct
            FROM key_decisions {where}
            GROUP BY reason ORDER BY count DESC
            LIMIT ?
        """
        cur.execute(q, params + [args.limit])
        fmt_table(cur.fetchall(), ["Reason", "Count", "%"])
    else:
        # Normalized categories + chosen_key breakdown.
        # Group by (reason, chosen_key) at the SQL level (efficient), then
        # collapse into categories in Python.
        print("\n--- Decision Reason Categories (normalized) ---")
        q = f"""
            SELECT reason, chosen_key, COUNT(*) as count
            FROM key_decisions {where}
            GROUP BY reason, chosen_key
        """
        cur.execute(q, params)
        cat_totals = {}
        cat_key_counts = {}
        for reason, chosen, count in cur.fetchall():
            cat = normalize_reason(reason)
            cat_totals[cat] = cat_totals.get(cat, 0) + count
            cat_key_counts.setdefault(cat, {})
            cat_key_counts[cat][chosen] = cat_key_counts[cat].get(chosen, 0) + count

        rows = []
        for cat in sorted(cat_totals, key=lambda c: -cat_totals[c]):
            n = cat_totals[cat]
            pct = round(100.0 * n / total, 1)
            keys = ", ".join(f"{k}={v}" for k, v in
                             sorted(cat_key_counts[cat].items(), key=lambda kv: -kv[1]))
            rows.append((cat, category_label(cat), n, pct, keys))
        fmt_table(rows, ["Category", "Description", "Count", "%", "By Key"])

    # Average quota percentages at decision time
    print("\n--- Average Quota at Decision Time ---")
    where_q, params_q = build_where(
        args, extra=["(ours_pct IS NOT NULL OR friend_pct IS NOT NULL)"])
    q = f"""
        SELECT ROUND(AVG(ours_pct), 1), ROUND(AVG(friend_pct), 1),
               COUNT(*) as n
        FROM key_decisions {where_q}
    """
    cur.execute(q, params_q)
    row = cur.fetchone()
    if row and row[0] is not None:
        print(f"  ours:   {row[0]}%   friend: {row[1]}%   (over {row[2]:,} decisions)")

    conn.close()


# ---------------------------------------------------------------------------
# Subcommand: export
# ---------------------------------------------------------------------------
def cmd_export(args):
    conn = get_connection(args.db)
    cur = conn.cursor()

    where, params = build_where(args)
    q = f"""
        SELECT
            id,
            datetime(ts, 'unixepoch') as datetime_utc,
            ts as ts_epoch,
            key_name, key_suffix, model, tier,
            prompt_tokens, completion_tokens, total_tokens,
            cache_hit, ollama_hit, ppq_hit,
            status_code, error, duration_ms
        FROM api_calls {where}
        ORDER BY ts
    """
    cur.execute(q, params)
    rows = cur.fetchall()
    headers = [d[0] for d in cur.description]

    fmt = args.format
    default_ext = "csv" if fmt == "csv" else "json"
    output = args.output or f"zai_usage_export.{default_ext}"

    if fmt == "csv":
        with open(output, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            writer.writerows(rows)
        print(f"Exported {len(rows):,} rows to {output} (CSV)")
    else:
        records = [dict(zip(headers, row)) for row in rows]
        with open(output, "w") as f:
            json.dump(records, f, indent=2, default=str)
        print(f"Exported {len(rows):,} rows to {output} (JSON)")

    conn.close()


# ---------------------------------------------------------------------------
# Subcommand: hit-rates
# ---------------------------------------------------------------------------
def cmd_hit_rates(args):
    conn = get_connection(args.db)
    cur = conn.cursor()

    total = count_range(cur, "api_calls", args)
    if total == 0:
        print("No data in range.")
        conn.close()
        return

    print(f"=== Hit Rates (total calls: {total:,}) ===\n")

    for hit_type, label in [("cache_hit", "Cache"),
                            ("ollama_hit", "Ollama cascade"),
                            ("ppq_hit", "PPQ fallback")]:
        w2, p2 = build_where(args, extra=[f"{hit_type} = 1"])
        cur.execute(f"SELECT COUNT(*) FROM api_calls {w2}", p2)
        hits = cur.fetchone()[0]
        pct = round(100.0 * hits / total, 1) if total else 0.0
        bar = "#" * int(pct / 5) + "." * (20 - int(pct / 5))
        print(f"  {label:16s} [{bar}] {pct:5.1f}%  ({hits:,}/{total:,})")

    # Tier distribution
    print("\n=== Tier Distribution ===")
    where2, params2 = build_where(args, extra=["tier IS NOT NULL"])
    q = f"""
        SELECT tier, COUNT(*) as count,
               ROUND(100.0 * COUNT(*) / {total}, 1) as pct
        FROM api_calls {where2}
        GROUP BY tier ORDER BY count DESC
    """
    cur.execute(q, params2)
    fmt_table(cur.fetchall(), ["Tier", "Count", "%"])

    # Status code distribution (useful for spotting error storms)
    print("\n=== Status Code Distribution ===")
    where3, params3 = build_where(args, extra=["status_code IS NOT NULL"])
    q = f"""
        SELECT status_code, COUNT(*) as count,
               ROUND(100.0 * COUNT(*) / {total}, 1) as pct
        FROM api_calls {where3}
        GROUP BY status_code ORDER BY count DESC
    """
    cur.execute(q, params3)
    fmt_table(cur.fetchall(), ["Status", "Count", "%"])

    conn.close()


# ---------------------------------------------------------------------------
# Main / argparse
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="z.ai usage report and CSV export tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p):
        p.add_argument("--from", dest="from_date", help="Start date (YYYY-MM-DD), UTC, inclusive")
        p.add_argument("--to", dest="to_date", help="End date (YYYY-MM-DD), UTC, inclusive")
        p.add_argument("--db", default=DB_PATH, help=f"Database path (default: {DB_PATH})")

    p_summary = sub.add_parser("summary", help="Total tokens per model, per key + peak/off-peak")
    add_common(p_summary)
    p_summary.set_defaults(func=cmd_summary)

    p_ts = sub.add_parser("timeseries", help="Tokens per hour/day, grouped by model or key")
    p_ts.add_argument("--interval", choices=["hour", "day"], default="hour")
    p_ts.add_argument("--group-by", choices=["model", "key"], default="model")
    add_common(p_ts)
    p_ts.set_defaults(func=cmd_timeseries)

    p_dec = sub.add_parser("decisions", help="Key selection distribution + normalized reason categories")
    p_dec.add_argument("--raw-reasons", action="store_true",
                       help="Show unnormalized raw reason strings (verbose)")
    p_dec.add_argument("--limit", type=int, default=50,
                       help="Max rows for --raw-reasons dump (default 50)")
    add_common(p_dec)
    p_dec.set_defaults(func=cmd_decisions)

    p_exp = sub.add_parser("export", help="Export api_calls to CSV or JSON")
    p_exp.add_argument("--format", choices=["csv", "json"], default="csv")
    p_exp.add_argument("--output", "-o", help="Output filename (default: zai_usage_export.<ext>)")
    add_common(p_exp)
    p_exp.set_defaults(func=cmd_export)

    p_hr = sub.add_parser("hit-rates", help="Cache/ollama/ppq hit rates + tier + status distribution")
    add_common(p_hr)
    p_hr.set_defaults(func=cmd_hit_rates)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
