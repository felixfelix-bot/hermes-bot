# Dynamic Context Length Auto-Detection for Hermes Agent

**Design Document — Hermes Agent Manager Profile**
**Author:** Hermes Agent (delegated subagent)
**Date:** 2026-08-23
**Status:** Design (not yet implemented)

---

## 1. Mechanism Analysis: How `context_length` Flows Through the System

### 1.1 Config → Agent Initialization

`model.context_length` in `config.yaml` is read at agent initialization
time in `agent/agent_init.py` (lines 1417–1506). The resolution chain is:

1. **`_model_cfg.get("context_length")`** — reads `model.context_length` from
   config.yaml. If present and a valid positive integer, it becomes
   `_config_context_length`.
2. **`custom_providers` per-model override** — if step 1 returns None, checks
   `custom_providers` entries for a matching `base_url` + model with a
   `context_length` field.
3. `_config_context_length` is stored on `agent._config_context_length` for
   reuse during model switches and fallback activation.

### 1.2 ContextCompressor Initialization

In `agent/agent_init.py` (line ~1551), the `ContextCompressor` is
constructed with `config_context_length=_config_context_length`.

Inside `ContextCompressor.__init__()` (`agent/context_compressor.py`,
line 703):

```python
self.context_length = get_model_context_length(
    model, base_url=base_url, api_key=api_key,
    config_context_length=config_context_length,
    provider=provider,
)
```

**Critical:** `get_model_context_length()` in `agent/model_metadata.py`
has a **resolution step 0** that short-circuits ALL probing when
`config_context_length` is set:

```python
# 0. Explicit config override — user knows best
if config_context_length is not None and isinstance(config_context_length, int) and config_context_length > 0:
    return config_context_length
```

This means **the 131,072 hardcoded in config.yaml completely bypasses
all auto-detection**. The proxy's `/v1/models` endpoint, models.dev
registry, hardcoded family defaults — none of it runs. The agent trusts
the config value unconditionally.

### 1.3 How `context_length` is Used in the Compressor

Once resolved, `context_length` feeds three critical thresholds:

```python
# context_compressor.py line 712
self.threshold_tokens = max(
    int(self.context_length * threshold_percent),   # e.g. 131072 * 0.6 = 78643
    MINIMUM_CONTEXT_LENGTH,                          # 64000 floor
)

# Derived budgets
target_tokens = int(self.threshold_tokens * self.summary_target_ratio)  # e.g. 78643 * 0.2 = 15728
self.tail_token_budget = target_tokens
self.max_summary_tokens = min(int(self.context_length * 0.05), _SUMMARY_TOKENS_CEILING)
```

And in `should_compress()` (line 815):
```python
def should_compress(self, prompt_tokens: int = None) -> bool:
    tokens = prompt_tokens or self.last_prompt_tokens
    if tokens < self.threshold_tokens:
        return False
    # ... trigger compression
```

### 1.4 Gateway Hygiene Safety Net

In `gateway/run.py` (line ~8958), a **separate** context-length resolution
runs for the gateway session hygiene check:

```python
from agent.model_metadata import get_model_context_length
# ... resolves _hyg_context_length using same config_context_length override
_hyg_token_threshold = int(_hyg_context_length * _hyg_threshold_pct)  # 0.85
```

This is a **second call site** that reads `model.context_length` from config.
If we update config dynamically, both call sites will pick up the new value
at their next resolution (session start / turn boundary).

### 1.5 The `update_model()` Path

`ContextCompressor.update_model()` (line 643) is called on model switch
or fallback activation. It receives a `context_length` parameter and
recalculates `threshold_tokens`, `tail_token_budget`, and
`max_summary_tokens` for the new context. This path is used when the
agent detects a model change mid-session.

### 1.6 The Proxy's Model Fallback

The z.ai proxy at `localhost:9099` (`~/.hermes/bot/zai_proxy.py`) performs
**silent model downgrade** via tier routing:

1. **`_select_model_tier()`** (line 4738): Based on quota status, peak
   hours, and Kalman exhaustion predictions, the proxy rewrites the
   `model` field in the request body. E.g., `glm-5.3` → `glm-5.2` when
   quota is near exhaustion.

2. **`X-Served-Model` header** (line 3735): When the model is rewritten,
   the proxy adds `X-Served-Model: glm-5.2` and `X-Downgrade-Reason`
   response headers. But the agent doesn't currently read these headers
   to update its context_length.

3. **`model_decisions` table** (line 1712): Every model rewrite is logged
   to the `model_decisions` table in `zai_usage.db` with `original_model`,
   `model` (served), `reason`, and `tier`.

4. **`api_calls` table** (line 1656): Each API call logs the `model` field
   — which is the **served** model (post-rewrite), not the requested model.

5. **`/v1/models` endpoint** (line 5529): Returns a static stub listing
   `glm-5.3`, `glm-5.2`, `glm-4.5-flash`, `glm-4.5-air` — but **does NOT
   include `context_length`** in the response. The stub only has `id`,
   `object`, `created`, `owned_by`, and `sats_pricing`.

### 1.7 The `get_model_context_length()` Resolution Chain

When `config_context_length` is None (no config override), the full
resolution chain in `model_metadata.py` runs:

