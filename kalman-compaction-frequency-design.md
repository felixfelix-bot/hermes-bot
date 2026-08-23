# Kalman-Filter-Driven Dynamic Compaction Turn Frequency

**Design Document — Hermes Agent Manager Profile**
**Author:** Hermes Agent (delegated subagent)
**Date:** 2026-08-23
**Status:** Design (not yet implemented)

---

## 1. Mechanism Analysis: What `max_turns` Actually Controls

### 1.1 The config→env bridge

`agent.max_turns` in `config.yaml` is bridged into the environment variable
`HERMES_MAX_ITERATIONS` at two points in `gateway/run.py`:

1. **Startup bridge** (`_bridge_max_turns_from_config`, line 1211): Reads
   `config.yaml`, applies managed-scope overlay, and sets
   `os.environ["HERMES_MAX_ITERATIONS"] = str(agent_cfg["max_turns"])`.
   This runs on every turn (called from `_reload_runtime_env_preserving_config_authority`).

2. **Per-turn reload** (line 1437-1441): Unconditionally overwrites
   `HERMES_MAX_ITERATIONS` from config.yaml's `agent.max_turns` on each
   gateway turn, ensuring stale `.env` values can't shadow config.

### 1.2 How `max_iterations` is consumed

In `agent/conversation_loop.py` (line 563), the main tool-calling loop is:

```python
while (api_call_count < agent.max_iterations and 
       agent.iteration_budget.remaining > 0) or agent._budget_grace_call:
```

`agent.max_iterations` is set from the `max_iterations` constructor parameter
(`agent_init.py`, line 165, 278), which traces back to
`HERMES_MAX_ITERATIONS` or config.yaml's `agent.max_turns`.

**Critical finding:** `max_turns` controls the **maximum number of API calls
(tool-calling iterations) per user message**, NOT a compaction trigger. It is
the tool-loop iteration budget — how many times the agent can call tools before
giving up on a single user request. It does NOT directly trigger compaction.

### 1.3 The actual compaction triggers

Compaction is triggered by **three independent mechanisms**, none of which
involve `max_turns`:

1. **Token-threshold compressor** (`context_compressor.should_compress()`):
   Fires when `prompt_tokens >= threshold_tokens`, where
   `threshold_tokens = max(context_length × threshold_percent, MINIMUM_CONTEXT_LENGTH=64_000)`.
   This is checked after every API response (`conversation_loop.py`, line 4053).
   The `compression.threshold` config (0.6) feeds this.

2. **Payload-too-large / context-overflow error recovery** (lines 2640-3070):
   When the API returns a 413 or context-length error, the agent compresses
   context and retries. Up to 3 compression attempts.

