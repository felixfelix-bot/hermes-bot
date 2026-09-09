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
| `QUOTA_MODEL_DRIFT` | `/quota` says headroom but probe 429, or marked dead/exhausted **with an ACTIVE backoff** but probe 200 | opencode_go `remaining: inf` vs real 429 |
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