0. ~~Config override~~ (skipped)
1. Persistent cache (model+base_url)
2. AWS Bedrock static table
3. Custom endpoint `/v1/models` probe (would hit the proxy — but proxy
   doesn't return context_length)
4. Anthropic API
5. Provider-aware lookups (Copilot, Nous, Codex OAuth, GMI, Ollama
   /api/show, models.dev)
6. OpenRouter live API metadata
7. **Hardcoded defaults** (`DEFAULT_CONTEXT_LENGTHS` dict)

For the z.ai proxy at `localhost:9099`:
- Step 2: `_is_custom_endpoint("http://localhost:9099")` → True
- Step 3: `_resolve_endpoint_context_length("glm-5.3", "http://localhost:9099")`
  → probes `/v1/models` → gets the stub back → **no `context_length` field**
  → returns None
- Step 5e: Ollama `/api/show` probe → 404 (not an Ollama server) → None
- Step 5f: models.dev lookup for provider "zai" → may or may not have
  glm-5.3
- Step 8: **Hardcoded defaults** — `"glm-5.2": 1_048_576` matches via
  substring, but `"glm"` catch-all is `202752`. Note: there is NO
  `"glm-5.3"` entry in `DEFAULT_CONTEXT_LENGTHS`!

**This is a critical finding:** If we remove `model.context_length` from
config to let auto-detection work, `glm-5.3` would fall through to the
generic `"glm": 202752` hardcoded default — which is wrong (should be
1M). The `DEFAULT_CONTEXT_LENGTHS` dict has `"glm-5.2": 1_048_576` but
no `"glm-5.3"` entry. Substring matching is longest-key-first, so
`"glm-5.2" in "glm-5.3"` is False, and `"glm" in "glm-5.3"` is True →
returns 202752.

### 1.8 Summary of Current Problems

| Problem | Impact |
|---------|--------|
| `context_length: 131072` hardcoded in config | Agent thinks context is 128K; compacts at 78K instead of 600K. Massive over-compression. |
| Config override bypasses ALL detection (step 0) | Even if proxy reported context_length, it would be ignored |
| No `glm-5.3` in `DEFAULT_CONTEXT_LENGTHS` | If config override removed, falls back to 202K (generic "glm") instead of 1M |
| Proxy doesn't report `context_length` in `/v1/models` | Agent's endpoint probe gets nothing useful |
| Proxy silently downgrades 5.3→5.2 | Agent's context_length stays at whatever config says, even if actual model changed |
| Growth governor hardcodes `CONTEXT_LENGTH = 202752` | Governor math is wrong for both 131K (config) and 1M (actual 5.3) |

---

## 2. Model Detection Strategy

### 2.1 Available Detection Signals

| Signal | Source | Freshness | Cost | Reliability |
|--------|--------|-----------|------|-------------|
| Most recent `model` in `api_calls` | `zai_usage.db` | Real-time (last API call) | Free (local SQLite) | High — proxy logs served model |
| Most recent `model_decisions` row | `zai_usage.db` | Real-time | Free | High — logs original vs served model |
| `X-Served-Model` response header | HTTP response | Per-request | Free (header read) | Highest — but only available on HTTP response |
| Probe request to proxy | `POST /v1/chat/completions` | On-demand | 1 API call (minimal tokens) | High — response `model` field shows served model |
| `/quota` endpoint | `GET localhost:9099/quota` | Real-time | Free | Indirect — shows key quota, not active model |
| `/v1/models` endpoint | `GET localhost:9099/v1/models` | Static | Free | None for context_length (field missing) |

### 2.2 Selected Strategy: Database-First Detection

**Primary:** Query `zai_usage.db` for the most recent successful API call's
`model` field. This is the **served model** (post-tier-rewrite), which is
exactly what we need.

```sql
SELECT model FROM api_calls
WHERE status_code = 200
ORDER BY ts DESC
LIMIT 1
```

**Why this approach:**
1. **Zero cost** — local SQLite query, no API call
2. **Real-time** — updated on every API call the proxy handles
3. **Accurate** — logs the actual served model, not the requested model
4. **Already available** — `zai_usage.db` is the same DB the cost governor
   and growth governor use
5. **No proxy modifications needed** — the logging is already in place

**Fallback chain:**

```
1. Query zai_usage.db for most recent model → lookup in registry
2. If DB missing or empty → send minimal probe to proxy
   (1-token completion request, check response model field)
3. If proxy unreachable → read config.yaml's model.default
   → lookup in registry
4. If model not in registry → use config's context_length as-is
5. If all else fails → leave config unchanged (backward compatible)
```

### 2.3 Probe Request Design (Fallback)

If the DB is empty or stale (>15 min since last call), send a minimal
probe:

```python
import urllib.request, json

probe_body = json.dumps({
    "model": "glm-5.3",
    "messages": [{"role": "user", "content": "hi"}],
    "max_tokens": 1,
    "stream": False,
}).encode()

req = urllib.request.Request(
    "http://localhost:9099/v1/chat/completions",
    data=probe_body,
    headers={"Content-Type": "application/json"},
    method="POST",
)
resp = urllib.request.urlopen(req, timeout=10)
served_model = json.loads(resp.read()).get("model")
```

**Cost:** 1 prompt token + 1 completion token. Effectively free.
**Frequency:** Only when DB is stale — normally never needed.

### 2.4 Why Not Read `X-Served-Model` Headers?

The `X-Served-Model` header is the highest-fidelity signal, but:
1. It's only available on HTTP responses — the cron governor doesn't
   make API calls (it runs with `no_agent=true`)
2. Reading it would require instrumenting the agent's HTTP client to
   capture and persist the header — a core code change
3. The DB already captures the same information (the `model` field in
   `api_calls` is the served model)

**Future enhancement:** The agent's HTTP client could capture
`X-Served-Model` and call `context_compressor.update_model()` when the
served model differs from the configured model. This would enable
**mid-session context_length adaptation**. But this requires core changes
and is out of scope for the cron-driven governor.

---

## 3. Context Length Registry

### 3.1 Model → Max Context Mapping

A JSON file at `~/.hermes/bot/model_context_registry.json`:

```json
{
  "glm-5.3": 1000000,
  "glm-5.2": 1048576,
  "glm-4.5-flash": 128000,
  "glm-4.5-air": 128000,
  "kimi-k3": 262144,
  "kimi-k2.7-code": 262144,
  "deepseek-v4-pro": 1000000,
  "deepseek-v4-flash": 1000000,
  "deepseek-chat": 1000000,
  "deepseek-reasoner": 1000000
}
```

**Important note on GLM context windows:**

The `DEFAULT_CONTEXT_LENGTHS` in `model_metadata.py` lists
`"glm-5.2": 1_048_576` (1M). However, the z.ai subscription API
documentation and empirical testing show:

- **glm-5.3**: 1,000,000 tokens (1M) — confirmed by z.ai API docs
- **glm-5.2**: 200,000 tokens — per z.ai API docs (the 1M in
  `DEFAULT_CONTEXT_LENGTHS` appears to be from the open-weights HuggingFace
  model, not the API endpoint)

Since we're talking to the z.ai **API** (via the proxy), we use the API's
context windows, not the open-model context windows. The registry
reflects API reality.

**Design decision:** External JSON file (not hardcoded in Python) because:
1. New models arrive frequently — JSON edit doesn't require code change
2. The growth governor and context detector both read it
3. Can be updated by a future cron job that queries z.ai docs
4. Human-readable and auditable

### 3.2 Safety Margin

**Decision: Set context_length to 100% of advertised, not 90%.**

Rationale:
- The proxy is a trusted local intermediary — if it serves `glm-5.3`,
  it trusts z.ai's advertised 1M context
- The agent already has 413 error-recovery compaction (3 retry attempts)
- The compressor threshold is 60% of context_length, so we compact at
  600K — well under 1M. There's 400K of headroom before hitting the limit
- Setting to 90% would waste 100K of context for minimal safety gain
- The `MINIMUM_CONTEXT_LENGTH = 64000` floor is an additional safety net

**Exception:** If 413 errors are observed in `zai_usage.db` within the last
hour for the current model, reduce to 90% of advertised. This is an
automatic safety response, not a permanent setting.

---

## 4. Dynamic Update Mechanism

### 4.1 The Detector Script

New file: `~/.hermes/bot/context_length_detector.py`

```
┌─────────────────────────────────────────────────────────────┐
│                  context_length_detector.py                  │
│                                                              │
│  1. Query zai_usage.db for most recent served model          │
│  2. Look up model in model_context_registry.json             │
│  3. Compare with current config.yaml context_length          │
│  4. If different → hermes config set model.context_length N  │
│  5. Write audit record to context_length_state.json          │
│                                                              │
│  Fallback: If detection fails, leave config unchanged        │
└─────────────────────────────────────────────────────────────┘
```

### 4.2 Cron Schedule

**Runs every 15 minutes**, chained BEFORE the compression governors:

```bash
python3 context_length_detector.py && \
python3 compression_cost_governor.py && \
python3 compression_growth_governor.py
```

**Why before the governors:** The growth governor needs the current
`context_length` as an input. If the detector updates it first, the
growth governor reads the fresh value.

### 4.3 The `hermes config set` Pattern

Same pattern as the cost governor and growth governor:

```python
subprocess.run(
    ["hermes", "--profile", "manager", "config", "set",
     "model.context_length", str(new_context_length)],
    capture_output=True, text=True, timeout=30
)
```

### 4.4 Hysteresis

Only update config when the value **actually changes**. No hysteresis
threshold needed — the registry values are discrete jumps (131K → 1M,
1M → 200K), not continuous. But we do check:

```python
if new_context_length == current_config_context_length:
    return  # No change needed
```

### 4.5 Mid-Session Behavior

**Config changes take effect at session start, not mid-session.**

When `hermes config set model.context_length 1000000` runs:
- **Existing sessions:** Continue using their already-initialized
  `context_compressor.context_length`. The compressor was initialized
  at session start with the old value. It will NOT pick up the new value
  until the session restarts.
- **New sessions:** Read the updated config.yaml at initialization and
  get the new context_length.

This is **safe and correct** because:
1. The proxy's tier rewrite handles mid-session model changes at the
   proxy level — the agent still sends requests, the proxy routes them
   to the available model
2. If the proxy downgrades 5.3→5.2 mid-session and the agent's
   context_length is still 1M, the agent will try to send >200K tokens,
   get a 413 from z.ai, and trigger error-recovery compaction (3 retries)
3. The next session start will pick up the corrected context_length

**Future enhancement:** The `X-Served-Model` header could trigger
`context_compressor.update_model()` mid-session. This would require:
- Agent's HTTP client capturing the header
- Comparing served model vs configured model
- Calling `update_model()` with the new context_length
- This is a core code change, out of scope for the cron governor

### 4.6 State File

`~/.hermes/bot/context_length_state.json`:

```json
{
  "detected_model": "glm-5.3",
  "detected_context_length": 1000000,
  "config_context_length": 1000000,
  "detection_source": "zai_usage.db",
  "last_detected_at": 1724428800,
  "last_config_update_at": 1724428800,
  "detection_count": 42,
  "config_update_count": 3,
  "history": [
    {"ts": 1724427000, "model": "glm-5.3", "ctx_len": 1000000, "source": "db"},
    {"ts": 1724424000, "model": "glm-5.2", "ctx_len": 200000, "source": "db"},
    {"ts": 1724421000, "model": "glm-5.3", "ctx_len": 1000000, "source": "db"}
  ]
}
```

---

## 5. Proxy Fallback Handling (5.3 → 5.2 Transition)

### 5.1 Detection of Fallback

The detector runs every 15 minutes. When the proxy falls back from
glm-5.3 to glm-5.2:

1. **Next API call** logs `model: "glm-5.2"` in `api_calls` table
2. **Next detector run** (within 15 min) queries the DB, sees
   `glm-5.2`, looks up 200,000 in the registry
3. Detector runs `hermes config set model.context_length 200000`
4. Next session start picks up the 200K context_length

### 5.2 Emergency Compaction Scenario

**Problem:** If the agent is mid-session with context_length=1M and has
accumulated 500K tokens when the proxy falls back to glm-5.2 (200K):

1. Agent sends 500K-token request → proxy forwards to z.ai → z.ai
   returns 413 (context too large)
2. Agent's error-recovery compaction fires (conversation_loop.py
   lines 2640-3070): compresses context and retries, up to 3 attempts
