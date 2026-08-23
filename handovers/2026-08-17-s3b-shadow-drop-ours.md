# Handover: S3b — remove dead `zai_ours` from shadow optimizer tap (t_872743b5)

**Date**: 2026-08-17 · **Branch**: `wt/shadow-drop-ours` · **Task**: t_872743b5 (llm-routing board)

## What changed

`~/.hermes/bot/zai_proxy.py` — the Phase 2.1 shadow-decision tap (`_shadow_optimizer`,
~L1172): the `zai_ours` provider registration ($0.068/M seed) was REMOVED. The shadow
set now registers: `zai_friend`, `ollama_cloud`, `ppq_external`, `deepinfra`, `telnyx`.

Companion change in merchant-routing-engine (`wt/shadow-drop-ours`, commit d1d0334):
`src/shadow_hook.py` `_SEED_COSTS`/`_QUOTA_TOTALS` also dropped `ours`.

## Why

The 'ours' z.ai key was disabled 2026-08-15 (`.key_disabled_ours`) and permanently
retired per Felix (friend-only policy, never re-add). The shadow tap is NOT
health-gated, so the dead key kept winning the price-first comparison and generated
~4.8k disagreeing shadow decisions/24h in `zai_usage.db :: routing_shadow_decisions`
(reason='cheapest viable provider: zai_ours…', tokens=0) — pure analytics pollution.

## What was NOT touched (on purpose)

- Live key handling: `best_key()`/live router path, `config/providers.yaml`,
  `.key_disabled_ours` gating — already correct.
- `ShadowLogger` name normalization (`zai_ours` → `ours`) — needed for historical rows.

## Deploy / revert

- Deploy: `systemctl --user restart zai-proxy` (branch must be checked out — file on
  disk is the deployed artifact).
- Revert: `git checkout master -- zai_proxy.py && systemctl --user restart zai-proxy`.
- Live routing is untouched; worst case the tap logs nothing (import guard keeps
  routing 100% unchanged).

## Verification

- After restart, new rows in `routing_shadow_decisions` must have
  `shadow_provider != 'ours'`:
  `sqlite3 ~/.hermes/bot/zai_usage.db "SELECT shadow_provider, COUNT(*) FROM routing_shadow_decisions WHERE ts > strftime('%s','now')-900 GROUP BY 1"`
- MRE tests: `cd ~/merchant-routing-engine && python3 -m pytest tests/ -q`
  (1979 pass; 15 telnyx failures are pre-existing from aa1c14c, unrelated).
