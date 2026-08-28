# PLAN — Experiential Harvest Integration (router quality stack)

- **Date:** 2026-08-28
- **Origin:** consultant review of github.com/experientiallabs/experiential (clone `~/repos/experiential`, read-only pattern source — their code is NEVER run)
- **Status:** consultant-reviewed draft, awaiting Felix approval → kanban

## Urgency: DEFER (implementation)
**Rationale:** operator (Felix, 2026-08-28): "Make the plan and schedule it now, but only dispatch the plan in the coming days when there is a cheap window." No active bleed, no deadline.
**Quota state at scheduling:** OK (5h 29%, weekly 33%, gate PASS, no paid burn).
**Dispatch decision:** plan authored now on glm-5.3; implementation tasks created on dedicated kanban board with `urgency=defer`, dispatched only via pressure-gated watchdog (cheap window: weekly <60% AND 5h <40%).

## Context
Experiential = task→MODEL router (offline, quality-guarded, guarded-kNN). Ours = model→PROVIDER router (live, price, Kalman burn-rate). Complement, different layer. Four harvest items, all pattern-adoption (Apache-2.0 read, zero code reuse):
1. Paired-CI estimator + evidence-bank/held-out for Kalman CPVO backtests
2. OTel genai-style request logging in zai_proxy
3. LLM-judge as quality multiplier
4. Quality term in flat-router objective