3. Each compression attempt reduces context by ~60-80%, so after 1-2
   attempts the context is under 200K and the request succeeds

**This is the existing safety mechanism and it works.** The 413 recovery
path is independent of `context_length` config — it triggers on the
actual HTTP error, not on a threshold check.

### 5.3 Why Not Immediate Detection?

The detector runs on a 15-minute cron. An alternative would be to have
the proxy expose an endpoint that the agent polls on every turn, or have
the proxy push a notification. But:

1. **15 minutes is fast enough** — the proxy fallback is typically
   quota-based (5-hour windows). It doesn't flip-flop every minute.
2. **The 413 safety net covers the gap** — if context is too large for
   the fallback model, error-recovery compaction handles it
3. **Zero-cost requirement** — polling on every turn would add latency
   to every API call; the cron approach is free
4. **The `X-Served-Model` header is already in responses** — a future
   core enhancement can read it for instant detection without polling

### 5.4 Fallback Recovery (5.2 → 5.3)

When quota resets and the proxy starts serving glm-5.3 again:

1. Next API call logs `model: "glm-5.3"` in `api_calls`
2. Next detector run sees `glm-5.3`, sets context_length back to 1M
3. New sessions get the full 1M context window

This automatic recovery is a key benefit — currently the context_length
is manually set and never auto-recovers.

