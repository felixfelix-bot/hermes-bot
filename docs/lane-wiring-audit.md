# Lane-Wiring Audit

**File:** `~/.hermes/bot/scripts/lane_wiring_audit.py`
**Schedule:** hourly (`0 * * * *`, no_agent cron)
**State:** `~/.hermes/bot/lane_audit_state.json` (probe rate-limit + finding dedup)

## Purpose

Detect routing lanes that are **unusable despite having value** — the class of
defect that took the ollama pool offline for ~24h on 2026-09-08/09:

- The P0-1 rugpull-resilience edit emptied `_OLLAMA_CLOUD_KEYS` in
  `zai_proxy.py`. The `.key_disabled_*` flags were later removed (re-enable),
  but the list was never restored — so the flat router listed `ollama_cloud`
  first in every chain, the dispatcher iterated an empty list, and every
  dispatch returned `False` instantly (54 `dispatch_fail`s, 30s backoff) while
  96% free quota sat idle and ~$5/h flowed to PAYGO.

Neither `efficiency-monitor` nor `cost-inefficiency` caught it — they compare
lane-vs-lane *rates*, not *wiring*. This audit closes that gap.

## How it works

Reads live state read-only (`/quota`, `key_health`, `api_calls`, flag files),
then runs **rate-limited live probes** (1 per lane per 6h) to disambiguate
"provider down" from "our wiring broken": a key that probes HTTP 200 but the
router keeps dispatch-failing is a config gap, not an outage.

### Finding classes

| Finding | Trigger | Caught |
|---|---|---|
| `LANE_WIRING_GAP` | end-to-end glm-5.2 probe lands on PAYGO while a quota lane is healthy + headroom | empty `_OLLAMA_CLOUD_KEYS` |
| `SUSTAINED_DISPATCH_FAIL` | `dispatch_fail` streak *actively climbing* + probe 200 + headroom | wiring/config gap (not stale history) |
| `QUOTA_MODEL_DRIFT` | `/quota` says headroom but probe 429, or marked dead/exhausted **with an ACTIVE backoff** but probe 200 — either contradiction **confirmed by a fresh re-probe** before firing/resolving | opencode_go `remaining: inf` vs real 429; 2026-09-10 stale-cache 429 false positive |
| `COST_LEAK` | 1h PAYGO spend while a wired-healthy quota lane idled | $5.05/h → deepseek/chutes |

#### Active-bench discrimination (2026-09-09 oc2 incident)

The "stale backoff" QUOTA_MODEL_DRIFT arm requires the bench to be **still
active**: `key_health.backoff_until` must be in the future. The mirror's
`last_error_type` is sticky — it lingers after recovery until the next state
transition — so it alone does not mean the lane is benched. The routing gate
(`zai_proxy._is_key_healthy`) only benches a key while `now < retry_after`
(= `backoff_until`).

Live case: ollama_cloud_2 took ONE transient 429 at 09:59:03 (exhausted #1,
backoff 2s, expired 09:59:05; self-healed by the proxy's server-truth recovery
heuristic at 10:02:53). The audit read the stale mirror at 10:00:39 — 94s after
the backoff expired — and scheduled a fix task for a lane that was already back
in rotation. With the fix, that shape no longer fires; it instead **resolves**
an open drift finding once the mirror shows no active bench, the lane probes
200, and quota headroom exists (previously the finding latched open forever —
no resolve path).

#### Fresh-probe confirmation (2026-09-10 t_cb9de508 incident)

Every signal the drift arms compare is read fresh each run — `/quota`,
`key_health`, 1h spend — **except the lane probe**, which is TTL-cached for
6h (rate limit: 1 live probe per lane per 6h). The ollama 5h session window is
*shorter* than the probe cache that measures it, so a cached 429 can outlive
the exhaustion it captured:

- 15:00 — probe catches a GENUINE session-limit 429 on ollama_cloud (all four
  ollama keys probe 429: oc/oc2 session limits, oc3/oc4 monthly).
- 16:02 — the 5h session window rolls; ollama_cloud serves again (777
  successful dispatches over the next hour, api_calls ts 1789038135–1789039802).
