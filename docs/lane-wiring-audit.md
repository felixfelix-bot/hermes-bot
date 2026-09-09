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
| `QUOTA_MODEL_DRIFT` | `/quota` says headroom but probe 429, or marked dead/exhausted but probe 200 | opencode_go `remaining: inf` vs real 429 |
| `COST_LEAK` | 1h PAYGO spend while a wired-healthy quota lane idled | $5.05/h → deepseek/chutes |

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

`--run-now` forces a pass (operator escape hatch). `--dry-run` never mutates state.

## Known heuristic nuance

`COST_LEAK` may name a specific idle lane (e.g. `ollama_cloud_2`) that is idle
by design — the ollama pool drains fullest-remaining first (`_ollama_cloud_key_order`),
so a sibling key can legitimately sit idle while another absorbs the pool's traffic.
The finding is still worth investigating (22M tokens/h spilled to PAYGO while the
pool had headroom), but the *culprit lane* is a hint, not a hard diagnosis.

