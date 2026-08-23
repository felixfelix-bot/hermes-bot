# Compression Growth Governor

## Overview

`compression_growth_governor.py` implements a 1-D Kalman filter that tracks
the context growth rate (tokens/call) from `zai_usage.db` and adjusts
`compression.threshold` via `hermes config set`.

Dense sessions (high growth) lower the threshold → compaction fires sooner.
Sparse sessions (low growth) raise the threshold → context is preserved longer.

## Corrected Constants (131K context)

The design doc assumed 202,752-token context (glm-5.3). The actual context
length for glm-5.2 is **131,072**. All constants were recalculated:

| Constant | Value | Rationale |
|----------|-------|-----------|
| `CONTEXT_LENGTH` | 131072 | glm-5.2 resolved context |
| `MIN_THRESHOLD` | 0.4883 | 64000 / 131072 — the floor |
| `MAX_THRESHOLD` | 0.70 | Prevents context bloat |
| `FALLBACK_THRESHOLD` | 0.60 | Current config baseline |
| `G_BASELINE` | 1800 | Measured average growth (tokens/call) |
| `K_SENSITIVITY` | 0.00003 | Recalculated for 131K range [0.488, 0.70] |

## Control Law

```
threshold = current + K * (g_baseline - g_estimate)
```

Clamped to `[MIN_THRESHOLD, MAX_THRESHOLD]`. Hysteresis of 0.02 prevents
config churn.

## Files

| File | Purpose |
|------|---------|
| `compression_growth_governor.py` | Main governor script |
| `compression_growth_state.json` | Kalman filter state (persisted) |
| `compression_growth_override.json` | Audit record of last decision |
| `tests/test_compression_growth_governor.py` | 30 tests, 96% coverage |

## Fallback Behavior

- DB missing/corrupt → returns `G_BASELINE`, no crash
- `hermes` CLI missing → returns `False`, config unchanged
- Any exception → config left unchanged, state saved

## Cron Integration

Chained after `compression_cost_governor.py` in the same 15-minute cron slot:

```bash
python3 compression_cost_governor.py && python3 compression_growth_governor.py
```

Zero LLM cost (`no_agent=true`, pure stdlib computation).