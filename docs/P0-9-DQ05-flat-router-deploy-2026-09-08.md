# P0-9 — DQ05 Flat-Router Deploy + Observability (2026-09-08)

**Task:** `t_dbb677c3` (P0-9) — Infra hardening: fix the DQ05 zombie proxy and
make routing telemetry visible.
**Plan ref:** `PLAN-wire-full-provider-pool-rugpull-resilience-2026-09-08.md` P0-9.
**Status:** DEPLOYED + VERIFIED 2026-09-08.

## Problem (the zombie)

DQ05 (`c03rad0r-DQ05proplus`, always-on mini PC) ran a `zai-proxy` user service
whose code was 5,882 lines (T470: 7,941), with **no `~/merchant-routing-engine`
repo and no `~/.hermes/bot/flat_router.py`**. `zai_proxy.py` auto-discovers
`~/merchant-routing-engine` and imports `src.*` (shadow_hook, live_router,
dispatch_gate, real_price_tracker, balance_collectors, …). With the repo and
flat_router absent, every advanced feature failed **silently** and the proxy
degraded to a dumb key-rotation proxy: `/quota` returned `windows: [{name:
"unknown", used_pct: 0}]`, `/kalman-pricing` returned empty prices, predictions
reported numpy/data starvation. (Fully documented in `routstr-node-ops` skill →
`references/kalman-pricing-endpoint-and-dq05-zombie.md`.)

## Fix deployed

1. **merchant-routing-engine repo** synced T470 → DQ05 (tar stream over ssh;
   excludes `.git`, `demo/` 113M, `datasets/` 74M, `logs/`, `htmlcov/`).
   15M landed at `~/merchant-routing-engine` incl. local uncommitted
   worker-merchant artifacts (`src/routstr_delist.py`, `src/routstr_sold_canary.py`).
2. **Bot runtime files** synced: top-level `*.py`/`*.json`, `src/` (bot-local
   `ollama_quota_tracker.py` and mirrors), `scripts/` — plus `zai_proxy.py`
   (7,941 lines), `flat_router.py`, `garbage_detector.py`, `model_matrix.py`,
   `model_matrix.json`. DQ05-local `*.db` files were **preserved** (host-local
   telemetry history, incl. its own `zai_usage.db`).
3. **`.key_disabled_*` exclusion flags** (6 flags + `.meta` sidecars:
   openrouter, ollama_cloud 1–4, telnyx) synced so DQ05 enforces the same
   operator exclusion policy (P0-1).
4. **Manager `.env` merged** (not clobbered): T470 value wins on the 36 shared
   keys, 26 T470-only keys added, 2 DQ05-only keys preserved
   (`PPQ_API_KEY`, `ZAI_API_KEY`). Backup on DQ05:
   `~/.hermes/profiles/manager/.env.bak-p0-9-20260908T170922Z`. No secret
   printed or committed anywhere.
5. **Service python deps**: `numpy 2.5.3` + `websocket-client` installed into
   DQ05's `~/.hermes/hermes-agent/venv` (the ExecStart interpreter had neither;
   Kalman features were silently starved).
6. **systemd drop-ins** installed on DQ05 (canonical copies in this repo under
   `config/systemd/user/zai-proxy.service.d/`):
   - `env-features.conf` — mirrors T470's pricing/pressure feature env
     (quota-pressure, credit-pressure, per-model pricing, dynamic rates) and
     sets `ZAI_NODE_SOURCE=c03rad0r-DQ05proplus`.
   - `spend-cap.conf` — display caps `999999` + `SPEND_CAP_METERED=25` (matches
     T470's D6 drop-in).
7. **Code fix (this commit):** `zai_proxy.py` `_build_kalman_pricing_json()`
   hardcoded `"source": "T470"`. Now
   `os.environ.get("ZAI_NODE_SOURCE", "T470")` — default unchanged (backward
   compatible); hosts set the env var to attribute telemetry correctly. This is
   what makes DQ05's `/kalman-pricing` report `c03rad0r-DQ05proplus` instead of
   impersonating T470.

## Verification (all live, 2026-09-08)

- Import smoke test on DQ05 (new code, service venv): ShadowHook initialized,
  converged rates loaded, LiveRouter initialized, ppq/openrouter/telnyx/
  routstr/neuralwatt balance bridges loaded, ProfitTracker loaded,
  `IMPORT_OK` — zero `[...] DISABLED` lines.
- `systemctl --user is-active zai-proxy` → `active`.
- `GET /quota` → real windows: `5-hour used_pct 3`, `weekly used_pct 75
  (locked weekly threshold 60)`, real `resets_at` timestamps. Blind-quota
  symptom gone.
- `GET /kalman-pricing` → non-empty providers with real `quota_used_pct`,
  locked state, effective prices; `"source": "c03rad0r-DQ05proplus"`.
- **Live dispatch** through DQ05 `:9099` (glm-5.2, 10 max_tokens) → HTTP 200,
  `X-Provider: zai:ours`; `zai_usage.db` gained
  `api_calls id=16161 key_name=ours tier='flat_router' status 200` (first
  flat_router-tier row in DQ05's DB) and
  `flat_router_shadow_decisions id=1` showing the full cheapest-first candidate
  order (ours/friend/opencode_go $0.001 → ppq $0.80 → routstr $1.00 → …).
  **Routing telemetry is visible on DQ05.**

## routstr-readiness-check cron

`routstr-readiness-check.sh` (manager cron `810fa8d923a8`, no_agent) run
manually post-deploy: exit 0, silent (no READY/REGRESSION), state unchanged —
gates 2/3/4 PASS, gate1 `UNVERIFIED` (traffic idle → `insufficient_data`,
0 samples). The cron stays **green** (no regression). Gate1 only flips PASS
when `kalman_health.py` has ≥5 hourly burn buckets per key (ours/friend) with a
healthy/improving verdict — that is traffic/quota-bound on the host the cron
runs on (T470) and outside P0-9's deterministic control; it will open when the
z.ai quota lanes carry enough sustained traffic for the Kalman backtest to
score <15% MAPE.

## Residual notes / follow-ups

- DQ05 has **no `kalman_npub.nsec`** → its kind-30315 publisher prints
  "[nostr] No private key found — publisher disabled" once and stays off
  (graceful). Local telemetry (DB + `/kalman-pricing`) is fully visible; give
  DQ05 its own nsec before making it the published 30315 source. Do NOT copy
  T470's nsec (two nodes publishing one identity confuses the VPS2 hook).
- DQ05 is not yet in the live serving path (fleet + routstr tunnel still point
  at T470). This deploy makes DQ05 **capable** of serving identically; cut
  traffic over when the autossh-hardened tunnel or a redundancy test demands it.
- Re-deploy anytime: `bash ~/.hermes/bot/scripts/deploy-dq05-proxy.sh`
  (idempotent).
- Tree note: this repo's working tree carries pre-existing runtime-mutated /
  untracked files (`.key_disabled_*`, `peak_hours.json`, `zai_proxy_state.json`,
  `price_viz.py`, `session_archiver.py`, `src/tag-sidecar.py`,
  `test_user_agent_headers.py`, `handovers/INDEX.md`, deleted
  `.enable_live_routing`) that predate P0-9 and are not part of this commit.