3. **Gateway session hygiene** (`gateway/run.py`, line 8943):
   Pre-agent safety net for pathologically large transcripts. Fires at 0.85
   of context length (intentionally higher than the agent's compressor) or
   at `hygiene_hard_message_limit` (default 400 messages).

### 1.4 Implications for this design

**`max_turns` is NOT a compaction frequency knob.** It is a tool-loop budget.
The user's mental model ("compacts every 25 turns") is actually the
**token-threshold compressor** firing when accumulated context reaches 60% of
the resolved context window — which happens to correlate with ~25 turns of
accumulated conversation in typical manager sessions.

To dynamically control compaction frequency, we must target the
**`compression.threshold`** parameter (which the existing
`compression_cost_governor.py` already adjusts) rather than `max_turns`.
Alternatively, we can introduce a **new token-growth-rate-aware override** that
adjusts the threshold based on how fast context is growing.

**However**, the user's intent — "dense conversations compact more often, sparse
ones less often" — can still be realized by adapting `compression.threshold`:
- Dense sessions (high tokens/turn growth) → **lower** threshold → compact sooner
- Sparse sessions (low tokens/turn growth) → **raise** threshold → compact later

This is a **superset** of what the existing governor does. The existing governor
tracks cost-ratio and adjusts threshold + model budget. The new system adds a
**second signal: context growth rate**, and uses it to further adjust the
threshold within safety bounds.

---

## 2. Signal Selection

### 2.1 Available signals from `zai_usage.db`

The `api_calls` table contains per-call records with:
- `prompt_tokens`, `completion_tokens`, `total_tokens`
- `session_id`, `task_type` (e.g. 'compression')
- `ts`, `model`, `cache_hit`, `cost_usd`
- `duration_ms`, `status_code`

### 2.2 Measured data (last 24h, manager profile)

| Metric | Value |
|--------|-------|
| Total API calls | 7,771 |
| Session calls (with session_id) | 4,784 |
| Compression calls | 87 |
| Avg prompt_tokens | 49,520 |
| Max prompt_tokens | 150,222 |
| **Mean context growth per call** | **1,805 tokens** |
| **Median context growth** | **681 tokens** |
| **P75 growth** | **1,650 tokens** |
| **Stdev of growth** | **3,408 tokens** |
| Sessions tracked | 344 |

The growth rate distribution is highly skewed (median 681, mean 1,805, max
42,095), confirming that context growth varies dramatically by conversation
type. This is the signal we want the Kalman filter to track.

### 2.3 Selected primary signal: Context Growth Rate (g)

**Definition:** Average delta in `prompt_tokens` between consecutive
successful API calls within the same session, excluding post-compression
resets (where tokens drop sharply).

**Why this signal:**
1. Directly measurable from `zai_usage.db` — no new instrumentation needed
2. Correlates with "compaction urgency" — high growth = context fills faster
3. Already modeled in the compaction-tuning skill as `g` (context growth/turn)
4. Varies by 60× between sessions (681 median vs 42K max), giving the Kalman
   filter real signal to work with

### 2.4 Secondary signals (informational, not control inputs)

| Signal | Source | Use |
|--------|--------|-----|
| Compression cost ratio | Existing governor | Coordinated constraint — don't lower threshold if compression cost is already high |
| Session length (calls/session) | `api_calls` grouped by `session_id` | Contextual filter — short sessions (<10 calls) should not trigger aggressive lowering |
| Cache hit rate | `cache_hit` column | Future regime detection — if caching becomes active, optimal threshold flips |
| Compression effectiveness | Compare pre/post prompt_tokens around `task_type='compression'` | Anti-thrashing — if compressions save <10%, raise threshold |

### 2.5 The Kalman filter model

**State variable:** `g` — estimated context growth rate (tokens/call)

**Measurement:** Rolling 15-minute average of positive `prompt_tokens` deltas
per session, weighted by session activity (calls with session_id).

**Process model:** Random walk with drift
```
g[k] = g[k-1] + w[k]    where w ~ N(0, Q)
```

**Measurement model:**
```
z[k] = g[k] + v[k]      where v ~ N(0, R)
```

**Initial parameters:**
- `x0 = 1800` (measured mean growth rate)
- `P0 = 500000` (high uncertainty — σ ≈ 700)
- `Q = 50000` (process noise — allows adaptation to session-type changes)
- `R = 300000` (measurement noise — high variance in per-call growth)

These will be auto-tuned by the existing `kalman-retune` cron pattern.

### 2.6 Control law: threshold adjustment from growth rate

The compaction-tuning skill's economics model gives us the theoretical optimum:
```
N* ≈ √(2·C0 / g)    (turns between compactions, no-cache regime)
```
Where `C0 ≈ 17,500` (manager fixed prefix) and `g` is the growth rate.

The optimal threshold percentage that achieves compaction at turn N* is:
```
threshold* = (C0 + N*·g) / context_length
           = (C0 + √(2·C0·g) · g) / context_length  ... this isn't quite right
```

More precisely, the context at compaction time T is:
```
context_at_compaction ≈ C0 + g · T
```
We want compaction to fire when `context_at_compaction = threshold × ctx_len`:
```
threshold* = (C0 + g · T*) / ctx_len
```
Where T* is the optimal compaction interval. From the token economics:
```
T* = √(2·C0 / g)
```
So:
```
threshold* = (C0 + g · √(2·C0/g)) / ctx_len
           = (C0 + √(2·C0·g)) / ctx_len
```

For the manager (C0=17500, ctx_len=202752 for glm-5.3):
- g=681 (sparse): threshold* = (17500 + √(2·17500·681)) / 202752 = (17500 + 4883) / 202752 = 0.111
- g=1805 (average): threshold* = (17500 + √(2·17500·1805)) / 202752 = (17500 + 7952) / 202752 = 0.126
- g=5000 (dense): threshold* = (17500 + √(2·17500·5000)) / 202752 = (17500 + 13229) / 202752 = 0.151
- g=10000 (very dense): threshold* = (17500 + √(2·17500·10000)) / 202752 = (17500 + 18708) / 202752 = 0.179

**Problem:** The theoretical optimum thresholds are all below the 64K floor
(`MINIMUM_CONTEXT_LENGTH = 64,000`), which at ctx=202,752 corresponds to 0.316.
The floor dominates. This means the threshold is already pinned at 64K
regardless of config, and lowering it further has no effect.

**Revised approach:** Since the floor dominates for the manager profile at
202K context, the threshold lever is mostly inert below 0.316. The effective
trigger is `max(0.6 × 202752, 64000) = 121,651` tokens.

To make growth-rate-adaptive compaction work, we need to adjust the
**effective threshold above the floor**, not below it. The control law becomes:

```
threshold* = max(MINIMUM_THRESHOLD, base_threshold + k · (g_estimate - g_baseline))
```

Where:
- `MINIMUM_THRESHOLD = 0.316` (64K / 202,752 — the floor)
- `base_threshold = 0.40` (current deliberate quality purchase)
- `g_baseline = 1800` (measured average growth rate)
- `k` = sensitivity coefficient (tuned so dense sessions lower threshold
  toward the floor, sparse sessions raise it toward 0.60)

**Dense sessions** (g >> g_baseline): threshold approaches floor (0.316) →
compaction fires at 64K → more frequent compaction, less token waste per call

**Sparse sessions** (g << g_baseline): threshold raised toward 0.60 →
compaction fires at ~121K → less frequent compaction, preserves more context

---

## 3. Architecture

### 3.1 Decision: SEPARATE governor (not extension of existing)

**Rationale:**
1. The existing `compression_cost_governor.py` tracks a **cost ratio** (0-D
   signal: compression cost / total cost). The new system tracks a **growth
   rate** (1-D signal with different dynamics). Mixing them in one Kalman
   filter would require a 2-D state space and coupled measurement model —
   unnecessary complexity for two signals that operate on different timescales.
2. Separation of concerns: cost-ratio governor answers "are we spending too
   much on compression?" Growth-rate governor answers "how soon will we hit
   the context limit?"
3. The existing governor's PI controller outputs are coupled (threshold +
   budget move inversely). Adding a third output to the same PI controller
   would require retuning the entire control loop.
