# Nsite Registry & Resilience Plan

**Created:** 2026-07-04
**Goal:** Never lose track of an nsite, never lose datapoints, full disaster recovery

---

## Current State — Complete Nsite Inventory

### 1. kalman-data (WORKING ✅)
- **URL:** `npub19y5kzwx985em9lc92xvdhr7d3qepftq5rv4q53rnljr4zzcwaswsnjalwn.nsite.lol`
- **npub hex:** `29296138c53d33b2ff055198db8fcd883214ac141b2a0a4473fc87510b0eec1d`
- **nsec location:** Hardcoded in `kalman-dashboard-deploy.sh` and `kalman_telemetry_publisher.py`
- **Data source:** `~/.hermes/bot/zai_usage.db` → `kalman_samples` table (5517 rows, 19MB DB)
- **Deploy script:** `~/.hermes/profiles/manager/scripts/kalman-dashboard-deploy.sh`
- **Cron:** Every 10 min, rebuilds data.json from SQLite, deploys to nsite
- **Frontend:** Fetches `data.json` on load, auto-refreshes every 2 min
- **GitHub:** `github.com/c03rad0r/kalman-data-dashboard` (main branch)
- **Status:** Just fixed. All 5517 samples served. No datapoint loss.

### 2. worker-dashboard (WORKING ✅)
- **URL:** `npub1f4d0pqxqx...nsite.lol` (hex: `f6bde015400239a...`)
- **nsec location:** `~/.hermes/state/worker-dashboard-nsec.key`
- **Data source:** `~/.hermes/bot/worker_metrics.db` → `worker_metrics` table (1119 rows, 96KB)
- **Deploy script:** `~/.hermes/profiles/manager/scripts/worker-dashboard-deploy.sh`
- **Cron:** Every 30 min, regenerates Plotly HTML, deploys to nsite
- **Frontend:** BAKED-IN snapshot (5MB self-contained HTML). All data is in the HTML file itself.
- **GitHub:** Pushed to ngit (`npub12m5exm...`)
- **Status:** Working but has the same "snapshot" architecture — 1119 points baked in at build time. No datapoint loss because the entire DB is small enough to embed.

### 3. community-sentiment (STALE ⚠️)
- **URL:** `npub10e6sy7z7jmgwd4l77f6r44q6uscczmsql5rfnkxgnfrkle6d2fgqxfkwya` (ngit only, NOT nsite.lol)
- **nsec location:** UNKNOWN — no key file found
- **Data source:** Unknown (no build script found)
- **Deploy script:** NONE
- **Cron:** NONE
- **Content:** Static 24KB index.html, last modified Jul 2
- **Status:** Orphaned. No way to rebuild or redeploy.

### 4. kanban-dashboard (EMPTY 💀)
- **Directory exists** at `~/nsites/kanban-dashboard/` but is completely empty (no files at all)
- **Status:** Dead. Never deployed.

---

## Data Sources Summary

| Database | Size | Tables | Feeds | Rows |
|---|---|---|---|---|
| `zai_usage.db` | 19MB | kalman_samples, system_readings, anomaly_events, key_decisions, api_calls, task_duration_samples, rate_limit_samples | kalman-data nsite | 5517 (kalman) |
| `worker_metrics.db` | 96KB | worker_metrics | worker-dashboard nsite | 1119 |
| `api_burn.db` | 244KB | balance_snapshots | nothing | ? |
| `code_index.db` | 424KB | chunks, meta | nothing | ? |
| `model_cache.db` | 324KB | prices, benchmarks | nothing | ? |

**Answer: YES, all datapoints are in databases.** The problem was never data loss — it was the frontend not loading them. The kalman-data nsite now fetches all rows from `zai_usage.db` via `data.json`.

---

## The Problem

We have **no central registry** for nsites. Keys are scattered across:
- `~/.hermes/state/worker-dashboard-nsec.key` (1 key)
- Hardcoded in `kalman-dashboard-deploy.sh` (1 key)
- Hardcoded in `kalman_telemetry_publisher.py` (1 key, same as deploy script)
- Unknown for community-sentiment

