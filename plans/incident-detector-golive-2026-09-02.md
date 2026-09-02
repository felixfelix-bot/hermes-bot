# Incident Detector Go-Live — Registration, Verification, Wiring

**Date:** 2026-09-02 (~06:20 IST)
**Status:** IN PROGRESS
**Trigger:** GO received from Felix for the auto-intervention detector with all
5 recommended defaults; NW decision resolved (top up when kWh runs dry — no
router down-weight, no kWh budget line; the existing `nw_phase_b` exhaustion
alert in `cost-escalation-check.py` covers the "time to top up" signal).

## Starting state (verified)

- The other session landed the detector implementation at 05:42 and pushed it:
  scripts repo `d6855c4` — `incident_detector.py` (570 lines) +
  `tests/test_incident_detector.py` (355 lines, 10 tests).
- Locked constants match the 5 approved defaults: K=3.0, 6h cooldown,
  compaction trigger 3/hr per profile, gate-blocked → alert-only, ollama
  liveness probe before dispatch. Consultant lane: deepseek-v4-pro via
  kanban dispatch on the `router-maintenance` board.
- Compaction crisis detection reads **session-store splits**
  (`end_reason='compression'` in profile state.db), not api_calls —
  independent of (and complementary to) tonight's B1 attribution fix.
- **NOT registered as a hermes-cron job** — the detector exists but never
  runs. This is the go-live gap.
- `--retro` mode: scans the last N hours against the live DB and reports
  which incidents would have fired (no latch, no dispatch, no alert) —
  purpose-built for verification.

## Checklist

- [x] G1. Audit complete — and extended: the concurrent manager session was
      editing `incident_detector.py` mid-audit (06:19, same flaw found
      independently: standing `ours` will_exhaust=1). Its flip-semantics
      implementation landed uncommitted with red tests; this session
      completed the arc (tests updated to genuine-transition semantics,
      +2 regressions: standing no-refire, locked-key exclusion)
- [x] G2. Detector suite: **12 passed** (10 original, trigger test
      rewritten for flip semantics, 2 new regressions)
- [x] G3. Retro 12h vs live DB: the detector would have caught tonight's
      crisis at its FIRST hour — `token_burn_spike` (ollama_cloud ratio
      4.25×, 21M ptok/h + kalman will_exhaust) and `compaction_crisis`
      (41 compactions/hr, 2,686 empty turns/hr). Pure mode, no alerts sent
- [x] G4. Live idle/latched run: forced tick 06:24:04 → `ok`, silent
      (both classes in 6h cooldown until ~11:31 from the 05:31 e2e)
- [x] G5. Registered `0a0e800dc39c`: every 30m, ∞, `no_agent`,
      `deliver=origin`, `script=incident_detector.py` (first attempt
      registered as one-shot — caught, removed, re-registered recurring)
- [x] G6. Verified `Last run: ok`; next natural tick 06:54:04
- [x] G7. This doc + bot-repo commit + push dr
- [x] G8. Collision handled: completed (not duplicated) the concurrent
      session's in-flight arc — flipped implementation committed together
      (`d80ba1e`, crediting it), its 05:31 live e2e discovered (see log),
      and its two stale blocked incident tasks (crashed 05:09 episodes)
      completed with supersede comments

## Verification log

- 06:23 Scripts-repo state: `d6855c4` (detector, pushed) + uncommitted flip
  fix at 06:19 by the concurrent manager session — flip semantics match
  this session's independent diagnosis (standing `ours` will_exhaust=1
  would fire a consultant every 6h latch expiry).
- 06:27 Completed the arc: tests updated (12 green), committed + pushed
  `d80ba1e`.
- 06:29 Retro 12h proves first-hour detection of tonight's real crisis.
- 06:31 Live schemas verified: `sessions.end_reason/ended_at`,
  `messages.role/content/timestamp`, `key_health.key_name/healthy`,
  `kalman_samples` full column set — the silent mis-wiring class that
  killed the cost governor does not apply here.
- 06:33 Cron registered recurring after one-shot misfire fix.
- 06:36 **Discovery: the concurrent session already ran a live e2e at
  05:31** — both classes fired, incident YAMLs written, gate=pass,
  ollama=up, consultant tasks created and dispatched (worker-base
  claimed). Compaction consultant completed with findings written back
  into the incident file (verdict: burn clause NOT_FIRED, kalman clause
  only). Failure path also validated: the 05:09 episodes' consultant
  agents **segfaulted twice** → dispatch auto-blocked (failure_limit 2),
  crash-wrapper captured — the exact resilience the design intended.
- 06:40 Forced tick `ok`; stale blocked tasks `t_e13ce4f1`/`t_a9be1ebf`
  completed with supersede comments (duplicates of the resolved
  standing-condition investigation).
- Operational state: cooldown latch until ~11:31 IST for both classes;
  detector cron live (every 30m); escalation path proven end-to-end
  including gate/probe/kanban/model_override/crash-block.

## Decisions locked (this session)

| Decision | Value | Source |
|---|---|---|
| Spike multiplier K | 3.0 | Felix GO, consultant rec |
| Cooldown | 6h per class | Felix GO |
| Gate-blocked behavior | incident file + alert only, `gate=block` | Felix GO |
| Ollama liveness probe | yes, before consultant dispatch | Felix GO |
| Compaction trigger | ≥3/hr per profile | Felix GO |
| NW energy posture | accept pace, top up when exhausted | Felix, this session |
| Consultant lane | deepseek-v4-pro (flat-rate pool, $0 marginal) | design |
| Escalation home | router-maintenance board via kanban dispatch | design |

## Out of scope (stay parked)

- P1 MRE↔bot src sync (blocked, design done) and the board DEFERs — untouched
- NW kWh budget line / router down-weight — explicitly declined