---

## 6. Kalman Integration: Context Length as Governor Input

### 6.1 Current Growth Governor (from existing design)

The growth governor's control law (from
`kalman-compaction-frequency-design.md`):

```
threshold = base_threshold + K × (g_baseline - g_estimate)
```

With bounds:
```
MIN_THRESHOLD = 0.316   (64K / 202,752 — the MINIMUM_CONTEXT_LENGTH floor)
MAX_THRESHOLD = 0.70
```

And a hardcoded constant:
```python
CONTEXT_LENGTH = 202752    # glm-5.3 resolved context length
```

**Problems:**
1. `CONTEXT_LENGTH = 202752` is wrong — glm-5.3 has 1M context, and
   glm-5.2 has 200K. Neither is 202,752.
2. `MIN_THRESHOLD = 0.316` is `64000 / 202752` — if context_length
   changes to 1M, the floor becomes `64000 / 1000000 = 0.064`, which
   changes the entire control law dynamics.
3. The `implied_turns_to_compaction` calculation uses `CONTEXT_LENGTH`
   directly.

### 6.2 Updated Control Law with Dynamic Context Length

**Replace the hardcoded `CONTEXT_LENGTH` with a dynamic read:**

```python
def get_current_context_length() -> int:
    """Read the current context_length from config.yaml."""
    import yaml
    config_path = Path.home() / ".hermes" / "profiles" / "manager" / "config.yaml"
    try:
        cfg = yaml.safe_load(config_path.read_text())
        ctx = cfg.get("model", {}).get("context_length")
        if isinstance(ctx, int) and ctx > 0:
            return ctx
    except Exception:
        pass
    return 200000  # Safe fallback (glm-5.2)
```

**Dynamic bounds:**

```python
MINIMUM_CONTEXT_LENGTH = 64000  # From agent/model_metadata.py

def compute_threshold_bounds(context_length: int) -> tuple[float, float]:
    """Compute MIN_THRESHOLD and MAX_THRESHOLD relative to context_length."""
    min_threshold = MINIMUM_CONTEXT_LENGTH / context_length
    max_threshold = 0.70  # Always 70% — don't let context exceed 70% of window
    # Ensure min < max (relevant for very small context_lengths)
    if min_threshold >= max_threshold:
        min_threshold = max_threshold * 0.5
    return min_threshold, max_threshold
```

**Updated control law:**

```python
def compute_threshold(g_estimate: float, current_threshold: float,
                      context_length: int) -> float:
    """Compute adjusted threshold from growth rate estimate."""
    min_threshold, max_threshold = compute_threshold_bounds(context_length)
    delta = K_SENSITIVITY * (G_BASELINE - g_estimate)
    new_threshold = current_threshold + delta
    return max(min_threshold, min(max_threshold, new_threshold))
```

### 6.3 Impact on Control Law Dynamics

The context_length dramatically changes the control law's behavior:

| context_length | MIN_THRESHOLD | Floor (tokens) | MAX_THRESHOLD | Ceiling (tokens) |
|----------------|---------------|----------------|---------------|-----------------|
| 131,072 (current config) | 0.488 | 64,000 | 0.70 | 91,750 |
| 200,000 (glm-5.2) | 0.320 | 64,000 | 0.70 | 140,000 |
| 1,000,000 (glm-5.3) | 0.064 | 64,000 | 0.70 | 700,000 |

**With 1M context (glm-5.3):**
- The floor is very low (0.064) — the growth governor has a wide range
  to maneuver
- At baseline growth (g=1800), the theoretical optimal threshold is
  `(17500 + √(2·17500·1800)) / 1000000 = 0.025` — well below the floor
- The floor (0.064) dominates → threshold is pinned at 0.064 → compaction
  fires at 64K tokens
- This means **with 1M context, the compressor fires at 64K minimum** —
  which is very aggressive. The growth governor's main value is raising
  the threshold ABOVE the floor for sparse sessions.

**With 200K context (glm-5.2):**
- MIN_THRESHOLD = 0.320 → floor at 64K
- At 0.40 threshold → compaction at 80K
- The governor has moderate room to maneuver

**With 131K context (current broken config):**
- MIN_THRESHOLD = 0.488 → floor at 64K
- At 0.60 threshold → compaction at 78K
- Very narrow range — governor is nearly inert

### 6.4 The K_SENSITIVITY Recalculation

The control law sensitivity `K_SENSITIVITY = 0.00004` was tuned for
`CONTEXT_LENGTH = 202752`. With dynamic context_length, the threshold
*percentage* has different semantics:

- At 202K: threshold 0.40 → 81K tokens → ~45 turns at g=1800
- At 1M: threshold 0.40 → 400K tokens → ~222 turns at g=1800
- At 200K: threshold 0.40 → 80K tokens → ~35 turns at g=1800

