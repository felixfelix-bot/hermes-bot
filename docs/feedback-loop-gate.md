# Feedback-Loop Closure Gate (`feedback_loop_gate.py`)

Part of the Step 8c enforcement added to the `adding-api-key-to-live-router`
skill (commit fbbd08a) after the DeepSeek Direct incident (2026-09-08): a
provider is **not onboarded** until the Kalman feedback loop is **provably
closed** — every `api_calls` row carries a real, non-fallback, correctly-valued
cost, canonical model forms match what `real_price_tracker` expects, rate tables
are statically verified (no NameError/KeyError), balance collectors are wired
where APIs exist, and the seed actually wins enough traffic for Kalman to learn.

## When to Run

* **Provider onboarding** — the last step before declaring a new provider done.
  Attach the `gate/<provider>.json` evidence block to the task/PR.
* **Incident investigation** — when an onboarding feels "done" but the provider
  serves zero traffic or burns invisibly, run the gate to surface the failing
  probe(s).
* **Ratification prerequisite** (Step 12) — the independent reviewer MUST NOT
  begin until the gate output is attached and all probes are GREEN.

## Usage

```bash
# Run against ALL applicable probes for deepseek
python3 ~/.hermes/bot/scripts/feedback_loop_gate.py --provider deepseek

# Multiple providers, custom DBs
python3 scripts/feedback_loop_gate.py --provider ppq,deepseek \
  --usage-db /tmp/test-usage.db --burn-db /tmp/test-burn.db

# Chutes-class no-balance provider with documented negative probe
python3 scripts/feedback_loop_gate.py --provider chutes \
  --no-balance chutes --negative-proof gate/chutes-negative-probe.json

# Override the reference rate (when static parse is insufficient)
python3 scripts/feedback_loop_gate.py --provider custom \
  --reference-rate custom=0.175

# Print full JSON evidence to stdout (in addition to writing gate/<p>.json)
python3 scripts/feedback_loop_gate.py --provider deepseek --json
```

Exit code:
- `0` = every applicable probe GREEN: the feedback loop is closed.
- `1` = at least one probe RED (or classification unknown): evidence block
  attached — fix the failing probe and re-run.

Output: `gate/<provider>.json` — a structured evidence block containing every
probe's verdict, raw SQL queries run, timestamps, and the data returned.

## The Probes

### (A) Live dispatch 200 + correct cost

Queries the latest `PROBE_A_MAX_ROWS` (default 20) routed 200 rows. Verifies
**every** row has:
- `cost_usd` non-NULL (invisible burn guard)
- `cost_source` NOT in `{rate_derived_fallback, NULL}` (the $1.0 catch-all trap)
- `cost_usd/total_tokens*1e6` within ±50% of the model's reference rate (kills
  the $1.2654 inflated fallback — pitfall 19/20)
- The model has a known reference rate in the parsed `*_RATES` table (unkeyed
  model = the A.lint concern surfacing in data)

**GREEN** iff every examined row passes. **RED** on any failure.

### (A.lint) Static: rate table defined + fully keyed

Parses `zai_proxy.py` with the AST — never imports it. Collects every `*_RATES`
name referenced inside `_get_provider_cost`/`_extract_cost`/`_estimate_cost_usd`
and checks:
- **Every referenced table is DEFINED at module scope** (kills the `DEEPSEEK_RATES`
  referenced-but-undefined NameError trap)
- **Every model the provider maps** (via `_PROVIDER_MODEL_NAMES`) has a
  resolvable rate entry in at least one defined table (kills the `KeyError` on
  first-run — a model registered in the map but missing from the rate table)

**GREEN** iff both checks pass. **RED** on any undefined or under-keyed table.

### (B) Canonical model form logged

Queries `SELECT model FROM api_calls WHERE key_name=? GROUP BY model` for the
trailing 168h window. Every distinct model must be a canonical form (a key in
`_PROVIDER_MODEL_NAMES[provider]`). Also requires
`real_price_tracker.get_real_rate(provider)` to return a finite float —
proving the aggregation key in the measured-rate query matches what is logged.

**GREEN** iff all logged forms are canonical AND the measured rate is finite.

### (C) Balance/usage collector

**Balance-API providers** (ppq, openrouter, routstr, deepseek): queries
`balance_snapshots` for rows with `ts` within 10 minutes and `balance_usd >= 0`.
GREEN when fresh non-negative rows exist.

**No-balance-API providers** (Chutes-class): requires a documented
verified-negative 404 probe file (JSON with `{"status": 404}` or `{"404": true}`)
to be supplied via `--negative-proof`. The absence must be proven, never assumed.

**Unknown classification**: reported as SKIP — surfaced as a note but does not
block the gate by default (the operator must classify via `--no-balance` or
adding to `BALANCE_PROVIDERS`/`NO_BALANCE_PROVIDERS` in the script constants).

### (D) Seed wins traffic (deadlock prevention)

A seed only learns if it wins traffic. Counts `status=200 AND ts > now - 168h`
rows; also checks whether `get_real_rate(provider)` returns a non-None float
(meaning a non-seed measurement exists in the trailing window). BOTH must pass.

**GREEN** iff >=1 routed 200 AND a measured rate exists (Kalman updated).

### (E) SSE cost extraction

For providers known to stream, queries the newest `api_calls` row. If `cost_usd`
is non-NULL, SSE extraction worked. Non-streaming providers: SKIP (not
required).

## Architectural notes

* **Import-safe**: never imports `zai_proxy` (which loads API keys, starts the
  HTTP server, and prints secrets to stdout). The one safe runtime dependency is
  `src.real_price_tracker` (a pure calculator that only opens sqlite).
* **Static parsing**: `zai_proxy.py` is read from disk and parsed by the AST
  module for rate-table definitions, canonical model maps, and function-body
  name references. This means the gate can lint the code before it is ever
  deployed.
* **Deterministic**: every probe takes explicit DB paths and a `_now` timestamp.
  Tests use ephemeral sqlite databases and synthetic source strings — the
  verdict is a pure function of the data provided.
* **Evidence block**: `gate/<provider>.json` records every probe's verdict,
  raw SQL queries, returned data, and a timestamp. This is the artifact attached
  to the PR/task for Step 12 ratification.