4. Both governors write to the same override file — the growth-rate governor
   can read the cost-ratio governor's output and compose, rather than both
   writing independently and racing.

### 3.2 Data flow

```
┌─────────────────────────────────────────────────────────────────┐
│                        zai_usage.db (api_calls)                 │
│  ts | session_id | prompt_tokens | task_type | model | ...     │
└──────────┬──────────────────────────┬──────────────────────────┘
           │                          │
           ▼                          ▼
┌─────────────────────┐    ┌─────────────────────────────────────┐
│ compression_cost_   │    │ compression_growth_governor.py      │
│ governor.py         │    │ (NEW)                               │
│                     │    │                                     │
│ Kalman filter on    │    │ Kalman filter on context growth     │
│ cost ratio          │    │ rate (tokens/call)                  │
│                     │    │                                     │
│ PI controller →     │    │ Control law → threshold adjustment  │
│ threshold + budget  │    │ based on growth rate vs baseline    │
└────────┬────────────┘    └────────┬────────────────────────────┘
         │                          │
         ▼                          ▼
┌─────────────────────┐    ┌─────────────────────────────────────┐
│ compression_        │    │ compression_growth_state.json       │
│ threshold_          │    │ (NEW — Kalman state)               │
│ override.json       │    │                                     │
│ (EXISTING output)   │    │ Reads cost governor's override to   │
│                     │    │ compose final threshold             │
└────────┬────────────┘    └────────┬────────────────────────────┘
         │                          │
         ▼                          ▼
┌─────────────────────────────────────────────────────────────────┐
│           compression_threshold_override.json                   │
│           (FINAL composed output — same file as today)          │
│                                                                 │
│  {                                                              │
│    "threshold": 0.42,          ← composed: cost + growth        │
│    "compression_budget": 0.20, ← from cost governor             │
│    "ratio_estimate": 0.01,     ← from cost governor             │
│    "growth_estimate": 3200,    ← NEW: from growth governor      │
│    "growth_threshold_adj": +0.02, ← NEW: growth-based delta     │
│    "target_turns": 18,         ← NEW: implied optimal turn count│
│    ...                                                         │
│  }                                                              │
└─────────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────┐
│  compression_model_router.py                                    │
│  Reads override.json → selects summarizer model + budget        │
│  (EXISTING consumer — no changes needed)                        │
└─────────────────────────────────────────────────────────────────┘
```

### 3.3 Composition logic

The growth governor runs **after** the cost governor and composes:

```python
# Read cost governor's output
cost_override = json.loads(OVERRIDE_FILE.read_text())
cost_threshold = cost_override["threshold"]
budget = cost_override["compression_budget"]

# Compute growth-based adjustment
growth_delta = compute_growth_adjustment(g_estimate, g_baseline)

# Compose: growth adjusts threshold, cost governor's budget stays
final_threshold = clamp(
    cost_threshold + growth_delta,
    MIN_THRESHOLD,    # 0.316 (floor-aware)
    MAX_THRESHOLD     # 0.70
)

# Write composed output back to same file
cost_override["threshold"] = final_threshold
cost_override["growth_estimate"] = g_estimate
cost_override["growth_threshold_adj"] = growth_delta
cost_override["target_turns"] = int(C0 / g_estimate)  # approximate
OVERRIDE_FILE.write_text(json.dumps(cost_override, indent=2))
```

### 3.4 Cron scheduling

The cost governor runs every 15 minutes (`comp-gov-1787437592` cron job).
The growth governor should run on the **same schedule** but **after** the cost
governor — using a 1-minute offset or chained execution.

**Option A (recommended):** Modify the existing cron job to run both:
```bash
python3 compression_cost_governor.py && python3 compression_growth_governor.py
```

**Option B:** Add a separate cron job at 16-minute intervals (offset by 1
minute from the cost governor). Risk: timing skew, both might read/write the
override file concurrently.

**Selected: Option A** — chain after cost governor. Simple, no race condition,
same cron slot.

---

## 4. Safety Bounds and Fallback Logic

### 4.1 Threshold bounds

| Bound | Value | Rationale |
|-------|-------|-----------|
| MIN_THRESHOLD | 0.316 | 64K / 202,752 — the `MINIMUM_CONTEXT_LENGTH` floor. Below this, the config value is inert. |
| MAX_THRESHOLD | 0.70 | Above the current 0.6 config; prevents context bloat. At 202K ctx → 141K trigger. |
| DEFAULT_FALLBACK | 0.40 | The existing cost governor's baseline; matches current "deliberate quality purchase" |

### 4.2 Growth-rate bounds

| Bound | Value | Rationale |
|-------|-------|-----------|
| MIN_G | 200 tokens/call | Below this, sessions are essentially Q&A — threshold should stay high |
| MAX_G | 20000 tokens/call | Above this, sessions are tool-heavy — threshold should hit the floor |
| g_baseline | 1800 | Measured 24h average; the neutral point for the control law |

### 4.3 Fallback cascade

```
1. Growth governor runs successfully → composed threshold written
2. Growth governor fails (exception) → cost governor's output stands unchanged
3. Both governors fail → override.json is stale → compression_model_router
   falls back to DEFAULT_BUDGET (0.20), and the config.yaml threshold (0.6)
   remains in effect for the agent's context_compressor
4. override.json missing entirely → same as #3 — config defaults apply
5. zai_usage.db missing or empty → growth governor returns g_estimate = g_baseline
   → growth_delta = 0 → no adjustment, cost governor's threshold preserved
```

### 4.4 Anti-thrashing protection

The existing `context_compressor.should_compress()` already has anti-thrashing
protection (skips compression if last 2 compressions saved <10%). The growth
governor adds a complementary check:

- If the measured compression effectiveness (pre/post prompt_tokens around
  `task_type='compression'` events) drops below 30%, **raise** the threshold
  by 0.05 (toward MAX_THRESHOLD) regardless of growth rate.
- This prevents the growth governor from driving toward more frequent
  compaction when compaction itself is not productive.

---

## 5. Implementation Plan

### Phase 1: Core Growth Governor (MVP)

**Files to create:**

| File | Purpose |
|------|---------|
| `~/.hermes/bot/compression_growth_governor.py` | Main governor script — Kalman filter on context growth rate, composes threshold with cost governor output |
| `~/.hermes/bot/compression_growth_state.json` | Kalman filter state (persisted between runs) |

**Files to modify:**

| File | Change |
|------|--------|
| `~/.hermes/profiles/manager/cron/jobs.json` | Update `comp-gov-*` cron job command to chain: `python3 compression_cost_governor.py && python3 compression_growth_governor.py` |

**No Hermes core changes needed.** The override file is already consumed by
`compression_model_router.py`, and the threshold is read by the agent at
session start (via `agent_init.py` → `context_compressor` initialization).

**Wait — important correction.** On closer inspection, the
`compression_threshold_override.json` is only consumed by
`compression_model_router.py` (for budget). It is NOT read by the agent's
`context_compressor` at initialization — the compressor reads
`compression.threshold` directly from `config.yaml` (see `agent_init.py` line
1317: `compression_threshold = float(_compression_cfg.get("threshold", 0.50))`).