The sensitivity should scale with context_length to keep the *token-level*
behavior consistent:

```python
def compute_k_sensitivity(context_length: int) -> float:
    """Scale K so that the token-level threshold adjustment is consistent."""
    # Reference: at 200K context, K=0.00004 gives a 0.13 swing for g=10000
    # We want the same token-level swing at any context_length
    reference_ctx = 200000
    return K_SENSITIVITY_BASE * (reference_ctx / context_length)
```

At 1M context: K = 0.00004 × (200000/1000000) = 0.000008
At 200K context: K = 0.00004 × (200000/200000) = 0.00004 (unchanged)
At 131K context: K = 0.00004 × (200000/131072) = 0.000061

### 6.5 Updated Implied Turns Calculation

```python
def implied_turns_to_compaction(threshold: float, context_length: int,
                                 g_estimate: float) -> int:
    """Estimate turns until compaction fires."""
    trigger_tokens = max(int(context_length * threshold), MINIMUM_CONTEXT_LENGTH)
    return int((trigger_tokens - C0) / max(g_estimate, 1))
```

### 6.6 Integration with Cost Governor

The cost governor (`compression_cost_governor.py`) does NOT use
context_length directly — it tracks cost ratio and outputs a threshold
+ budget. The threshold it outputs is a *percentage*, not token count.

**Composition concern:** Both the cost governor and growth governor
output threshold percentages. The growth governor composes by adding
a delta to the cost governor's threshold. With dynamic context_length,
the composition still works because both operate in percentage space.

However, the cost governor's bounds are hardcoded:
```python
COMPRESSION_THRESHOLD_MIN = 0.4
COMPRESSION_THRESHOLD_MAX = 0.85
```

These should NOT change with context_length — they're about cost
optimization, not context capacity. The growth governor's bounds (which
ARE about context capacity) change with context_length. The composition
is: growth governor adjusts within its own bounds, then the final value
is clamped to the cost governor's bounds.

Actually, looking more carefully: the cost governor writes to
`compression_threshold_override.json` which is consumed by the model
router for budget, and the growth governor writes to `config.yaml` via
`hermes config set compression.threshold`. These are different output
paths and don't conflict.

### 6.7 Complete Updated Growth Governor Pseudocode

```python
#!/usr/bin/env python3
"""Context-growth-rate Kalman governor with dynamic context_length."""

# ... imports ...

# --- Constants that DON'T scale with context_length ---
C0 = 17500                         # Manager fixed prefix (tokens)
MINIMUM_CONTEXT_LENGTH = 64000     # From model_metadata.py
G_BASELINE = 1800                  # Measured average growth rate
G_MIN = 200
G_MAX = 20000
K_SENSITIVITY_BASE = 0.00004       # Sensitivity at 200K reference context
FALLBACK_THRESHOLD = 0.40
HYSTERESIS = 0.02
MAX_THRESHOLD = 0.70

# --- Dynamic context_length ---
def get_current_context_length() -> int:
    """Read context_length from config.yaml (updated by detector)."""
    # ... reads config.yaml model.context_length ...
    return ctx_len  # fallback: 200000

def compute_k_sensitivity(context_length: int) -> float:
    return K_SENSITIVITY_BASE * (200000 / context_length)

def compute_min_threshold(context_length: int) -> float:
    min_t = MINIMUM_CONTEXT_LENGTH / context_length
    return min(min_t, MAX_THRESHOLD * 0.5)  # ensure min < max

def compute_threshold(g_estimate, current_threshold, context_length):
    k = compute_k_sensitivity(context_length)
    min_t = compute_min_threshold(context_length)
    delta = k * (G_BASELINE - g_estimate)
    new_threshold = current_threshold + delta
    return max(min_t, min(MAX_THRESHOLD, new_threshold))
```

---

## 7. Safety Bounds and Fallback Logic

### 7.1 Detector Safety

| Scenario | Detector Action |
|----------|----------------|
| DB missing or empty | Leave config unchanged, log warning |
| DB query fails | Leave config unchanged, log error |
| Model not in registry | Leave config unchanged, log warning |
| `hermes config set` fails | Leave config unchanged, log error |
| Detected model same as config | No-op (no config write) |
| Proxy unreachable (probe fallback fails) | Leave config unchanged |
| 413 errors in last hour for current model | Set to 90% of registry value |

### 7.2 Governor Safety

| Scenario | Governor Action |
|----------|----------------|
| context_length read fails | Use fallback 200000 |
| context_length < 64000 | Use 64000 (MINIMUM_CONTEXT_LENGTH) |
| context_length > 2,000,000 | Cap at 2M (sanity check) |
| Growth measurement fails | Use g_baseline, no threshold change |
| `hermes config set` fails | Leave config unchanged |
| Threshold within hysteresis of current | No-op |

### 7.3 Backward Compatibility

**If the detector is removed or fails to run:**
- Config.yaml retains whatever `context_length` was last set
- The agent reads it at session start as before
- Everything works as it did before the detector was installed

**If the registry JSON is missing:**
- Detector logs a warning and leaves config unchanged
- The growth governor falls back to reading config.yaml directly

**If the growth governor is removed:**
- The cost governor continues to operate independently
- Config's `compression.threshold` stays at whatever was last set

### 7.4 413 Error Safety Valve

If `zai_usage.db` shows 413 (payload too large) errors for the current
model in the last hour:

```python
def check_413_pressure(db_path, model, hours=1):
    """Check if recent 413 errors suggest context_length is too high."""
    cutoff = time.time() - hours * 3600
    conn = sqlite3.connect(str(db_path))
    count = conn.execute(
        "SELECT COUNT(*) FROM api_calls WHERE model=? AND status_code=413 AND ts >= ?",
        (model, cutoff)
    ).fetchone()[0]
    conn.close()
    return count

# In detector:
if check_413_pressure(DB_PATH, detected_model) > 3:
    # Reduce to 90% of registry value
    context_length = int(registry_value * 0.90)
    log_warning(f"413 errors detected — reducing context_length to 90%: {context_length}")
```

