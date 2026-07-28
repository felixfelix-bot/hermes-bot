# Telemetry Analysis — P3.7: Stray `parse_error` post-SSE-fix

**Task:** t_4327e484 · **Date:** 2026-07-29 · **Investigator:** worker-merchant
**Scope:** Determine why `parse_error` rows still appear in `provider_telemetry`
after the SSE-streaming fix (commit `6a99a41`), and fix or document accordingly.

---

## TL;DR

The SSE fix **worked**: valid JSON and valid SSE (`data:` lines with `choices`)
responses now classify as `response_valid=1` instead of `parse_error`.

The stray `parse_error` rows that remain are **not** a regression of the SSE
fix. They fall into two genuine classes — plus a **timezone bug in the task's
query threshold** that made the count look like zero:

1. **2 rows are real DNS/network failures** (`502`, *Temporary failure in name
   resolution*) that were **mislabeled** `parse_error`. The validator's
   unparseable-body branch clobbered the real `error_text`
   (`"proxy error: <urlopen error …>"`) with the generic string `parse_error`.
   → **Fixed in P3.7** (see below).
2. **13 rows are genuine non-JSON upstream responses** (HTTP `200` bodies that
   are neither JSON-with-`choices` nor SSE — e.g. gateway/edge error pages or
   truncated streams). `parse_error` is the correct label for these; they are
   real provider anomalies, not a parsing bug.
3. The task's threshold `ts > '2026-07-29T04:04:00'` was compared against the
   **UTC** `ts` column, but `04:04` is the *local* (+0530) wall-clock of the
   fix. The real post-fix UTC boundary is **`2026-07-28T22:34:17Z`** (proxy
   process start). Using the wrong boundary returns 0 rows and hides the burst.

---

## Method

1. **Schemas.** `provider_telemetry(ts TEXT ISO-UTC, …, error_type TEXT,
   billed_tokens, actual_tokens, token_mismatch)` and
   `api_calls(ts REAL epoch, …, status_code, error TEXT, total_tokens)`. Neither
   table stores the raw response buffer.
2. **Process restart time** (the true "fixed code" boundary) =
   `ps` STARTED `2026-07-29 04:04:17 +0530` = **`2026-07-28T22:34:17Z`**.
   Commit `6a99a41` was recorded 32 s later (`04:04:49 +0530`).
3. Queried all `parse_error` rows with `ts >= 22:34:17Z` (post-restart) and
   joined each to its `api_calls` row by exact timestamp (±8 s).

## Findings

### Post-restart parse_errors: 15 rows (and ongoing at analysis time)

All 15 have `response_received=1, response_valid=0`. Joined to `api_calls`:

| class | n  | api_calls signal | diagnosis |
|-------|----|------------------|-----------|
| DNS/network `502` | 2  | `status=502`, `error='proxy error: <urlopen error [Errno -3] Temporary failure in name resolution>'` | **genuine connection error**, mislabeled `parse_error` |
| non-JSON `200`    | 13 | `status=200`, `error=''`, `total_tokens=0`, telemetry `actual_tokens` 19–480 (text present) | **genuine invalid upstream body** (non-JSON, non-SSE) |

Representative rows (UTC):

```
id=2054 22:39:43 ours  glm-4.5-flash 502 "proxy error: …name resolution"  ← DNS
id=2241 22:47:18 ours  glm-5.2       502 "proxy error: …name resolution"  ← DNS
id=1953 22:34:55 friend glm-5.2      200 tokens=0  actual=97             ← non-JSON 200
id=1969 22:35:19 ours  glm-4.5-flash 200 tokens=0  actual=480            ← non-JSON 200
id=2623 22:55:11 friend glm-4.5-flash 200 tokens=0 actual=70             ← non-JSON 200
… (last observed: id=2749 22:57:07Z)
```

**Why these are provably non-JSON (no buffer inspection needed):** telemetry
only sets `parse_error` on the path where `json.loads(buf)` *raises* **and** the
SSE scan finds no `data:` line with `choices`. A JSON body lacking `choices`
would instead yield `error_type='none'`. Since we observe `parse_error`, the
buffers failed `json.loads` — i.e. they are non-JSON text. The 2 DNS rows are
the literal string `"proxy error: …"` written by the request handler's
exception branch.

### Root-cause diagram (telemetry validator, `do_POST` `finally`)

