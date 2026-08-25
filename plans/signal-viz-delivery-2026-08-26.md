# Plan: Signal Visualization Delivery & Surfacing

**Date:** 2026-08-26
**Status:** DONE
**Channel:** hermes-admin-setup group (`V8tnIinI5Yh6wAqXj2vGa0PfJ27j6zHLgpeZJexODEA=`)
**Triggers:** regime-shift alert, daily digest @ 09:00 IST, on-demand `/plot` skill

## Checklist

### Phase 0 — Send plots now for immediate feedback
- [x] Create `~/.hermes/bot/scripts/send-viz-signal.sh` helper (curl JSON-RPC send with attachments)
- [x] Send the 4 PNGs + ASCII caption to hermes-admin-setup group (ts=1787695865524)
- [x] Verify delivery (OK sent via signal-cli)

### Phase 1 — Wire surfacing triggers per user decisions

#### P1a — Regime-shift alert hook
- [x] In `cost-escalation-check.py` Section 14, when a regime alert fires, also attach the relevant plot via `send-viz-signal.sh`
- [x] Both up (quota→metered) and down (metered→quota) shifts spawn `send-viz-signal.sh --plot price-envelope`
- [x] Respects hysteresis (4h cooldown) + Kalman gates — no spam

#### P1b — Daily digest
- [x] Add system crontab entry: `30 3 * * * send-viz-signal.sh --digest` (09:00 IST)
- [x] Daily digest sends envelope + quota heatmap + ascii summary text

#### P1c — On-demand `/plot` command
- [x] Discovered bot command router: Hermes slash-command registry (`hermes_cli/commands.py`) + skills (`/skill-name` resolves via `scan_skill_commands()`)
- [x] Created `~/.hermes/profiles/manager/skills/devops/plot/SKILL.md` — `/plot [name]` invokes skill, agent runs shell helper via terminal toolset

### Phase 2 — Fix quota-heatmap data source
- [x] Verified: `load_quota_series` previously read only `provider_balances` table (which has zero rows for ours/friend/ollama_cloud/ollama_cloud_2/opencode_go)
- [x] Added synthetic time-series from `api_calls` token sums for quota-tier providers (hourly buckets over 48h)
- [x] Calibrated limits (ours/friend 2M/14M, ollama_cloud/* 500M/3.5B per ollama_quota_tracker)
- [x] Verified render shows missing rows: ours 100%, friend 83.1%, ollama_cloud 38.7%, ollama_cloud_2 6.1%, opencode_go 2.8% (matches ASCII table)

### Phase 3 — Envelope dot price inconsistency
- [x] Root cause: envelope dot falls back to `SEED_RATES["ours"]=0.068` when theoretical curve hits infinity at 100% quota (cur_pps returns `float('inf')`)
- [x] ASCII uses asymptote-capped value (`MIN_EFFECTIVE_PRICE * 2.5 = $0.0025`) — different ceiling
- [x] Fixed: dot picker now caps quota-tier providers at `MIN_EFFECTIVE_PRICE * QUOTA_PRESSURE_ASYMPTOTE` ($0.0015) instead of falling back to SEED_RATES — consistent with curve asymptote
- [x] Per-token providers keep SEED_RATES fallback (their curves are flat at the SEED price — correct)

## Rollback
- Remove crontab entry for daily digest (`crontab -e` → delete line)
- Revert `cost-escalation-check.py` Section 14 attachment hook
- Delete `~/.hermes/profiles/manager/skills/devops/plot/SKILL.md`
- `send-viz-signal.sh` self-contained — deletion disables all triggers
