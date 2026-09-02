# Compaction Crisis Relief — Session Reset + Kalman-Native Control Law

**Date:** 2026-09-02 (~05:30 IST)
**Status:** IN PROGRESS
**Trigger:** Manager quality degraded by constant compaction — active sessions sit at
139–142K context, permanently above the 100K trigger (0.5 × 200K), so compaction fires
at essentially every turn boundary (~28–36 events/hr system-wide). Each event is a
lossy summary, eroding the manager's working memory.

## Context

- Empty-turn bleed is FIXED (proxy restart 05:24:42 carries `df8ed9c` reasoning
  normalization): 450 empty turns/hr → 13/hr.
- Exemption patch for the dynamic-ctx governor landed (`979bee0`) — manager pinned at
  ctx 200K as a **safety ceiling** (fallback-window immunity), not tuning.
- **Governance decision (Felix):** NO manual threshold overrides. The threshold knob
  belongs to the growth-governor Kalman. The manager stays under adaptive control.
- **Root defect:** no controller receives the crisis feedback signal. The growth
  Kalman sees growth-rate and cost-ratio; nobody sees compaction frequency or
  trigger pressure. Compounded by: compression calls log `session_id = NULL` (the
  agent's `trajectory_compressor.py` never passes one) → per-profile compaction rates
  are unmeasurable, so both the Kalman feedback AND the planned incident detector's
  ≥3/hr-per-profile trigger are blind.

## Phase A — Session reset (immediate relief, no tuning conflicts)

- [x] A1. Identify second hot session `20260901_044100_2c35fba2` — **RESOLVED:
      not present in ANY profile's `sessions.json` nor the gateway session
      store** — it is a worker/delegation lane. Left untouched; Phase B's
      control law governs it generically, and (post-B7) attributed compaction
      counts cover it.
- [x] A2. Export DM session `20260901_231649_f7431165` to JSONL backup:
      `~/.hermes/bot/backups/session-exports/20260901_231649_f7431165-pre-reset.jsonl`
      (1.0 MB, full history preserved).
- [ ] A3. Reset the DM session — **the sanctioned trigger is `/new` (or
      `/reset`) sent by Felix in the Signal DM** (`run.py::_do_reset` path;
      bypasses the running-agent guard, flips the channel to a fresh session
      with `is_fresh_reset` machinery + bootstrap skill injection). Raw store
      surgery was rejected: a live gateway owns its SQLite store, and editing
      `sessions.json` behind it races with in-memory state. `/new` PRESERVES
      the old session in the searchable store — the A2 export is
      belt-and-suspenders. Note: auto-reset cannot fire first — daily boundary
      is 04:00 and the session updated 04:52; idle threshold is 24h.
- [x] A4. Verified bootstrap machinery: context-snapshots written every 30 min
      (05:30 tick fresh, 6+ groups), `skills/devops/signal-group-bootstrap`
      consumes them, session-notes pipeline intact.
- [x] A5. Second session: leave it (see A1).

## Phase B — Kalman-native control law (durable fix)

- [x] B1. Attribution: `agent/auxiliary_client.py::call_llm` accepts
      `session_id=` and attaches `X-Hermes-Session` when the effective
      endpoint is loopback (mirrors the zai provider plugin's gate — the id
      never leaks to external providers); `agent/context_compressor.py`
      stashes the id via `on_session_start` (wired at `run_agent.py:589`,
      previously dropped) and threads it into the summarization call.
      10 tests: `tests/agent/test_compression_session_attribution.py`.
      Activates at the next hermes-gateway restart (B7).
- [x] B2. Growth-governor control-law extension
      (`compression_growth_governor.py`):
      - `_profile_pressure_signals()` — median prompt/(thr×ctx) over the
        profile's mapped gateway sessions + attributed compaction rate
      - `_pressure_step()` — thermostat integrator: sustained pressure
        (2 consecutive ticks) steps the target up, clear readings decay it,
        hovering holds (hysteresis); bounded at PRESSURE_MAX_ADJ
      - `MAX_THRESHOLD` 0.70 → 0.75; manager NOT exempted (Kalman owns it)
      - **BONUS FIX (real bug found during B2): the C1 price nudge was inert
        in production** — `compute_threshold` called `_price_nudge()` with no
        price, so the measured realized $/M reached state but never the
        control law. Price is now threaded through (`price=` kwarg) with a
        regression test pinning the wiring.
- [x] B3. Regression tests: 22 added (86 total in
      `tests/test_compression_growth_governor.py`) — sustain gate, hysteresis
      hold/decay, k_comp pathologies, pinned-session fixtures (142K/200K
      shape), session-map reading, price wiring, extended cap.
- [x] B4. Full suite: 460 passed, 12 failed — **identical to the documented
      pre-existing baseline** (telnyx ×4, pressure_wiring ×5,
      shadow_drop_ours ×1, cost_correction ×2). Zero new failures.
- [ ] B5. Commit + push `dr` (bot repo) + commit hermes-agent files
- [ ] B6. Verify on the governor's next cron tick — the cron runs the
      working-tree script directly, so the new law is live on the next 15-min
      tick; check `compression_growth_state.json` for `pressure_adj` /
      `pressure_runs` and the DM's rising target.
- [ ] B7. Gateway-restart window (graceful drain, after in-flight work):
      activate B1, verify compression rows carry `session_id`

## Deferred (documented, out of scope here)

- C2 preflight compaction (mid-turn overshoot class) — board task `t_d172b9b6`
- Auto-intervention detector implementation — awaiting GO on the 5 design defaults
  (this pass fixes its attribution prerequisite)
- D4 cached_tokens accounting (cache_hit dead since Aug 25)

## Verification log

- 05:31 Plan written. Diagnosis verified live: DM session f7431165 at 142K vs
  100K trigger; compaction 9/7/7/5 per 15-min quarter; empty turns 13/hr
  (fixed by the 05:24 proxy restart); ctx-governor exemption live (979bee0).
- 05:39 A1: 2c35fba2 not in any profile sessions.json nor the gateway session
  store (400-row scan) → worker lane, left alone.
- 05:44 A2: DM session exported to JSONL (1.0 MB, 1 session).
- 05:47 B2+B3: control-law extension + price-wiring fix landed; 86 tests
  green (22 new). Full suite 460 pass / 12 baseline failures — identical set.
- B1: auxiliary_client + context_compressor attribution landed; 10 tests
  green (`hermes -m pytest tests/agent/test_compression_session_attribution.py`).

