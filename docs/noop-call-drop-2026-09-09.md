# No-op API call drop — zai_proxy (T3, cost-reduction-sprint)

Date: 2026-09-09 · Author: worker-dq05 · Branch: worker-admin/noop-drop

## 1. No-op pattern characterization (read-only SQL against `~/.hermes/bot/zai_usage.db`)

**Predicate that defines an "empty no-op call"** (37,461 rows — exactly matches the
audit's 37,461 figure), scoped to the DROP predicate in code as
`_is_noop_api_call` (tightened per cold-review CHANGES_REQUESTED, 2026-09-09):

```
key_name IN ('ours','friend')
AND total_tokens = 0
AND (model IS NULL OR model = '')
AND status_code IS NULL
AND (error IS NULL OR error = '')
```

Evidence (all read-only):

| Query | Result |
|---|---|
| Total api_calls rows | 220,521 |
| Rows with the no-op signature (any key) | 37,461 |
| No-op rows on `ours`/`friend` keys, `tier='zai'` | 37,461 (all of them) |
| Rows with zero tokens at all | 46,139 (rest are flagged 200/400/502/401) |
| No-op rows with `model` set | **0** |
| No-op rows with `status_code` set | **0** |
| No-op rows with `error` set | **0** |
| No-op `session_id` set | **0** — all `(null),(null)` |
| Duration | 2–11 ms (no upstream network round trip) |

**Time scope is decisive**: every one of the 37,461 rows falls between
**2026-08-14 and 2026-08-23**. The count by day is 5070, 8809, 2456, 5151, 3155,
782, 3987, 3221, 3489, 1341 — and **ZERO no-op rows exist after 2026-08-25**
(both for this signature and for any `model IS NULL` row at all).

**What they are**: empty telemetry rows logged by the historical `best_key()`
rollback path in `zai_proxy.py` (the `finally` block at ~line 7249 in the current
source). Requests that reached that path with no usable `model` field and no
upstream response (buffer empty) were logged as `key_name=ours|friend`,
`model=None`, `status_code=None`, `error=None`, `total_tokens=0`. The 2–11 ms
durations confirm **no upstream API hit occurred** — the row is pure logging
overhead, not a billable call. It never consumed quota and was not routed
anywhere.

The flat-router full cutover (commit `08127e8`, ~Aug 24) made that legacy
`best_key()` path dormant (it only runs behind `.disable_flat_router`), which is
why the rows stopped after Aug 25. **No real traffic path has ever been harmed
by dropping them** — they never reached an LLM.

## 2. Drop point decision

**Chosen layer: the logging function `_log_api_call()` itself.**

Rationale:
1. **Cheapest correct layer.** The no-op rows are a *telemetry* artifact, not an
   upstream call — there is no quota to save by dropping earlier (the request
   never reached a provider). The rows exist solely because the logger inserted
   a row for a request that resolved to nothing. Suppressing the insert at the
   single chokepoint through which 100% of api_calls rows flow is the smallest
   possible change and the only layer that is *always* correct.
2. **BEFORE-routing vs AFTER does not apply** here — this is not a retry/dedup
   artifact that sends duplicate upstream hits. The audit's "25% of calls are
   empty" was a ratio of *telemetry rows*, not upstream hits. A proxy-side
   request-dedupe or pre-routing drop is unnecessary and risky; it would change
   real traffic behavior to chase rows that are already pure overhead.
3. **Exactness.** The guard fires only when ALL of `key_name ∈ {ours,friend}`,
   `total_tokens == 0`, `model is empty`, `status is None`, and `error is
   None/''` hold. This is the audited signature's FULL discriminating set, so
   genuine failure/exhaustion telemetry (a real 503 "all providers exhausted"
   or 404 with a model-less body and a real key) is NEVER suppressed, and
   non-z.ai providers are untouched. Per cold-review CHANGES_REQUESTED: the
   predicate keeps `token>0` rows (upstream round-trip happened) and any row
   with a status/error, even when model is empty.

**Design**: a pure predicate `_is_noop_api_call(*, key_name, model, status_code,
error, total_tokens)` added to `zai_proxy.py`, evaluated as the first line of
`_log_api_call()`; when true, the function returns immediately without inserting.
This:
- is a single-function, no-refactor, minimal diff;
- keeps logging fail-open (only *skips* the insert, never raises);
- cannot break any real traffic path (real rows always pass the predicate);
- matches the audit signature exactly.

**Historical DB rows are NOT deleted** — the task is to stop *generating* them.
Deleting 37,461 historical telemetry rows is a separate concern (requires the
manager to confirm nothing reads them; out of scope for a minimal proxy change).
They remain as an audit record of the old path's behavior.

## 3. Verification notes

- Gate 1 (TDD): failing unit test written first (`test_noop_call_drop.py`),
  RED (predicate absent) → GREEN (guard added). Covered in same commit as source.
- Gate 2: full suite = 632 passed, 12 failed, 0 subtests. The 12 failures are
  PRE-EXISTING on main@ee4a5ac (verified: stash my change → same 12 fail in
  test_cost_correction_and_deepseek_routing, test_pressure_wiring,
  test_shadow_drop_ours, test_telnyx_failover) — unrelated to this change.
  Zero failures caused by the no-op guard.
- Gate 2.5 (cold review, 2026-09-09): reviewer returned CHANGES_REQUESTED —
  original guard over-broad; tightened to key∈{ours,friend}+total_tokens==0 so
  model-less 503/404 failure telemetry and non-z.ai providers are never dropped.
  Verdict artifact: workspaces/t_66d2c01e/review_verdict.json.
- No live `:9099` daemon restart — manager schedules the restart (sequencing note).
