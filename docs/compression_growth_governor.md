# Compression Growth Governor

A Kalman-filter-driven governor that dynamically adjusts
`compression.threshold` based on context growth rate (tokens/call).

## Overview

The growth governor complements the existing `compression_cost_governor.py`.
Where the cost governor tracks the **cost ratio** of compression vs. total API
spend, the growth governor tracks how fast context is **growing** and adjusts
the compaction threshold accordingly:

- **Dense sessions** (high growth rate, e.g. 10,000 tokens/call) → lower
  threshold → compact sooner → less wasted tokens per call.
- **Sparse sessions** (low growth rate, e.g. 200 tokens/call) → raise
  threshold → compact later → preserve context longer, better quality.

## Architecture

```
zai_usage.db ───────────────► measure_growth_rate()
                                    │
                                    ▼
                              GrowthRateKalman.update()
                                    │
                                    ▼
                              compute_threshold(g, ctx_len)
                                    │
                                    ▼
                              apply_threshold()  ──►  hermes config set compression.threshold
                                    │
                                    ▼
                              State + Audit JSON
```

### Key design decisions

1. **Separate governor** — not an extension of `compression_cost_governor.py`.
   The cost governor tracks a 0-D cost-ratio signal; the growth governor tracks
   a 1-D growth-rate signal with different dynamics. Mixing would require a 2-D
   Kalman filter and coupled measurement model — unnecessary complexity.

2. **Dynamic context_length** — read from `config.yaml` at runtime, NOT
   hardcoded. When the model switches (e.g. glm-5.3 → glm-5.2 with different
   context windows), the threshold floor (`64000 / context_length`)
   automatically adjusts.

3. **Absolute control law** — `threshold = FALLBACK + K × (G_BASELINE − g)`.
   The same growth rate always produces the same threshold, regardless of
   history. Hysteresis in `apply_threshold()` prevents churn when g is near
   baseline.

4. **Chained after cost governor** — runs in the same 15-minute cron slot
   but after the cost governor, composing (not clobbering) its output.

5. **Multi-profile** — iterates over ALL profiles in `~/.hermes/profiles/`.
   The growth rate is measured **globally** from `zai_usage.db` (which does
   not track per-profile sessions); the threshold computation is per-profile
   because each profile can have a different `context_length`. Profiles are
   discovered via `discover_profiles()` and logged individually.

## Multi-Profile Support

The governor now manages ALL profiles, not just `manager`:

- **Discovery**: `discover_profiles()` returns all profile directories under
  `~/.hermes/profiles/` that contain `config.yaml`.
- **Per-profile threshold**: Each profile's `context_length` and current
  `compression.threshold` are read from its own `config.yaml`. The growth
  rate (Kalman estimate) is shared globally, but the computed threshold
  differs per profile because `MIN_THRESHOLD = 64000 / context_length`
  depends on each profile's context length.
- **Application**: `apply_threshold(new_value, config_path, profile_name)`
  calls `hermes --profile <name> config set compression.threshold <value>`
  for each profile individually.
- **Audit**: `_write_audit()` records the profile name in the audit JSON.
- **Skipped profiles**: Profiles without `config.yaml` are logged to stderr
  and included in the output as skipped entries.

## Files

| File | Purpose |
|------|---------|
| `compression_growth_governor.py` | Main governor: Kalman filter, control law, multi-profile CLI |
| `compression_growth_state.json` | Persisted Kalman state (auto-created on first run) |
| `compression_growth_override.json` | Audit record of threshold decisions |
| `compression_growth_health.py` | Health check / status report |
| `test_compression_growth_governor.py` | Test suite (39 tests, 94% coverage) |

## Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `WINDOW_HOURS` | 6 | Lookback window for growth measurement |
| `G_BASELINE` | 1800 | Measured average growth rate (tokens/call) |
| `G_MIN` / `G_MAX` | 200 / 20000 | Clamp bounds for growth estimate |
| `K_SENSITIVITY` | 0.00004 | Control law gain |
| `FALLBACK_THRESHOLD` | 0.40 | Base threshold; also used on any failure |
| `MAX_THRESHOLD` | 0.70 | Upper clamp for threshold |
| `MIN_THRESHOLD` | `64000 / context_length` | Dynamic floor (MINIMUM_CONTEXT_LENGTH) |
| `HYSTERESIS` | 0.02 | Minimum delta to trigger config change |

## Kalman Filter Parameters

| Parameter | Value | Description |
|-----------|-------|-------------|
| `x0` | 1800 | Initial state estimate |
| `P0` | 500000.0 | Initial estimate uncertainty |
| `Q` | 50000.0 | Process noise (keeps filter adaptive) |
| `R` | 300000.0 | Measurement noise (high variance in per-call growth) |

The `predict()` step adds Q to P (random walk). The `update(measurement)` step
computes the Kalman gain and corrects the state estimate, then clamps to
`[G_MIN, G_MAX]`.

## Control Law

```
threshold = FALLBACK_THRESHOLD + K_SENSITIVITY × (G_BASELINE − g_estimate)
```

Clamped to `[MIN_THRESHOLD, MAX_THRESHOLD]` where
`MIN_THRESHOLD = 64000 / context_length`.

### Example outputs (context_length=200000, MIN=0.32)

| Growth rate | Description | Threshold |
|-------------|-------------|-----------|
| 200 | Sparse (Q&A) | 0.464 |
| 1800 | Normal (baseline) | 0.400 |
| 5000 | Dense (tool-heavy) | 0.328 |
| 10000+ | Very dense | 0.320 (floor) |

## Fallback Cascade

1. DB missing or corrupt → `measure_growth_rate` returns `G_BASELINE`
2. Config missing → `read_config` returns `(131072, FALLBACK_THRESHOLD)`
3. `hermes config set` fails → config left unchanged
4. State file corrupt → defaults to initial Kalman state
5. Any exception → no crash, no config change

## Cron Integration

Chained after the cost governor in the same 15-minute slot:

```bash
python3 compression_cost_governor.py && python3 compression_growth_governor.py
```

Both run with `no_agent=true` (zero LLM cost, pure computation).

## Usage

### Manual run

```bash
python3 ~/.hermes/bot/compression_growth_governor.py
```

Output (JSON):
```json
{
  "growth_rate": 597.0,
  "kalman_estimate": 731.8,
  "old_threshold": 0.6228,
  "new_threshold": 0.4427,
  "context_length": 200000,
  "applied": true
}
```

### Health check

```bash
python3 ~/.hermes/bot/compression_growth_health.py
```

### Tests

```bash
python3 -m pytest test_compression_growth_governor.py -v --cov=compression_growth_governor
```

## See Also

- [Design Document](../../profiles/manager/state/kalman-compaction-frequency-design.md)
- `compression_cost_governor.py` — existing cost-ratio governor
- `compression_model_router.py` — consumes override file for model selection