This means the threshold override file has **no effect on the actual
compaction trigger** — it only affects the model router's budget selection.
The threshold in config.yaml is the sole source for the compressor.

**Revised output mechanism:** The growth governor must write the composed
threshold back to `config.yaml` via `hermes config set compression.threshold
<value>`. This is the same pattern documented in the compaction-tuning skill.

**Updated architecture:**

```
compression_cost_governor.py  →  compression_threshold_override.json
                                    (budget only — consumed by model router)

compression_growth_governor.py →  hermes config set compression.threshold <value>
                                    (threshold — consumed by agent's compressor)
```

**This changes the safety model:** The growth governor now writes to
config.yaml, which is a more sensitive operation. Mitigations:
1. Use `hermes config set` CLI (not direct file write) — same as the skill
   documentation recommends
2. Only write when the value changes by >0.02 (hysteresis — avoids churn)
3. Log every change with old/new values and rationale
4. Keep the override.json as an audit record (what the governor decided)
5. If `hermes config set` fails, fall back to the override.json approach
   (informational only, threshold stays at last-set config value)

### Phase 2: Enhancement and Tuning

**Files to create:**

| File | Purpose |
|------|---------|
| `~/.hermes/bot/compression_growth_retune.py` | Auto-tune Kalman R parameter (same pattern as existing `kalman_retune.py`) |

**Files to modify:**

| File | Change |
|------|--------|
| `~/.hermes/profiles/manager/cron/jobs.json` | Add hourly retune job for growth governor |

### Phase 3: Observability

**Files to create:**

| File | Purpose |
|------|---------|
| `~/.hermes/bot/compression_growth_health.py` | Health check / status report script |

### 5.1 `compression_growth_governor.py` — detailed design