- 17:00 — the audit reads FRESH `/quota` headroom (session 29%) against the
  **2h-stale cached 429** → contradiction → false QUOTA_MODEL_DRIFT task for a
  lane that was actively serving traffic.

The fix: before any QUOTA_MODEL_DRIFT arm consumes probe evidence to **change
state** (fire a finding, or resolve an open one), it re-probes the lane once
with `probe_lane(..., force=True)`, bypassing the cache:

- Fresh 200 after a cached 429 → the cached code was stale → no finding (and
  an open finding resolves — the lane provably serves).
- Fresh 429 confirming the cache → genuine drift → fires with live evidence
  ("re-probed fresh this run" in the task detail).
- Fresh probe impossible (env key gone) → unconfirmable cached evidence → no
  action; the lane's other signals (mirror, `/quota` server truth, spend) are
  still audited hourly.

Cost bound: zero extra probes in steady state (no contradiction → no
re-probe); at most one extra 1-token probe per lane per run, only while a
contradiction or an open drift finding exists. The fresh result replaces the
cached entry, restarting the rate-limit window.

### Handling (operator directive 2026-09-09)

- **Silent** to the user: anomaly rows are written `alerted=1`, so
  `anomaly-notify.sh` (`pending-alerts` selects `alerted=0`) skips them.
- **Auto-fixes**: each finding auto-creates a SOON-priority task on the
  `llm-routing` board (worker-routing, glm-5.3, QGATES + consultant cold-review
  gate). Transition-deduped via state file: a new task only on ok→broken;
  recurrence comments on the open task instead.
- **Legit exhaustion is never a finding**: oc3 monthly 100%, oc2 session 100%,
  opencode_go until reset are expected states.

## Ops

```
# dry-run (prints findings, no task/anomaly writes)
python3 ~/.hermes/bot/scripts/lane_wiring_audit.py --dry-run

# manual run (real findings → silent anomaly + SOON fix task)
python3 ~/.hermes/bot/scripts/lane_wiring_audit.py

# force a run even if backoff says not-due
python3 ~/.hermes/bot/scripts/lane_wiring_audit.py --run-now

# tests
~/.hermes/hermes-agent/venv/bin/python -m pytest test_lane_wiring_audit.py
```

Exit codes: `0` = clean, `1` = findings (still silent; tasks created).

## Backoff (novelty-reset, binary exponential, 24h cap)

The cron still fires hourly, but the script self-throttles via `next_run_at` in
`lane_audit_state.json`. After a real pass:

- **any finding** this run → reset to 1h
- **any open (unresolved) finding** → hold at 1h (known-broken lanes stay watched)
- **state-signature changed** → reset to 1h (novelty: failure counts, error
  types, health flags, headroom, e2e provider)
- otherwise → clean-streak +1, interval doubles: 1h → 2h → 4h → 8h → 16h → **24h cap**

This mirrors the G1 recovery-hold philosophy (binary-exponential, capped, reset on
signal). The novelty-reset is what makes the 24h ceiling safe: incidents of the
"lane broken" class always move a failure count or health flag before any
threshold trips, collapsing the backoff to hourly long before a finding would fire.

`--run-now` forces a pass (operator escape hatch). `--dry-run` never mutates
state. Dry-run is **fully inert**, including the resolve path: when a finding
clears, `_resolve_finding(dry_run=True)` transitions the in-memory record but
neither persists state nor posts the kanban "condition cleared" comment —
only a real (non-dry) pass may write to the board (2026-09-09 t_efc68b73
incident: the unguarded subprocess posted ~40 duplicate comments from inside
pytest runs, because bare `audit(dry_run=True)` reloads the real state file
where the finding was still open).

## Known heuristic nuance

`COST_LEAK` may name a specific idle lane (e.g. `ollama_cloud_2`) that is idle
by design — the ollama pool drains fullest-remaining first (`_ollama_cloud_key_order`),
so a sibling key can legitimately sit idle while another absorbs the pool's traffic.
The finding is still worth investigating (22M tokens/h spilled to PAYGO while the
pool had headroom), but the *culprit lane* is a hint, not a hard diagnosis.

