#!/usr/bin/env python3
"""backfill_null_costs.py — Backfill NULL cost_usd rows in api_calls.

Finds all api_calls rows with NULL cost_usd, computes an estimated cost
using _estimate_cost_usd() (same logic as the _log_api_call safety net),
and updates the rows in-place.

Usage:
    python3 scripts/backfill_null_costs.py [--dry-run]

Reports:
    - Number of rows backfilled per provider
    - Total estimated cost recovered
    - Rows that could not be backfilled (unknown provider / zero rate)
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

# Ensure we can import zai_proxy from the bot directory
BOT_DIR = os.path.expanduser("~/.hermes/bot")
sys.path.insert(0, BOT_DIR)

import zai_proxy as z  # noqa: E402

DB_PATH = os.path.join(BOT_DIR, "zai_usage.db")


def backfill(dry_run: bool = False) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Find all NULL cost_usd rows
    c.execute("""
        SELECT id, key_name, model, total_tokens, prompt_tokens, completion_tokens
        FROM api_calls
        WHERE cost_usd IS NULL
        ORDER BY key_name
    """)
    rows = c.fetchall()

    if not rows:
        print("No NULL cost_usd rows found — nothing to backfill.")
        conn.close()
        return

    print(f"Found {len(rows)} rows with NULL cost_usd\n")

    # Group by provider for reporting
    stats: dict[str, dict] = {}
    unfixable: list[dict] = []
    updates: list[tuple[float, str, int]] = []  # (cost_usd, cost_source, id)

    for row in rows:
        key_name = row["key_name"]
        model = row["model"]
        total_tokens = row["total_tokens"] or 0
        prompt_tokens = row["prompt_tokens"]
        completion_tokens = row["completion_tokens"]
        row_id = row["id"]

        if key_name not in stats:
            stats[key_name] = {"count": 0, "tokens": 0, "cost": 0.0, "fixed": 0}

        stats[key_name]["count"] += 1
        stats[key_name]["tokens"] += total_tokens

        # Compute estimated cost using the same logic as _log_api_call safety net
        try:
            est_cost = z._estimate_cost_usd(
                key_name, total_tokens,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
        except Exception as e:
            est_cost = None
            unfixable.append({"id": row_id, "key_name": key_name, "reason": f"estimate error: {e}"})

        if est_cost is not None and est_cost != float("inf"):
            cost = float(est_cost)
            # Guard against negative cost (shouldn't happen, but be safe)
            if cost < 0:
                cost = 0.0
            updates.append((cost, "estimated", row_id))
            stats[key_name]["cost"] += cost
            stats[key_name]["fixed"] += 1
        else:
            unfixable.append({
                "id": row_id,
                "key_name": key_name,
                "reason": f"estimate returned {est_cost} (None or inf)",
            })

    # Print report
    print("=" * 70)
    print(f"{'Provider':<20} {'Rows':>6} {'Fixed':>6} {'Tokens':>14} {'Est Cost':>12}")
    print("=" * 70)

    total_fixed = 0
    total_cost = 0.0
    total_tokens = 0

    for provider in sorted(stats.keys()):
        s = stats[provider]
        print(f"{provider:<20} {s['count']:>6} {s['fixed']:>6} "
              f"{s['tokens']:>14,} ${s['cost']:>10.4f}")
        total_fixed += s["fixed"]
        total_cost += s["cost"]
        total_tokens += s["tokens"]

    print("=" * 70)
    print(f"{'TOTAL':<20} {len(rows):>6} {total_fixed:>6} "
          f"{total_tokens:>14,} ${total_cost:>10.4f}")
    print()

    if unfixable:
        print(f"Unfixable rows: {len(unfixable)}")
        for u in unfixable[:10]:
            print(f"  id={u['id']} provider={u['key_name']} reason={u['reason']}")
        if len(unfixable) > 10:
            print(f"  ... and {len(unfixable) - 10} more")
        print()

    if dry_run:
        print("[DRY RUN] No rows were updated.")
        conn.close()
        return

    # Batch update
    if updates:
        print(f"Updating {len(updates)} rows...")
        c.executemany(
            "UPDATE api_calls SET cost_usd = ?, cost_source = ? WHERE id = ?",
            updates,
        )
        conn.commit()
        print(f"Updated {c.rowcount} rows successfully.")
    else:
        print("No rows to update.")

    conn.close()


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print("[DRY RUN MODE — no changes will be made]\n")
    backfill(dry_run=dry_run)