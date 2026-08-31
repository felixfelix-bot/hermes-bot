# ADR-014: Resource-Savings Strategies — Tiered Triage, Prefill Tax, and Cache-Hit Economics

## Status

Accepted

## Date

2026-08-31

## Related

- `zai_proxy.py` (flat router, daemon :9099)
- `zai_usage.db` table `api_calls` (usage log; no cached-token split as of this ADR)
- ADR-002 (5-tier provider pricing model)
- ADR-004 (NeuralWatt two-phase state machine)
- ADR-011 (per-model pressure pricing)
- Kanban board `cost-reduction-sprint` (implementation tasks)
- 30-day cost audit (2026-08-31): ~$642.89 logged / 6.42B tokens / 154k calls, of which the largest share is NeuralWatt L2-estimated compute

## Context

A 30-day cost audit found four money leaks with a common root: **prefill is the tax**. Every
API call pays for the prompt prefix it sends. Long, churned prefixes mean paying full prefill
on every call — on flat-rate providers this is invisible, on compute-priced providers
(NeuralWatt) it is real kWh, and on providers with prompt caching (DeepSeek, z.ai) it
forfeits the ~10x cached-prefix discount.

Measured symptoms (audit, 2026-08-31):

1. Three high-frequency cron jobs ran on `glm-5.2` ($0.126/M tok) instead of
   `glm-5.3` ($0.077/M tok, −39%/call): 96 runs/day total across
   `net4sats-review-watchdog`, `plebeian-pr-review-autodispatch`,
   `opentollgate-pr-review-e2e`.
2. Runaway sessions on NeuralWatt: avg 76k tokens/call; one session burned
   4.6M tokens in 5 minutes; the 08-23 spike cost ~$293. Our DB overcounts NW
   spend 5.7x vs their authoritative `/v1/quota` — internal numbers are
   estimates, not cash.
3. An unmonitored key (`opencode_go`) burned $42 in 48h undetected.
4. 37,461 empty no-op calls (25% of `api_calls` rows) on the flat-rate keys —
   quota-window pressure and per-call overhead with zero value.
5. **Prefix-churn bug in our own harness**: context assembly places volatile
   state (dates, memory percentages, quota numbers) near the TOP of the system
   prompt. The prefix changes every turn, provider-side prompt caches never
   hit, and we pay full prefill every call.
6. **Instrumentation gap**: `zai_usage.db` logs no cached-token split, so cache
   hit rate is unmeasurable today.

## Decision

Adopt four strategies. All are alert-only — no caps, no model restrictions
(free-market price discovery via the live router stays; abnormal burn surfaces
as alerts, not blocks).

### 1. Tiered triage-then-escalate (review + cron lanes)

Cheap model (`deepseek-v4-flash`) triages every diff/PR/cron tick; only
low-confidence verdicts or large diffs escalate to the expensive tier
(`glm-5.3`). Applied first to the 3 glm-5.2 cron jobs (now glm-5.3 + watchdog
halved 30m→60m). Est. $25–60/mo saved. Merges with the existing tiered-review
doctrine (cross-family reviewers, two-stage review).

### 2. Kill the prefill tax (stable-prefix ordering + runaway detection)

Two fixes, both landed as tasks on `cost-reduction-sprint`:

- **Stable-first prefix ordering** in the agent's context assembly: stable
  content (identity, skills, memory, instructions) FIRST; volatile state
  (dates, memory %, quota) LAST in a small delimited block. Consecutive turns
  must share the longest possible identical prefix so provider prompt caches
  hit. Within-conversation byte-stability is preserved (per-conversation
  caching is sacred).
- **Runaway-session watchdog** on DQ05 (script-only, edge-triggered, silent
  unless alerting): >500k tokens/hour per session, >100k tokens/call, >$10/hr
  per key. Alerts, never caps.

### 3. Per-key first-$10/day alert + no-op call drop

- Any provider key crossing $10 in a UTC day alerts ONCE (edge-triggered).
  Catches new/unmonitored keys (opencode_go case) without throttling anything.
- The 37k no-op calls get dropped at the cheapest correct layer in
  `zai_proxy.py` (characterize pattern first, then drop before routing if
  they never need an upstream hit).
- **cached_tokens instrumentation** in the proxy response-logging path
  (OpenAI-compatible `usage.prompt_tokens_details.cached_tokens`, 0 when
  absent) so cache hit rate becomes measurable.

### 4. Atomic claims + per-worker state shards (reliability, not cost)