If any of these files is lost, the nsite becomes an orphan — we can see it but can't update it.

---

## Plan — 4 Phases

### Phase 1: Central Registry (IMMEDIATE)

Create `~/nsites/REGISTRY.yaml` — single source of truth for all nsites:

```yaml
nsites:
  kalman-data:
    npub_hex: "29296138c53d33b2ff055198db8fcd883214ac141b2a0a4473fc87510b0eec1d"
    npub_bech32: "npub19y5kzwx985em9lc92xvdhr7d3qepftq5rv4q53rnljr4zzcwaswsnjalwn"
    gateway_url: "https://npub19y5kzwx985em9lc92xvdhr7d3qepftq5rv4q53rnljr4zzcwaswsnjalwn.nsite.lol/"
    nsec_file: "~/.hermes/state/kalman-data-nsec.key"
    data_source: "~/.hermes/bot/zai_usage.db"
    data_table: "kalman_samples"
    deploy_script: "~/.hermes/profiles/manager/scripts/kalman-dashboard-deploy.sh"
    github: "github.com/c03rad0r/kalman-data-dashboard"
    cron: "every 10m"
    status: "active"
    
  worker-dashboard:
    npub_hex: "f6bde015400239a0053812b8c7bbfd0133d84d67bb062d98981ea2e102a1c92a"
    nsec_file: "~/.hermes/state/worker-dashboard-nsec.key"
    data_source: "~/.hermes/bot/worker_metrics.db"
    data_table: "worker_metrics"
    deploy_script: "~/.hermes/profiles/manager/scripts/worker-dashboard-deploy.sh"
    cron: "every 30m"
    status: "active"
    
  community-sentiment:
    npub_bech32: "npub10e6sy7z7jmgwd4l77f6r44q6uscczmsql5rfnkxgnfrkle6d2fgqxfkwya"
    gateway: "ngit only (NOT nsite.lol)"
    nsec_file: "UNKNOWN — needs recovery"
    status: "orphaned"
    action: "investigate or decommission"

  kanban-dashboard:
    status: "empty — never deployed"
    action: "remove directory or implement"
```

### Phase 2: Key Consolidation (IMMEDIATE)

1. Move ALL nsec keys to `~/.hermes/state/<name>-nsec.key` format
2. Remove hardcoded nsec strings from scripts — read from key files instead
3. Store keys (encrypted) in the KeePass database alongside other secrets
4. Add key file paths to REGISTRY.yaml

### Phase 3: Data-Serving Architecture (STANDARDIZE)

Apply the kalman-data fix to ALL dashboards:

- Each nsite serves `data.json` alongside `index.html`
- Frontend fetches `data.json` on load (same-origin, no CORS)
- Cron rebuilds `data.json` from the database every N minutes
- No reliance on WebSocket, ContextVM, or browser cache for initial render
- The `data.json` IS the server-side cache the user asked about

For the worker-dashboard specifically: migrate from the 5MB baked-in HTML to a data.json architecture (same as kalman-data). This will reduce deploy size from 5MB to ~7KB HTML + ~100KB JSON.

### Phase 4: Health Monitoring (WEEKLY)

Create a cron job (every 1h, silent on success) that:
1. Fetches each nsite's `data.json` and checks `generated_at` timestamp
2. Checks that the age is within 2x the expected refresh interval
3. Verifies the nsec key file exists and is readable
4. Reports only on anomalies (stale data, missing keys, broken deployments)

---

## Why NOT ContextVM as data server?

ContextVM is an MCP server running on DQ05 (LAN-only, `192.168.1.218`). It:
- Is NOT publicly accessible as an HTTP endpoint
- Requires MCP protocol, not HTTP/REST
- Would need a tunnel (Netbird/VPN) to be reachable from a browser
- Would be a single point of failure for the dashboard

The `data.json` approach is better:
- Same-origin as the dashboard (no CORS, no tunnel)
- Hosted on 5 Blossom CDNs (redundant)
- Rebuilt every 10 min from the SQLite database (which is the actual source of truth)
- The SQLite DBs are backed up via the `hermes-bot` repo replication rule