```
error_text = "proxy error: …name resolution"   ← set by 502 handler
resp_received = True  (buffer has the "proxy error:" text)
   json.loads(buf)  → raises (not JSON)
      SSE scan      → no data: lines → no choices
         else: error_type = "parse_error"      ← BUG: clobbers error_text
```

Result: `api_calls` correctly recorded the DNS error, but `provider_telemetry`
recorded `parse_error` for the same request — an **inconsistency between the two
tables** that hid real network failures inside the parse_error bucket.

---

## Fix (P3.7)

`zai_proxy.py`:

1. **Extracted** the inline classification block (formerly in `do_POST`'s
   `finally`) into a pure, importable helper **`_classify_response(buf,
   error_text) -> (received, valid, error_type)`**, next to `_parse_usage` (same
   module convention; same SSE handling).
2. **Preserved `error_text`**: the unparseable-body fallback now returns
   `error_text or "parse_error"` instead of unconditionally `"parse_error"`.
   Effect:
   - DNS/network/HTTP failures surface with their real cause
     (`"proxy error: …"`, `"HTTPError 429"`, …) instead of `parse_error`.
   - Genuinely unparseable `200` bodies (no `error_text`) still label
     `parse_error`, so the bucket now means exactly one thing: *an upstream 200
     that wasn't valid JSON/SSE*.
3. No change to request handling. Telemetry is wrapped in `try/except: pass`,
   so any failure stays silent and never affects a proxied request.

**Effect on the parse_error bucket:** the 2 DNS rows will move out of
`parse_error` into their real error labels. The remaining `parse_error` rows
are genuine provider anomalies worth a separate investigation (non-JSON 200s —
see Recommendations).

### Tests

New file `test_telemetry_classification.py` — **12/12 pass**, covering:
valid JSON w/ choices, valid SSE, JSON/SSE error bodies, empty buffer
(±`error_text`), JSON-without-choices, DONE-only, garbage/`None` (never raises),
and the **regression test** for the exact production DNS-failure buffer.

`test_response_parsing.py`: 9/10. The single failure
(`test_spend_tier_classification`) is **pre-existing config drift** — verified
by running it against the committed `HEAD:zai_proxy.py`, where `_spend_tier`
returns `'unknown'` for all four models. It is unrelated to this change (the
diff touches only `_classify_response` + the telemetry `finally` block).

### Quality gates

- **Gate 2 (tests):** PASS — 12/12 new; no regressions (1 pre-existing fail documented).
- **Gate 3 (this doc):** DONE.
- **Gate 4 (commit):** DONE — local commit on `master` (zai_proxy.py + test + this doc).
- **Gate 5 (push):** **DEFERRED to manager review** — see "Push / security note".
- **Gate 6 (manager review):** BLOCKED for review.

---

## Recommendations (follow-ups, not done here)

1. **Investigate the non-JSON 200s (the real anomaly).** 13 requests in ~23 min
   returned HTTP 200 with non-JSON, non-SSE bodies across both providers
   (z.ai `friend`, DeepInfra `ours`) and models `glm-5.2` / `glm-4.5-flash`.
   Correlated with the 2 DNS failures in the same window → suspect transient
   upstream/edge instability. To confirm the body content, add an opt-in debug
   capture of `response_buffer` for `parse_error` rows (the buffer is currently
   discarded). **Out of scope for P3.7.**
2. **Request-handler passthrough.** `do_POST` treats a non-JSON 200 as a success
   (the `except: pass` at the JSON-parse step leaves `is_empty=False`, so the
   raw body is forwarded to the client as 200). Consider failing over instead of
   forwarding a non-JSON 200. **Behavioral change — needs its own task + review.**
3. **Restart the proxy** to activate the telemetry fix (currently running the
   pre-P3.7 binary). Operational decision — left to the operator.
4. **Fix the task's timezone convention** when writing UTC thresholds: use an
   explicit `Z`/`+00:00` suffix, or convert local commit time to UTC.

## Push / security note (read before push)

- The repo has **two remotes**: `origin` (c03rad0r/hermes-bot) and `dr`
  (felixfelix-bot/hermes-bot). The `dr` remote URL **embeds a GitHub PAT**
  (`ghp_…`). Do **not** push to `dr` from logs/CI that may leak the URL; prefer
  `origin`, and consider rotating that token and moving it out of the git
  config. Flagging only — not modified by this task.
- The working tree is very dirty (many untracked/modified files unrelated to
  this task). The P3.7 commit stages **only** `zai_proxy.py`,
  `test_telemetry_classification.py`, and `docs/telemetry-analysis.md`.