The atomic-claim pattern (mkdir-style atomic lock + per-worker state files +
atomic publish) prevents cross-worker merge collisions like the 2026-08-30
cross-CW merge race. Folded into worker handover rules: workers claim state
atomically, never write shared files in place.

### Tenant-isolation rules (conditional, for routstr or any fronted inference)

If routstr ever fronts self-hosted or cached inference: divergence marker per
tenant, per-tenant KV slots, shared cache holds PUBLIC content only. The
side-channel analysis holds — prefill timing leaks whether content was seen
before, so a shared cache across tenants is a confidentiality boundary
violation, not just a performance knob.

## Invariants

- **Alerts, not caps.** No monitoring job blocks, throttles, or restricts
  model choice. The router's free-market price discovery stays.
- **All new monitoring is script-only** (`no_agent` crons, zero LLM tokens),
  edge-triggered, silent stdout unless alerting.
- **Within a conversation the system prompt stays byte-stable** (existing
  contract); stable-first ordering changes assembly order ACROSS turns, never
  mutates past context.
- **No per-token external inference hosting.** 6.4B tokens/month at any
  per-token price dwarfs the flat-rate z.ai plan. KV-cache tricks only work on
  models we host ourselves — not applicable to upstream fleets.
- **NeuralWatt spend is quoted from their `/v1/quota`, never from our DB**
  (ours overcounts 5.7x).

## Consequences

### Positive

- −39%/call on 3 high-frequency crons (~$50/week est.) with zero behavior
  change; triage pattern extends the saving to reviewer lanes.
- Cache-hit rate becomes measurable (cached_tokens column) and improvable
  (stable prefixes → provider discounts ~10x on cached prefix).
- Runaway sessions and new keys surface within minutes instead of days.
- 25% of logged calls stop wasting quota windows.

### Costs

- Proxy edits are on the load-bearing path (23 workers + all crons route
  through :9099) — smallest viable diffs, timestamped .bak before edit,
  manager-scheduled restart, second-instance smoke test before deploy.
- Tiered triage adds a triage step to review workflows (bounded by the
  existing SLA/watchdog structure).
- The 2-week NeuralWatt trend must be collected BEFORE deciding the NW tier
  and the GPU question — sequencing is a constraint, not a nicety.

## Rollout

This week (dispatched 2026-08-31, board `cost-reduction-sprint`):

1. DONE directly: 3 cron jobs migrated glm-5.2 → glm-5.3; watchdog halved
   30m→60m.
2. T1: runaway-session watchdog on DQ05 (alert-only, script-only cron).
3. T2: per-key first-$10/day alert (script-only cron).
4. T3: no-op call drop at `zai_proxy.py` (pattern characterization →
   minimal drop change; no daemon restart by worker).
5. T4: cached_tokens instrumentation (proxy) + stable-first prefix ordering
   (agent core).

Next 2 weeks:

6. Collect NeuralWatt trend data from their authoritative `/v1/quota`
   (ours overcounts 5.7x).
7. Decide NW tier right-sizing (~$90/mo est.) from the trend.
8. Only then evaluate the GPU question with real numbers (see Notes).

## Notes

**Explicitly rejected (from external docs reviewed 2026-08-31):**

- Buying warm-codebase hosting — per-token pricing vs 6.4B tok/month makes any
  external per-token offer uncompetitive; KV-cache injection into upstream
  fleets (z.ai, NW) is impossible anyway.
- Local KV save/restore — llama.cpp-server-only feature; our ollama setups do
  not expose it and local lanes are short-context. Revisit only if we self-host.
- An 8-GPU review fleet — we have zero GPUs.

**Self-hosting verdict (operator question: "cover ALL DeepSeek needs with a
GPU?"):** Not possible at consumer prices. Full DeepSeek V4 is a 671B-class
model — hundreds of GB quantized, datacenter territory, not a €4,910 RTX 5090.
What a €700–1,200 16GB card (5070/5070 Ti) buys: a 12–14B coder model covering
triage/summaries/review drafts (~30–50% of call volume); quality-critical work
stays on API. Neither DQ05 nor T470 has a GPU slot → new box (+€300), German
electricity ~€50/mo at 200W 24/7 → payback vs cancelling NeuralWatt ($100/mo)
≈ 10–12 months. Marginal — decide only after NW trend data exists.

**Context:** strategies distilled from an external infra operator's notes
(llama.cpp KV save/restore, 8-GPU data-parallel review fleet, warm-codebases
product, GPU price research), mapped against our audit numbers.