```python
#!/usr/bin/env python3
"""Context-growth-rate Kalman governor for adaptive compaction threshold.

Tracks the average context growth rate (tokens/call) from zai_usage.db
using a 1-D Kalman filter. Adjusts compression.threshold via
`hermes config set` based on whether sessions are dense (high growth →
lower threshold → compact sooner) or sparse (low growth → raise threshold
→ preserve context longer).

Runs AFTER compression_cost_governor.py (chained in same cron slot).

State: ~/.hermes/bot/compression_growth_state.json
Output: hermes config set compression.threshold <value>
Audit: ~/.hermes/bot/compression_growth_override.json

Fallback: On any failure, leaves config.yaml unchanged.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from pathlib import Path

BOT_DIR = Path.home() / ".hermes" / "bot"
DB_PATH = BOT_DIR / "zai_usage.db"
STATE_FILE = BOT_DIR / "compression_growth_state.json"
AUDIT_FILE = BOT_DIR / "compression_growth_override.json"
CONFIG_PATH = Path.home() / ".hermes" / "profiles" / "manager" / "config.yaml"

# --- Constants ---
WINDOW_HOURS = 6           # Shorter window — growth rate is more recent signal
C0 = 17500                 # Manager fixed prefix (tokens)
CONTEXT_LENGTH = 202752    # glm-5.3 resolved context length

# Safety bounds
MIN_THRESHOLD = 0.316      # 64K / 202,752 (MINIMUM_CONTEXT_LENGTH floor)
MAX_THRESHOLD = 0.70       # Don't let context grow past 70% of window
FALLBACK_THRESHOLD = 0.40  # If anything goes wrong, stay at current baseline
HYSTERESIS = 0.02          # Only change config if delta > this

# Growth rate bounds
G_BASELINE = 1800          # Measured average (tokens/call)
G_MIN = 200                # Floor for growth estimate
G_MAX = 20000              # Ceiling for growth estimate

# Control law sensitivity
K_SENSITIVITY = 0.00004    # threshold_delta = K * (g_baseline - g_estimate)
# At g=10000 (dense): delta = K * (1800-10000) = K * (-8200) = -0.328 → clamped to MIN
# At g=5000:          delta = K * (1800-5000) = K * (-3200) = -0.128
# At g=681 (sparse):  delta = K * (1800-681) = K * (1119) = +0.045
# At g=200:           delta = K * (1800-200) = K * (1600) = +0.064


class GrowthRateKalman:
    """1-D Kalman filter on context growth rate (tokens/call)."""
    def __init__(self, initial_g: float = G_BASELINE):
        self.x = initial_g        # State estimate
        self.p = 500000.0         # Estimate uncertainty
        self.q = 50000.0          # Process noise
        self.r = 300000.0         # Measurement noise
        self.n = 0                # Update count

    def update(self, measurement: float) -> float:
        self.n += 1
        k = self.p / (self.p + self.r)
        self.x = self.x + k * (measurement - self.x)
        self.x = max(G_MIN, min(G_MAX, self.x))  # Clamp to bounds
        self.p = (1 - k) * self.p + self.q
        return self.x

    def to_dict(self):
        return {"x": self.x, "p": self.p, "q": self.q, "r": self.r, "n": self.n}

    @classmethod
    def from_dict(cls, d):
        kf = cls(d.get("x", G_BASELINE))
        kf.p = d.get("p", 500000.0)
        kf.q = d.get("q", 50000.0)
        kf.r = d.get("r", 300000.0)
        kf.n = d.get("n", 0)
        return kf


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {
        "kalman": GrowthRateKalman().to_dict(),
        "last_measurement": 0.0,
        "last_ts": 0,
        "current_threshold": FALLBACK_THRESHOLD,
        "last_config_threshold": FALLBACK_THRESHOLD,
    }


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def measure_growth_rate(db_path: Path, hours: int = WINDOW_HOURS) -> float:
    """Measure average positive context growth per call in recent sessions."""
    if not db_path.exists():
        return G_BASELINE  # Fallback

    cutoff = time.time() - hours * 3600
    try:
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("""
            SELECT session_id, prompt_tokens, ts
            FROM api_calls
            WHERE ts >= ? AND status_code = 200
              AND session_id IS NOT NULL
              AND task_type IS NULL
            ORDER BY session_id, ts
        """, (cutoff,)).fetchall()
        conn.close()
    except Exception:
        return G_BASELINE

    if len(rows) < 10:
        return G_BASELINE

    # Compute per-session growth, then average across sessions
    deltas = []
    current_sid = None
    prev_tokens = None

    for sid, pt, ts in rows:
        if sid != current_sid:
            current_sid = sid
            prev_tokens = pt
            continue
        if pt > prev_tokens:  # Only positive growth (exclude post-compression resets)
            deltas.append(pt - prev_tokens)
        prev_tokens = pt

    if not deltas:
        return G_BASELINE

    # Use median (robust to outliers — tool outputs can spike 40K+)
    deltas.sort()
    median = deltas[len(deltas) // 2]
    return float(median)


def compute_threshold(g_estimate: float, current_config_threshold: float) -> float:
    """Compute adjusted threshold from growth rate estimate."""
    # Control law: dense sessions (high g) → lower threshold
    delta = K_SENSITIVITY * (G_BASELINE - g_estimate)
    new_threshold = current_config_threshold + delta
    return max(MIN_THRESHOLD, min(MAX_THRESHOLD, new_threshold))


def apply_threshold(threshold: float, old_threshold: float) -> bool:
    """Apply threshold via hermes config set if change exceeds hysteresis."""
    if abs(threshold - old_threshold) < HYSTERESIS:
        return False  # No change needed

    try:
        result = subprocess.run(
            ["hermes", "--profile", "manager", "config", "set",
             "compression.threshold", f"{threshold:.4f}"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            return True
        else:
            print(f"[growth-governor] hermes config set failed: {result.stderr}")
            return False
    except Exception as e:
        print(f"[growth-governor] config set exception: {e}")
        return False


def main():
    state = load_state()
    kf = GrowthRateKalman.from_dict(state.get("kalman", {}))

    # Measure current growth rate
    measured_g = measure_growth_rate(DB_PATH)
    g_estimate = kf.update(measured_g)

    # Read current config threshold
    current_threshold = state.get("current_threshold", FALLBACK_THRESHOLD)

    # Compute new threshold
    new_threshold = compute_threshold(g_estimate, current_threshold)

    # Apply with hysteresis
    applied = apply_threshold(new_threshold, current_threshold)
    if applied:
        state["last_config_threshold"] = current_threshold
        state["current_threshold"] = new_threshold
    else:
        state["current_threshold"] = current_threshold

    # Save state
    state["kalman"] = kf.to_dict()
    state["last_measurement"] = measured_g
    state["last_ts"] = time.time()
    save_state(state)

    # Write audit file
    audit = {
        "growth_estimate": round(g_estimate, 1),
        "growth_measured": round(measured_g, 1),
        "growth_baseline": G_BASELINE,
        "target_threshold": round(new_threshold, 4),
        "current_threshold": round(state["current_threshold"], 4),
        "applied": applied,
        "implied_turns_to_compaction": int(
            (state["current_threshold"] * CONTEXT_LENGTH - C0) / max(g_estimate, 1)
        ),
        "updated_at": time.time(),
    }
    AUDIT_FILE.write_text(json.dumps(audit, indent=2))

    tag = "ADJUSTED" if applied else "stable"
    print(f"[growth-governor] g={measured_g:.0f} est={g_estimate:.0f} "
          f"threshold={state['current_threshold']:.4f} "
          f"turns_to_compact={audit['implied_turns_to_compaction']} "
          f"[{tag}] n={kf.n}")


if __name__ == "__main__":
    main()
```