---

## 8. Implementation Plan

### Phase 1: Context Length Detector (MVP)

**Files to create:**

| File | Purpose |
|------|---------|
| `~/.hermes/bot/context_length_detector.py` | Main detector script — queries DB, looks up registry, updates config |
| `~/.hermes/bot/model_context_registry.json` | Model → max context mapping |
| `~/.hermes/bot/context_length_state.json` | Detector state (last detection, history) |

**Files to modify:**

| File | Change |
|------|--------|
| `~/.hermes/profiles/manager/cron/jobs.json` | Update comp-gov cron to chain: `context_length_detector.py && compression_cost_governor.py && compression_growth_governor.py` |

**No Hermes core changes needed.** Uses existing `hermes config set` CLI.

### Phase 2: Growth Governor Integration

**Files to modify:**

| File | Change |
|------|--------|
| `~/.hermes/bot/compression_growth_governor.py` | Replace hardcoded `CONTEXT_LENGTH` with `get_current_context_length()`, dynamic K_SENSITIVITY, dynamic MIN_THRESHOLD |

**No new files.**

### Phase 3: Observability

**Files to create:**

| File | Purpose |
|------|---------|
| `~/.hermes/bot/context_length_health.py` | Health check — shows detected model, context_length, detection source, history |

### 8.1 `context_length_detector.py` — Detailed Design

```python
#!/usr/bin/env python3
"""Dynamic context_length auto-detection for Hermes Agent.

Detects which model the z.ai proxy is actually serving (from zai_usage.db)
and updates model.context_length in config.yaml to match the model's
maximum supported context window.

Runs BEFORE the compression governors (chained in same cron slot).

State: ~/.hermes/bot/context_length_state.json
Registry: ~/.hermes/bot/model_context_registry.json
Output: hermes config set model.context_length <value>

Fallback: On any failure, leaves config.yaml unchanged.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from pathlib import Path

import yaml

BOT_DIR = Path.home() / ".hermes" / "bot"
DB_PATH = BOT_DIR / "zai_usage.db"
STATE_FILE = BOT_DIR / "context_length_state.json"
REGISTRY_FILE = BOT_DIR / "model_context_registry.json"
CONFIG_PATH = Path.home() / ".hermes" / "profiles" / "manager" / "config.yaml"

# Safety
MAX_CONTEXT_LENGTH = 2_000_000   # Sanity cap
MIN_CONTEXT_LENGTH = 64_000      # From model_metadata.py
STALE_DB_SECONDS = 900           # 15 min — if no calls in this window, probe
FOUR13_SAFETY_RATIO = 0.90       # Reduce to 90% if 413 errors observed
FOUR13_THRESHOLD = 3             # More than 3 413s in last hour → reduce
HISTORY_MAX = 100                # Keep last 100 detection records


def load_registry() -> dict[str, int]:
    """Load model → context_length mapping from JSON file."""
    try:
        data = json.loads(REGISTRY_FILE.read_text())
        return {k: int(v) for k, v in data.items()}
    except Exception as e:
        print(f"[ctx-detector] Failed to load registry: {e}")
        return {}


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {
        "detected_model": None,
        "detected_context_length": None,
        "config_context_length": None,
        "detection_source": "none",
        "last_detected_at": 0,
        "last_config_update_at": 0,
        "detection_count": 0,
        "config_update_count": 0,
        "history": [],
    }


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def detect_model_from_db(db_path: Path) -> tuple[str | None, str]:
    """Detect the most recently served model from zai_usage.db.

    Returns (model_name, source) where source is 'zai_usage.db'.
    Returns (None, 'empty') if DB is empty or missing.
    """
    if not db_path.exists():
        return None, "db_missing"

    try:
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT model, ts FROM api_calls "
            "WHERE status_code = 200 AND model IS NOT NULL "
            "ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        conn.close()
    except Exception as e:
        print(f"[ctx-detector] DB query failed: {e}")
        return None, "db_error"

    if not row:
        return None, "db_empty"

    model, ts = row
    age = time.time() - ts
    if age > STALE_DB_SECONDS:
        return model, f"db_stale_{int(age)}s"

    return model, "zai_usage.db"


def check_413_pressure(db_path: Path, model: str, hours: int = 1) -> int:
    """Count 413 errors for this model in the last hour."""
    if not db_path.exists():
        return 0
    try:
        cutoff = time.time() - hours * 3600
        conn = sqlite3.connect(str(db_path))
        count = conn.execute(
            "SELECT COUNT(*) FROM api_calls "
            "WHERE model=? AND status_code=413 AND ts >= ?",
            (model, cutoff)
        ).fetchone()[0]
        conn.close()
        return count
    except Exception:
        return 0


def read_config_context_length() -> int | None:
    """Read current model.context_length from config.yaml."""
    try:
        cfg = yaml.safe_load(CONFIG_PATH.read_text())
        ctx = cfg.get("model", {}).get("context_length")
        if isinstance(ctx, int) and ctx > 0:
            return ctx
    except Exception as e:
        print(f"[ctx-detector] Failed to read config: {e}")
    return None


def update_config_context_length(value: int) -> bool:
    """Update model.context_length via hermes config set."""
    try:
        result = subprocess.run(
            ["hermes", "--profile", "manager", "config", "set",
             "model.context_length", str(value)],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            return True
        print(f"[ctx-detector] hermes config set failed: {result.stderr}")
        return False
    except Exception as e:
        print(f"[ctx-detector] config set exception: {e}")
        return False


def main():
    state = load_state()
    registry = load_registry()

    if not registry:
        print("[ctx-detector] Registry empty — skipping detection")
        return

    # Step 1: Detect model from DB
    model, source = detect_model_from_db(DB_PATH)

    if model is None:
        print(f"[ctx-detector] No model detected (source={source}) — leaving config unchanged")
        state["detection_source"] = source
        save_state(state)
        return

    # Step 2: Look up context_length in registry
    context_length = registry.get(model)
    if context_length is None:
        # Try case-insensitive match
        for reg_model, reg_ctx in registry.items():
            if reg_model.lower() == model.lower():
                context_length = reg_ctx
                break

    if context_length is None:
        print(f"[ctx-detector] Model '{model}' not in registry — leaving config unchanged")
        state["detected_model"] = model
        state["detection_source"] = f"registry_miss"
        save_state(state)
        return

    # Step 3: Safety checks
    context_length = max(MIN_CONTEXT_LENGTH, min(context_length, MAX_CONTEXT_LENGTH))

    # 413 safety valve
    four13_count = check_413_pressure(DB_PATH, model)
    if four13_count > FOUR13_THRESHOLD:
        original = context_length
        context_length = int(context_length * FOUR13_SAFETY_RATIO)
        print(f"[ctx-detector] {four13_count} 413 errors in last hour — "
              f"reducing {original} → {context_length}")

    # Step 4: Compare with current config
    current_config_ctx = read_config_context_length()

    if current_config_ctx == context_length:
        # Already correct — no update needed
        state["detected_model"] = model
        state["detected_context_length"] = context_length
        state["config_context_length"] = current_config_ctx
        state["detection_source"] = source
        state["last_detected_at"] = time.time()
        state["detection_count"] += 1
        save_state(state)
        print(f"[ctx-detector] model={model} ctx={context_length} — already correct")
        return

    # Step 5: Update config
    applied = update_config_context_length(context_length)

    state["detected_model"] = model
    state["detected_context_length"] = context_length
    state["config_context_length"] = context_length if applied else current_config_ctx
    state["detection_source"] = source
    state["last_detected_at"] = time.time()
    if applied:
        state["last_config_update_at"] = time.time()
        state["config_update_count"] += 1
    state["detection_count"] += 1

    # Append to history
    state["history"].append({
        "ts": time.time(),
        "model": model,
        "ctx_len": context_length,
        "source": source,
        "applied": applied,
        "previous": current_config_ctx,
    })
    if len(state["history"]) > HISTORY_MAX:
        state["history"] = state["history"][-HISTORY_MAX:]

    save_state(state)

    tag = "UPDATED" if applied else "FAILED"
    print(f"[ctx-detector] model={model} ctx={context_length} "
          f"previous={current_config_ctx} [{tag}] source={source}")


if __name__ == "__main__":
    main()
```

