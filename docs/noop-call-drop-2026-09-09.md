# No-op API call drop — zai_proxy (T3, cost-reduction-sprint)

Date: 2026-09-09 · Author: worker-dq05 · Branch: worker-admin/noop-drop

## 1. No-op pattern characterization (read-only SQL against `~/.hermes/bot/zai_usage.db`)

**Predicate that defines an "empty no-op call"** (37,461 rows — exactly matches the
audit's 37,461 figure), scoped to the DROP predicate in code as
`_is_noop_api_call` (tightened per cold-review CHANGES_REQUESTED 2026-09-09 and
round-2 execution review 2026-09-09):

```
key_name IN ('ours','friend')
AND total_tokens = 0
AND (model IS NULL OR model = '')
AND status_code IS NULL
AND (error IS NULL OR error = '')
AND 0 < duration_ms <= 50        -- duration guard (round-2 fix)
```

Evidence (all read-only):

| Query | Result |
|---|---|
| Total api_calls rows | 224,764 @ audit · 254,165 re-verified 2026-09-11 |
| Rows with the no-op signature (any key) | 37,464 @ audit · 37,471 re-verified 2026-09-11 |
| No-op rows on `ours`/`friend` keys, `tier='zai'` | 37,464 (all of them) |
| Rows with zero tokens at all | 46,139 (rest are flagged 200/400/502/401) |
| No-op rows with `model` set | **0** |
| No-op rows with `status_code` set | **0** |
| No-op rows with `error` set | **0** |
| No-op `session_id` set | **0** — all `(null),(null)` |
| Duration of the fast no-op class | 2–11 ms (no upstream network round trip) |
| Would be DROPPED by this change (`0 < duration_ms ≤ 50`) | **31,361** |
| Kept (duration > 50 ms — real upstream attempts) | **6,102** |
| Kept (NULL duration — fail-open) | **8** |

**Duration distribution of the 37,464 signature rows** (the round-2 fix's
evidence — the signature alone is NOT a safe discriminator):

| Duration bucket | Count |
|---|---|
| NULL | 3 |
| 0–5 ms | 23,473 |
| 6–11 ms | 5,131 |
| 12–15 ms | 1,106 |
| 16–20 ms | 659 |
| 21–30 ms | 590 |
| 31–50 ms | 402 |
| 51–100 ms | 238 |
| >100 ms | 5,862 |

The **fast class (≤50 ms) is 31,361 rows** — the pure-logging no-op the audit
targeted. The **slow tail (>50 ms) is 6,100 rows** (116 of them >1 s, up to
192 s) — these are REAL upstream attempts that produced no tokens (genuine
503/404 failure telemetry), NOT the 2–11 ms never-reached-upstream class. The
round-2 execution review confirmed the real production 503 "all providers
exhausted" path and the non-chat 404 path leave `status_code`/`error_text` as
None (they are only assigned inside the retry loop) and may carry a model-less
body, so key+tokens+model alone would silently drop them. **The duration guard
(0 < duration_ms ≤ 50) is what separates the pure-logging no-op class from real
failure telemetry.**

**Time scope — CORRECTED (round-2), re-verified 2026-09-11**: the bulk of the
signature rows fall between **2026-08-14 and 2026-08-23** (the legacy
`best_key()` rollback path, dormant after the flat-router cutover ~Aug 24).
However, the earlier claim that "ZERO no-op rows exist since Aug 25" is
**empirically false**. Re-verified read-only on 2026-09-11: signature rows are
still appearing at roughly 3/day — 3 on 2026-09-09 (ids 222080/223259/224612)
and 3 more on 2026-09-10 (ids 240549/240569/240592). All 6 are `ours`,
0 tokens, NULL model/status/error, `cost_source='flat_rate'`, and **NULL
duration**.

**Attribution — verified, not assumed.** Every `_log_api_call()` call site in
the current source passes `duration_ms=int((time.time() - t0) * 1000)`, including
the z.ai site that alone emits `tier='zai'` (zai_proxy.py ~line 7336); `git log`
confirms that has held for every `zai_proxy.py` revision of the past week. So the
residual NULL-duration rows are **not** produced by the current code path — they
were written either by a daemon running an older/different revision or by an
out-of-band writer. This is recorded as an **open follow-up** (verify which
process writes them after the T3/T4 restart); it is NOT in T3's scope.

Because those residual rows carry **NULL duration**, the duration guard
**preserves them** (fail-open when the duration signal is unknown). Expected
effect of T3: the fast (≤50 ms) pure-logging no-op class is suppressed, as is any
future fast no-op row; the ~3/day NULL-duration residual class survives until its
emitter is identified. Measured scope of this change on existing data: **31,361**
of 37,471 signature rows (83.7%) are dropped; the 6,102 slow (>50 ms) real-failure
rows and the 8 NULL-duration rows are kept.

**What the fast no-op rows are**: empty telemetry rows logged by the historical
`best_key()` rollback path in `zai_proxy.py` (the `finally` block at ~line 7249
in the current source). Requests that reached that path with no usable `model`
field and no upstream response (buffer empty) were logged as
`key_name=ours|friend`, `model=None`, `status_code=None`, `error=None`,
`total_tokens=0`. The 2–11 ms durations confirm **no upstream API hit occurred**
— the row is pure logging overhead, not a billable call. It never consumed
quota and was not routed anywhere.

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
3. **Exactness + duration guard.** The guard fires only when ALL of
   `key_name ∈ {ours,friend}`, `total_tokens == 0`, `model is empty`, `status
   is None`, `error is None/''`, AND `0 < duration_ms ≤ 50` hold. The duration
   guard (round-2 fix) is what protects the real 503/404 failure paths that
   leave status/error as None: those always take real wall-clock time (>50 ms
   cycling providers / making upstream attempts), so they never match. NULL or
   0 durations are preserved (fail-open). Non-z.ai providers are untouched.

**Design**: a pure predicate `_is_noop_api_call(*, key_name, model, status_code,
error, total_tokens, duration_ms)` added to `zai_proxy.py`, evaluated as the
first line of `_log_api_call()`; when true, the function returns immediately
without inserting. This:
- is a single-function, no-refactor, minimal diff;
- keeps logging fail-open (only *skips* the insert, never raises);
- cannot break any real traffic path (real rows always pass the predicate);
- matches the audit signature exactly, plus the round-2 duration guard.

**Historical DB rows are NOT deleted** — the task is to stop *generating* them.
Deleting 37,461 historical telemetry rows is a separate concern (requires the
manager to confirm nothing reads them; out of scope for a minimal proxy change).
They remain as an audit record of the old path's behavior.

## 3. Verification notes

- Gate 1 (TDD): failing unit test written first (`test_noop_call_drop.py`),
  RED (predicate absent) → GREEN (guard added). Covered in same commit as source.
- Gate 2: full suite run — see completion summary for exact pass/fail counts.
  The 12 pre-existing failures on main@ee4a5ac are unrelated to this change
  (verified via stash test in run 14).
- Gate 2.5 (cold review, 2026-09-09): reviewer returned CHANGES_REQUESTED —
  original guard over-broad; tightened to key∈{ours,friend}+total_tokens==0 so
  model-less 503/404 failure telemetry and non-z.ai providers are never dropped.
- Round-2 execution review (2026-09-09): two blocking defects — (1) the
  tightened predicate still dropped real 503/404 failure telemetry (those paths
  leave status/error None; DB shows 6,100 slow rows matching the signature);
  (2) the "historical / zero since Aug 25" claim was falsified by 3 today-rows.
  **Fix**: added the duration guard (0 < duration_ms ≤ 50) so only the fast
  pure-logging no-op class is dropped, and corrected the doc's time-scope claim.
  Regression tests added for the real 503/404 paths (status None, slow) and
  NULL-duration rows.
- No live `:9099` daemon restart — manager schedules the restart (sequencing note).