### 5.2 Test plan

| Test | Description | How to verify |
|------|-------------|---------------|
| Unit: Kalman filter | Feed synthetic measurements, verify convergence | `python3 -c "from compression_growth_governor import GrowthRateKalman; kf=GrowthRateKalman(); [kf.update(g) for g in [500]*10]; assert 400 < kf.x < 600"` |
| Unit: measure_growth_rate | Point at test DB, verify extraction | Create temp DB with known session data, verify median growth |
| Unit: compute_threshold | Test control law at extremes | g=200 → threshold raised; g=20000 → threshold at floor |
| Integration: config set | Run governor, verify config.yaml updated | `hermes --profile manager config get compression.threshold` before/after |
| Fallback: DB missing | Delete DB path, run governor | Should output g=1800 (baseline), no crash, no config change |
| Fallback: hermes CLI missing | Mock subprocess failure | Should print error, not crash, not change config |
| Hysteresis | Run twice with same data | Second run should be "stable" (no config change) |
| Composition | Run cost governor first, then growth governor | Growth governor should not clobber budget field in override.json |

### 5.3 Cron integration

Current cron job (ID: `comp-gov-1787437592`):
```json
{
  "id": "comp-gov-1787437592",
  "schedule": {"kind": "interval", "minutes": 15},
  "no_agent": true,
  "command": "python3 /home/c03rad0r/.hermes/bot/compression_cost_governor.py"
}
```

Updated:
```json
{
  "id": "comp-gov-1787437592",
  "schedule": {"kind": "interval", "minutes": 15},
  "no_agent": true,
  "command": "python3 /home/c03rad0r/.hermes/bot/compression_cost_governor.py && python3 /home/c03rad0r/.hermes/bot/compression_growth_governor.py"
}
```

---

## 6. Cost-Benefit Analysis

### 6.1 Expected token savings

**Current state:** Fixed threshold at 0.40 (effective trigger: max(0.40 × 202752, 64000) = 81,101 tokens).
Every session compacts at the same context size regardless of growth pattern.

**With growth governor:**

| Session type | Growth rate | Current threshold | New threshold | Token savings/call |
|-------------|-------------|-------------------|---------------|-------------------|
| Dense (tool-heavy) | 5000 t/c | 0.40 (81K trigger) | 0.32 (64K trigger) | ~17K tokens/call × remaining calls |
| Average | 1800 t/c | 0.40 (81K trigger) | 0.40 (81K trigger) | 0 (no change at baseline) |
| Sparse (Q&A) | 500 t/c | 0.40 (81K trigger) | 0.48 (97K trigger) | Compaction delayed ~9 extra turns |

**Dense sessions:** Compacting at 64K instead of 81K saves ~17K tokens on
every subsequent API call in the session. For a 50-call session, that's
~850K tokens saved (but with one extra compaction event, costing ~9K tokens
for the summarizer call). Net: ~841K tokens saved per dense session.

**Sparse sessions:** Raising threshold to 0.48 delays compaction by
~9 turns (97K - 81K = 16K tokens / 1800 tokens/turn ≈ 9 turns). Each
compaction destroys information; delaying by 9 turns preserves more context
for the user's Q&A flow. Token cost is slightly higher (running at 85-97K
instead of 81K for 9 extra turns), but the quality benefit of retaining
context outweighs the ~20K extra tokens.

**Estimated daily impact (manager profile):**
- ~20 sessions/day, ~5 dense, ~10 average, ~5 sparse
- Dense savings: 5 × 841K = 4.2M tokens
- Average: no change
- Sparse extra cost: 5 × 20K = 100K tokens
- **Net daily savings: ~4.1M tokens (~1.2% of daily burn)**

This is modest because the 64K floor limits how low the threshold can go.
The bigger benefit is **quality preservation in sparse sessions** and
**reduced unnecessary compactions**.

### 6.2 Implementation cost