### 8.2 Test Plan

| Test | Description | How to verify |
|------|-------------|---------------|
| Unit: registry load | Load valid JSON, verify all entries | `python3 -c "from context_length_detector import load_registry; r=load_registry(); assert r['glm-5.3']==1000000"` |
| Unit: DB detection | Point at test DB with known model, verify detection | Create temp DB, insert row with model='glm-5.2', verify detection |
| Unit: 413 check | Create test DB with 413 rows, verify count | Insert 5 rows with status_code=413, verify check_413_pressure returns 5 |
| Integration: config set | Run detector, verify config.yaml updated | `hermes --profile manager config get model.context_length` before/after |
| Fallback: DB missing | Delete DB path, run detector | Should print "db_missing", no crash, no config change |
| Fallback: registry missing | Delete registry JSON, run detector | Should print "Registry empty", no crash |
| Fallback: model not in registry | Insert unknown model in DB | Should print "registry_miss", no config change |
| Fallback: hermes CLI missing | Mock subprocess failure | Should print error, not crash |
| Hysteresis | Run twice with same DB state | Second run should be "already correct" |
| End-to-end: model switch | Insert glm-5.2 in DB, run detector, verify config changes to 200000 | Check config before/after |
| Growth gov: dynamic ctx | Update config to 1M, run growth governor, verify K and MIN_THRESHOLD scale | Check governor output |

### 8.3 Cron Integration

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
  "command": "python3 /home/c03rad0r/.hermes/bot/context_length_detector.py && python3 /home/c03rad0r/.hermes/bot/compression_cost_governor.py && python3 /home/c03rad0r/.hermes/bot/compression_growth_governor.py"
}
```

**Execution order:**
1. `context_length_detector.py` — detects model, updates `model.context_length` in config
2. `compression_cost_governor.py` — tracks cost ratio, adjusts threshold + budget
3. `compression_growth_governor.py` — tracks growth rate, reads fresh `context_length`, adjusts threshold

---

## 9. Integration with Existing Growth Governor Design

### 9.1 What Changes in the Growth Governor

The growth governor design in `kalman-compaction-frequency-design.md` has
the following changes:

| Component | Before (hardcoded) | After (dynamic) |
|-----------|-------------------|-----------------|
| `CONTEXT_LENGTH` | `202752` (constant) | `get_current_context_length()` (reads config.yaml) |
| `MIN_THRESHOLD` | `0.316` (64K/202752) | `MINIMUM_CONTEXT_LENGTH / context_length` (computed) |
| `MAX_THRESHOLD` | `0.70` (constant) | `0.70` (unchanged — absolute cap) |
| `K_SENSITIVITY` | `0.00004` (constant) | `K_SENSITIVITY_BASE × (200000 / context_length)` (scaled) |
| `implied_turns_to_compaction` | Uses hardcoded `CONTEXT_LENGTH` | Uses dynamic `context_length` |
| `FALLBACK_THRESHOLD` | `0.40` (constant) | `0.40` (unchanged — if ctx read fails, 200K assumption) |

### 9.2 What DOESN'T Change

- The Kalman filter model (state, process noise, measurement noise)
- The growth rate measurement logic (median of positive deltas per session)
- The composition logic with the cost governor
- The hysteresis check
- The `hermes config set compression.threshold` output mechanism
- The anti-thrashing protection
- The fallback cascade

### 9.3 Data Flow (Updated)

```
┌─────────────────────────────────────────────────────────────────────┐
│                     zai_usage.db (api_calls)                        │
│  ts | session_id | prompt_tokens | model | status_code | ...       │
└──────────┬──────────────────────────┬──────────────────────────────┘
           │                          │
           ▼                          ▼
