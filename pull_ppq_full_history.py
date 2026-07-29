#!/usr/bin/env python3
"""
pull_ppq_full_history.py — Fetch PPQ history and insert into zai_usage.db.
Handles pagination, currency conversion, and deduplication.
"""

import json
import os
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

# ── Config ────────────────────────────────────────────────────────────────
DB = Path.home() / ".hermes" / "bot" / "zai_usage.db"
ENV_FILE = Path.home() / ".hermes" / ".env"
HISTORY_URL = "https://api.ppq.ai/queries/history"
BTC_URL = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
PAGE_COUNT = 100


def load_ppq_key():
    """Load PPQ_API_KEY from environment (not file)."""
    key = os.environ.get("PPQ_API_KEY", "").strip()
    if key and len(key) > 20:  # Valid API key
        return key
    # Fallback: read from file (with warning)
    if ENV_FILE.exists():
        with open(ENV_FILE) as f:
            for line in f:
                if line.startswith("PPQ_API_KEY="):
                    file_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if len(file_key) > 20:
                        print("WARNING: Using file key — set PPQ_API_KEY in env", file=sys.stderr)
                        return file_key
    raise RuntimeError("No valid PPQ_API_KEY found")


def fetch_btc_price():
    """Fetch current BTC price with caching."""
    cache_file = Path.home() / ".cache" / "btc_price.json"
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text())
            if time.time() - cached.get("fetched_at", 0) < 300:  # 5 min cache
                return cached["usd"]
        except:
            pass

    try:
        req = urllib.request.Request(BTC_URL)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        btc_usd = data["bitcoin"]["usd"]
        
        # Cache it
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({
            "usd": btc_usd,
            "fetched_at": time.time()
        }))
        return btc_usd
    except Exception as e:
        print(f"ERROR: BTC price fetch failed: {e}", file=sys.stderr)
        return 63200  # Fallback


def fetch_page(page):
    """Fetch a single page of PPQ history."""
    url = f"{HISTORY_URL}?page={page}&page_count={PAGE_COUNT}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {load_ppq_key()}")
    
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return data
    except Exception as e:
        print(f"ERROR: Page {page} failed: {e}", file=sys.stderr)
        return None


def main():
    days = 7  # Last 7 days
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    
    all_queries = []
    page = 1
    
    while True:
        if page % 5 == 0:
            print(f"Fetching page {page}...", flush=True)
            
        data = fetch_page(page)
        if not data or data.get("status") != "success":
            print(f"Stopped at page {page}: no success response", flush=True)
            break
            
        queries = data.get("data", [])
        if not queries:
            print(f"Stopped at page {page}: no data", flush=True)
            break
            
        # Filter by cutoff date
        recent = []
        for q in queries:
            try:
                ts = datetime.fromisoformat(q["timestamp"].replace("Z", "+00:00"))
                if ts >= cutoff:
                    recent.append(q)
            except Exception:
                continue
                
        all_queries.extend(recent)
        if len(recent) < len(queries) * 0.1:  # Less than 10% recent queries
            print(f"Stopped at page {page}: reached old data", flush=True)
            break
            
        page += 1

    print(f"\nCollected {len(all_queries)} queries since {cutoff.date()}", flush=True)
    
    # Calculate totals
    total_usd = sum(q.get("price_in_usd", 0) or 0 for q in all_queries)
    total_tokens = sum((q.get("input_count", 0) or 0) + (q.get("output_count", 0) or 0) 
                       for q in all_queries)
    
    btc_usd = fetch_btc_price()
    total_sats = (total_usd / btc_usd) * 100_000_000
    
    print(f"USD: ${total_usd:.4f}", flush=True)
    print(f"Tokens: {total_tokens:,}", flush=True)
    print(f"SATs: {total_sats:.0f} (BTC: ${btc_usd})", flush=True)
    if total_tokens > 0:
        sats_per_mtok = (total_sats / total_tokens) * 1_000_000
        print(f"Price: {sats_per_mtok:.2f} sats/M tokens", flush=True)
    
    # Insert into database
    db = sqlite3.connect(str(DB))
    inserted = 0
    
    for q in all_queries:
        try:
            ts = datetime.fromisoformat(q["timestamp"].replace("Z", "+00:00")).timestamp()
        except:
            continue
            
        total_tokens = (q.get("input_count", 0) or 0) + (q.get("output_count", 0) or 0)
        if total_tokens == 0:
            continue
            
        # Check for duplicates
        existing = db.execute(
            "SELECT id FROM api_calls WHERE ABS(ts - ?) < 5 AND ppq_hit = 1 AND total_tokens = ?",
            (ts, total_tokens)
        ).fetchone()
        if existing:
            continue
            
        db.execute("""
            INSERT INTO api_calls 
            (ts, key_name, model, prompt_tokens, completion_tokens, 
             total_tokens, status_code, ppq_hit, duration_ms)
            VALUES (?, 'ppq', ?, ?, ?, ?, 200, 1, 0)
        """, (
            ts, q.get("model", "unknown"), 
            q.get("input_count", 0) or 0,
            q.get("output_count", 0) or 0,
            total_tokens
        ))
        inserted += 1
    
    db.commit()
    db.close()
    
    print(f"Inserted {inserted} new PPQ rows", flush=True)
    
    # Save summary
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days": days,
        "query_count": len(all_queries),
        "total_usd": total_usd,
        "total_sats": total_sats,
        "total_tokens": total_tokens,
        "btc_usd": btc_usd,
        "sats_per_mtoken": (total_sats / total_tokens) * 1_000_000 if total_tokens else 0
    }
    with open(Path.home() / ".hermes" / "bot" / "ppq_history_summary.json", "w") as f:
        json.dump(summary, f, indent=2)
    
    print(f"\nSummary saved to ppq_history_summary.json", flush=True)


if __name__ == "__main__":
    main()