## Architecture (Consultant A, verified against live code)
1. **Paired-CI (kalman_evidence.py beside ~/.hermes/bot/kalman_health.py):** paired per-hour error *diffs* vs naive baseline (EMA/last-value) on same hours; `SE = max(empirical_SE, 1/√n)` Popoviciu floor; reject on `mean − k·SE < −tol` / insufficient pairs / sign disagreement. Backtest key gains baseline comparator, temporal split (last 20% held out, R/Q tuned on fit only), config sha256 (tuning+WARMUP) + `kalman_backtests` table. **Trap:** hourly autocorrelation → pair on 3h non-overlapping blocks.
2. **OTel spans (genai_telemetry.py):** adopt semconv v1.37.0 attribute NAMES only (`gen_ai.operation.name`, `gen_ai.request.model`, `gen_ai.usage.*`, `error.type`), W3C hex trace/span ids, parent/child spans, append-only JSONL `~/.hermes/bot/logs/genai_spans.jsonl` daily-rotated. Request span at `_proxy()` entry (~L5608); child span per provider attempt in flat-router loop. Custom `zaiproxy.*` attrs (provider, tier, cost_usd, cost_source, session_id, candidates_tried). NEVER message content.
3. **Judge (quality_judge.py in merchant-routing-engine/src):** draft-07 strict JSON schema `{dimensions:[{dimension_id, raw_score, rationale?}]}`, temp 0, reject tool_calls/prose, frozen prompt sha256, **model-identity check** (response.model must equal calibrated judge — no silent failover swap). ONE dimension (task_success 0–3). Monotonic calibration from 30–50 human pairs. Offline cron samples ~50 req/day from spans.
4. **Quality term (flat_router.py select_provider):** `adjusted = effective_cost × (1 + λ·(1−q))`, λ=0.25 initial, q default 1.0 (no evidence ⇒ today's price-only behavior). Reads `provider_quality` EWMA, 5-min cache. Skip when n_judged<10 or CI crosses neutral. **Anti-CAP guards:** floor multiplier 0.5, 7-day evidence TTL, re-rank never gate, rollback flags `.disable_flat_router` + `.disable_quality_term`.

**Do NOT adopt:** ArtifactStore content-addressed graph (10× ceremony), kNN embedder in hot path (no embedding model in catalog anyway), runtime/sandbox stack, OTLP AnyValue re-encoding (emit only), mandatory human-review gates (Signal-approve instead), multi-axis rubrics (one dimension until calibration proves stable).

## Validation protocol (Consultant B, quality-gates v3.1.0 mapping)
Shared: evidence artifact must run the PR's own code, real output pasted in PR comment; fit = anything that informed a parameter; freeze protocol = tune on fit → immutable policy lock (sha256 params+code rev, canonical JSON) → only then one-shot held-out, no re-tuning; bad artifacts renamed `.bad-<date>`, never edited.
- **Paired-CI:** RED tests (analytic coverage, 1000-sample property test, degenerates). Accept: 95% CI empirical coverage ∈ [0.92,0.97] over ≥1000 sims; width shrinks ≥1.5× when n×4; paired < unpaired width at ρ=0.6.
- **OTel spans:** RED schema-conformance tests; **zero-telemetry-leak assertion** (machine-checked: 0 occurrences of prompt/completion text or raw keys in spans); 100% of ≥200 sampled calls conform; p99 overhead <5%. GATE EXCEPTION on held-out split (no fitted params).
- **Judge:** fail-safe multiplier=1.0 when insufficient calibration; leakage-safe grouped OOF lineage split; accept: held-out judge–human agreement ≥80% or Spearman ρ ≥ 0.6; multiplier ∈ [0.5,2.0]; calibration report = measurements only, no thresholds.
- **Quality term:** RED tests (equal-cost fixture picks higher-q; λ=0 byte-identical to old objective; no-data ⇒ term=0). Accept on held-out replay: quality-weighted objective ≥5% better than cost-only; cost regression ≤10%; decision-flip rate ≤20% (pre-registered); zero new 429s.
- **Gate 9 (Playwright video): NOT satisfiable** (no UI) — declared exemption per backend-only path: pytest output + cold-review JSON in PR comment.

## Model matrix + budget (Consultant C, live catalog 2026-08-28)
- **Judge:** glm-5.3 primary (1M ctx, transcripts fit); kimi-k3 (telnyx) fallback as SEPARATELY CALIBRATED judge, never silent swap. Output bound 16,384 tokens, strict JSON.
- **Simulated workload generator:** glm-4.5-flash (validator = judge w/ distinct rubric; generator ≠ judge kills self-preference).
- **Embeddings:** SKIP — no embedding model in catalog. Lexical/deterministic features; reserve RouterEmbeddingReservation shape for future.
- **Mechanical workers:** deepseek/deepseek-v4-flash (Felix standing rule).
- **Budget:** per-run `RunBudget` object (NOT a global provider cap): max_judgments=100, sats ceilings per phase, `on_ceiling="warn"` default (block = explicit opt-in), prices read live from /v1/models at reservation. Worst-case judgment phase ≈ 300 sats. Alerts, never blocks — NO-CAPS policy intact.

## Phases & kanban tasks (board: router-quality)
| # | Task | Dep | Effort | Gate highlights |
|---|------|-----|--------|-----------------|
| T1 | genai_telemetry.py spans, flat-router path, RED leak test | — | S | leak assertion RED-first |
| T2 | kalman_evidence.py paired_ci + in-report CI (additive) | — | S | coverage property tests |
| T3 | quality_judge.py harness + 20 calibration samples | — | M | strict-schema tests, identity check |
| T4 | flat_router quality term, λ=0 SHADOW (log would-be rankings only) | — | S | λ=0 byte-identical test |
| T5 | kalman held-out split + config pinning + backtests table | T2 | M | freeze protocol |
| T6 | judge shadow-scoring cron (~50 req/day from spans) | T1,T3 | M | RunBudget, leak-free |
| T7 | spans reader → retry-waste detector report | T1 | S | e2e HTTP test |
| T8 | enable quality term λ=0.25 (needs calibration ≥30 pairs, agreement gate) | T3,T4,T6 | M | held-out replay ≥5% win |
| T9 | tune λ via held-out backtests + retune gate in kalman_retune | T8 | M | one-shot held-out |

Workers commit to feature branches, PRs to GitHub mirror, Felix merges (standing rule). Every task: TDD RED-GREEN, docs same commit, atomic conventional commits, push verified — quality-gates skill all gates.

## Risks
- Autocorrelation → SE underestimate (mitigate: 3h block pairing)
- Judge self-preference (mitigate: generator≠judge, rotate 2 judges, monthly refit)
- Stale quality score = hidden CAP (mitigate: TTL 7d, floor 0.5, re-rank not gate, shadow-first)
- Disk growth from spans (daily rotate, counts/ids/hashes only)

## Approval
Felix approves here → board goes live with pressure-gated dispatch. Plan versioned in this repo (ngit/GitHub dual-push per convention).
