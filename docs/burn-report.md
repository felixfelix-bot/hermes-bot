# API Burn Report — Methodology

> Canonical methodology for the periodic API burn report (48h rolling
> window). Introduced R6 (task t_b36cb39d, 2026-08-15) which made ≥80% of
> burn attributable by board/task/worker; measured coverage after R6:
> **99.9% of tokens** in the 48h window.

## Data sources

| Source | Table | What it gives | Notes |
|---|---|---|---|
| Local proxy log | `zai_usage.db → api_calls` | one row per proxied call: ts, key, model, prompt/completion/total tokens, tier, cost_usd, session_id | authoritative timestamps; `session_id` only populated from commit f509758 onward (0.4% historical) |
| PPQ queries API | `api_burn.db → ppq_queries` | authoritative PPQ cost + token counts per query | backfilled ~11 days of reachable history (pagination limit); live rows added by `api_burn_collector.py` via `ppq_common.record_ppq_queries` |
| Provider balances | `api_burn.db → balance_snapshots` | $-burn per provider over time | drives the 6h rolling analyzer (`api_burn_analyzer.py`) |

## Attribution methodology (R6)

`burn_attribution.py` attributes every `api_calls` row in the window to
kanban tasks / chat sessions via **timestamp-overlap** (the proxy did not
record session_id historically, so overlap is the only sound heuristic):

1. Load `task_runs` from every board DB whose `[start, end]` interval
   overlaps the window. Open runs are clamped:
   `end = min(last_heartbeat + grace, now)`.
2. Load per-profile sessions + message activity from
   `~/.hermes/profiles/*/state.db`.
3. For each call, find candidates active at call-ts:
   runs covering ts; sessions active at ts with ≥1 message within ±90s
   (sessions only considered for profiles with **no** active run —
   idle sessions never absorb burn).
4. Split the call's tokens/cost across candidates:
   one candidate → full share (`unique_run`); several → proportional to
   message counts (`weighted`) or equal split when all weights are zero
   (`overlap_equal`).
5. Calls with no candidates land in the `unattributed` sink (share=1.0).

Honesty guarantees: shares per call sum to exactly 1.0; sink rows are
explicit; no retroactive guessing beyond the window. Output:
`burn_attribution.db → attribution`, one row per (call × candidate).

### Current coverage (48h window ending 2026-08-15 ~21:45 UTC)

- 26,053 calls · 896.7M tokens · **99.9% of tokens attributed**
  (29 unattributed calls)
- Methods: 24,237 weighted · 1,446 overlap_equal · 341 unique_run · 29 unattributed
- Top consumers are kanban tasks (board/task_id/profile) and manager
  chat sessions — see `burn_attribution.py --top` for the live ranking.

## Backfills performed (R6)

- **ppq_queries**: 714 rows backfilled from the PPQ `/queries/history`
  API (~11-day reachable window; all rows have tokens + cost_usd).
  Proxy-log source (`--from-proxy`) contributed 0 additional rows —
  historical `api_calls` rows predate the `ppq_hit` tag, so API history
  is the only viable historical source. Idempotent: UNIQUE dedup index
  on (ts±2s, model, total_tokens); safe to re-run.
- **messages.token_count**: `token_backfill.py` stamps the first
  following NULL assistant message in the attributed session with the
  call's completion_tokens (match window 120s, never overwrites
  non-NULL, skips messages <5 min old to avoid racing live agents).
  3,128 messages stamped in the 48h window (~18% of targets; the rest
  have no attributable call within the match window).

## Known limits

- Historical `session_id` on `api_calls` is absent (0.4% populated);
  commit f509758 populates it going forward, so attribution precision
  (unique_run vs weighted) improves over time.
- PPQ history beyond ~11 days is unreachable via API pagination.
- `weighted` splits are proportional to message activity, not measured
  per-request token usage — individual task shares within an overlap
  are estimates; the *total* is exact.

## Reproducing the report

```bash
cd ~/.hermes/bot
python3 ppq_backfill.py --from-api --from-proxy   # idempotent
python3 burn_attribution.py --since 48h --top 20
python3 token_backfill.py --since 48h             # --apply to write
```