┌──────────────────────┐    ┌──────────────────────────────────────┐
│ context_length_      │    │ compression_growth_governor.py        │
│ detector.py          │    │                                      │
│                      │    │ Kalman filter on context growth       │
│ 1. Detect model      │    │ rate (tokens/call)                   │
│ 2. Lookup registry   │    │                                      │
│ 3. hermes config set │   │ Reads config.yaml for current         │
│    model.ctx_length  │    │ context_length (updated by detector)  │
└────────┬─────────────┘    │                                      │
         │                  │ Dynamic K_SENSITIVITY and MIN_THRESH  │
         ▼                  │ based on context_length              │
┌──────────────────────┐    │                                      │
│ config.yaml          │    │ Control law → threshold adjustment   │
│ model.context_length │◄───┤ hermes config set compression.thresh │
│ compression.threshold│    │                                      │
└──────────────────────┘    └──────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Agent Session Start                                                │
│  Reads config.yaml → initializes ContextCompressor                  │
│  context_length = config value (now dynamically updated)            │
│  threshold_tokens = max(ctx_len × threshold, 64000)                │
└─────────────────────────────────────────────────────────────────────┘
```

### 9.4 Risk: Detector and Governor Race

**Scenario:** Detector updates `model.context_length` at T=0. Growth
governor runs at T=1 (same cron tick, chained with `&&`). Growth governor
reads config.yaml — will it see the new value?

**Answer: Yes.** `hermes config set` writes to config.yaml synchronously.
The growth governor reads config.yaml with `yaml.safe_load()` — it gets
the file as it exists on disk at read time. The `&&` chaining ensures
sequential execution. No race.

### 9.5 Risk: Context Length Changes Affect Active Sessions

**Scenario:** Detector changes context_length from 1M to 200K while a
session is active with 500K tokens of context.

**Impact on active session:** None directly. The `ContextCompressor`
instance was initialized at session start with `context_length=1M`. The
config change doesn't propagate to the running compressor.

**What happens next API call:** The proxy is now serving glm-5.2 (200K).
The agent sends 500K tokens. z.ai returns 413. Agent triggers
error-recovery compaction (up to 3 retries). Context is compressed to
fit under 200K.

**What happens at next session start:** The new session reads
`context_length=200000` from config and initializes the compressor
correctly.

**This is acceptable** — the 413 recovery path handles the gap, and the
next session is correctly configured. The alternative (propagating
mid-session) would require core changes to the agent's HTTP client.

---

## 10. Open Questions and Future Enhancements

### 10.1 `X-Served-Model` Header Integration (Future)

The proxy already emits `X-Served-Model` and `X-Downgrade-Reason` headers
on model rewrites. A future enhancement could:

1. Have the agent's HTTP client capture these headers
2. Compare served model vs configured model
3. If different, look up context_length from registry
4. Call `context_compressor.update_model()` with the new context_length
5. This enables **instant mid-session context_length adaptation** — no
   need to wait for the next session start or the 15-minute cron tick

**Complexity:** Requires modifying the agent's HTTP response handler
(core code change). Out of scope for the cron-based governor.

### 10.2 Registry Auto-Update (Future)

A future cron job could query z.ai's API documentation or models.dev
to automatically update `model_context_registry.json` when new models
are released. This would eliminate the need for manual registry edits.

### 10.3 Proxy `/v1/models` Enhancement (Future)

If the proxy's `/v1/models` endpoint included `context_length` in each
model entry, the agent's built-in `_resolve_endpoint_context_length()`
would detect it automatically — no config override needed. This is a
one-line change in `zai_proxy.py`:

```python
def _m(mid, owner):
    return {"id": mid, "object": "model", "created": now,
            "owned_by": owner, "sats_pricing": dict(_sp),
            "context_length": MODEL_CONTEXT_LENGTHS.get(mid, 200000)}
```

With this change, removing `model.context_length` from config.yaml
would let the agent auto-detect via the endpoint probe. However, the
`DEFAULT_CONTEXT_LENGTHS` dict in `model_metadata.py` would still need
a `"glm-5.3"` entry (it currently falls through to `"glm": 202752`).

### 10.4 Removing the Config Override (Future)

Once the detector has run at least once and set `context_length` to the
correct value, the config override could be removed to let the agent's
built-in detection work. But this requires:
1. Adding `"glm-5.3": 1000000` to `DEFAULT_CONTEXT_LENGTHS` in
   `model_metadata.py`
2. Adding `context_length` to the proxy's `/v1/models` response
3. Testing that the full detection chain works end-to-end

For now, the detector + config override approach is simpler and doesn't
require core code changes.

---

## 11. Summary

| Aspect | Decision |
|--------|----------|
| **Model detection** | Query `zai_usage.db` for most recent served model (zero cost, real-time) |
| **Context length mapping** | External JSON registry (`model_context_registry.json`) |
| **Config update** | `hermes config set model.context_length <value>` (same pattern as governors) |
| **Schedule** | Every 15 minutes, chained before compression governors |
| **Fallback** | If detection fails, leave config unchanged (backward compatible) |
| **Mid-session** | Config changes take effect at next session start; 413 recovery handles the gap |
| **Growth governor** | Replace hardcoded `CONTEXT_LENGTH` with `get_current_context_length()` |
| **K sensitivity** | Scale with `200000 / context_length` to keep token-level behavior consistent |
| **MIN_THRESHOLD** | Compute as `64000 / context_length` (dynamic floor) |
| **413 safety** | If >3 413 errors in last hour, reduce to 90% of registry value |
| **LLM cost** | Zero (cron, no_agent=true, pure computation) |
| **Core changes** | None — uses existing CLI + config mechanism |
| **Files created** | 3 (detector script, registry JSON, state JSON) |
| **Files modified** | 2 (growth governor script, cron job JSON) |