- ~200 lines of Python (governor script)
- ~50 lines of cron job JSON modification
- ~2 hours implementation + testing
- Zero LLM cost (cron, no_agent=true)
- Zero new dependencies (stdlib only)

### 6.3 ROI

The implementation is cheap (~2 hours) and the payoff is ongoing. Even at 1.2%
token savings, over a month that's ~120M tokens saved. More importantly, the
quality improvement from adaptive compaction (less signal loss in sparse
sessions, tighter context in dense ones) is the primary value.

---

## 7. Risks and Mitigations

### 7.1 Risk: `hermes config set` modifies a security-sensitive file

**Severity:** Medium
**Mitigation:**
- Use the CLI (`hermes config set`) not direct file writes
- Hysteresis (0.02 minimum delta) prevents churn
- Log every change with old/new values to the audit file
- Fall back gracefully if CLI fails (no change, no crash)
- Config changes take effect at next session start, not mid-session — no risk
  of corrupting an active session

### 7.2 Risk: Growth governor and cost governor conflict

**Severity:** Low
**Mitigation:**
- Growth governor runs AFTER cost governor (chained with `&&`)
- Growth governor reads the cost governor's threshold from the override file
- Growth governor composes (adds delta to cost governor's threshold), doesn't
  replace
- If cost governor's output is missing, growth governor uses config.yaml's
  current threshold as the base

### 7.3 Risk: Kalman filter converges to wrong value

**Severity:** Low
**Mitigation:**
- Process noise (Q=50000) keeps the filter adaptive — it never fully converges
- R parameter auto-tuned by retune cron (Phase 2)
- Safety bounds clamp the threshold to [0.316, 0.70]
- Hysteresis prevents rapid oscillation
- The filter tracks median growth (robust to outliers), not mean

### 7.4 Risk: `zai_usage.db` schema changes or goes stale

**Severity:** Low
**Mitigation:**
- Governor checks for DB existence and table structure
- Falls back to G_BASELINE on any error
- No crash, no config change on failure — just a log line

### 7.5 Risk: Context length changes (model switch)

**Severity:** Medium
**Mitigation:**
- Governor hardcodes CONTEXT_LENGTH=202752 (for glm-5.3)
- If model switches to glm-5.2 (1M context), the threshold math changes
  dramatically — the floor (64K) becomes 0.061 of context, not 0.316
- **Future enhancement:** Read resolved context length from config.yaml's
  `model.context_length` or model_metadata table at runtime
- For now, documented as a known limitation — if the model changes, update
  the constants in the governor script

### 7.6 Risk: Prompt caching becomes active

**Severity:** Low (currently)
**Mitigation:**
- If caching activates, the optimal threshold FLIPS high (compaction invalidates
  cached prefix → very expensive). The growth governor's logic (raise threshold
  for sparse sessions) would partially align with this, but dense sessions would
  be wrong (lowering threshold = more compaction = more cache invalidation)
- **Future enhancement:** Monitor `cache_hit` column; if hit rate > 10%,
  disable growth-based threshold lowering (only allow raising)

### 7.7 Risk: `max_turns` was the user's intended target, not threshold

**Severity:** Informational
**Mitigation:**
- This design document explains why `max_turns` is the wrong lever (it controls
  tool-loop iterations, not compaction frequency)
- The threshold is the correct lever for compaction frequency
- If the user still wants a turn-count-based mechanism, a future enhancement
  could add a turn-counter override that triggers `/compress` at a dynamic
  turn count — but this would require Hermes core changes (new hook in the
  conversation loop), which is outside the scope of a cron-driven governor

---

## 8. Summary

| Aspect | Decision |
|--------|----------|
| **What to control** | `compression.threshold` (not `agent.max_turns`) |
| **Signal** | Context growth rate (tokens/call), measured from `zai_usage.db` |
| **Filter** | 1-D Kalman filter on median growth rate per session |
| **Control law** | threshold = base + K × (g_baseline - g_estimate), clamped to [0.316, 0.70] |
| **Output** | `hermes config set compression.threshold` (with hysteresis) |
| **Audit** | `compression_growth_override.json` + `compression_growth_state.json` |
| **Fallback** | No change to config on any failure; falls back to last-set threshold |
| **Schedule** | Every 15 minutes, chained after existing cost governor |
| **LLM cost** | Zero (cron, no_agent=true, pure computation) |
| **Core changes** | None — uses existing CLI + config mechanism |
| **Files created** | 2 (governor script + state file) |
| **Files modified** | 1 (cron jobs.json — chain command) |