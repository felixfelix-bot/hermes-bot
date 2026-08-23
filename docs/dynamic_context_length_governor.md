# Dynamic Context Length Governor

Detects the proxy's currently served model and sets `model.context_length`
to the appropriate maximum, enabling automatic context window optimization
without manual config edits.

## Overview

The z.ai proxy performs silent model tier rewrites (e.g., `glm-5.3` →
`glm-5.2` when quota is near exhaustion). The governor detects the
actually-served model from `zai_usage.db` and updates `context_length`
in config so the next session starts with the correct window size.

**Key properties:**
- **Zero LLM cost** — detection is purely from local SQLite, not API calls
- **Backward-compatible** — if detection fails, config is left unchanged
- **Safety-first** — never sets below 128,000 tokens; reduces to 90% on 413 pressure

## Files

| File | Purpose |
|------|---------|
| `dynamic_context_length_governor.py` | Main governor script (multi-profile) |
| `model_context_registry.json` | Model → max context length mapping |
| `dynamic_context_state.json` | Last detection result and state |
| `test_dynamic_context_length_governor.py` | Test suite |

## Multi-Profile Support

The governor iterates over ALL profiles in `~/.hermes/profiles/*/` and
applies the same context-length logic to each one. Profiles are discovered
dynamically via `discover_profiles()` — any subdirectory of
`~/.hermes/profiles/` containing a `config.yaml` is eligible.

- **Discovery**: `discover_profiles()` scans `~/.hermes/profiles/` for
  directories with `config.yaml`, returns sorted list of profile names.
- **Per-profile processing**: `process_profile(name)` runs the full
  detection → lookup → apply chain for a single profile.
- **Skipped profiles**: Directories without `config.yaml` are logged and
  skipped. Profiles where the detected model is `None` or the context
  length matches current config are no-ops.
- **Backward compat**: `main(profiles=None)` discovers all profiles
  automatically. `main(profiles=["manager"])` processes a single profile
  (useful for testing and backward compatibility).

## How It Works

### Detection Chain

1. **Primary:** Query `zai_usage.db` for the most recent `api_calls` row
   with `status_code = 200`. The `model` field contains the post-tier-rewrite
   model (what the proxy actually served).
2. **Probe fallback (optional):** If the DB is empty/missing and probing is
   enabled, send a minimal 1-token request to the proxy and read the
   response `model` field.
3. **Terminal fallback:** Return `None` — leave config unchanged.

### Context Length Lookup

Resolution order in `get_model_context_length()`:

1. **Exact match** — `"glm-5.3"` in registry → 1,000,000
2. **Prefix match** — longest registry key that is a prefix of the model
   name (e.g., `"glm-5.3"` matches `"glm-5.3-chat"`)
3. **Family fallback** — `"glm"` → 200,000, `"kimi"` → 128,000, etc.
4. **None** — no match, leave config unchanged

### 413 Safety Valve

If `zai_usage.db` shows more than 3 HTTP 413 (payload too large) errors
for the current model in the past hour, the governor reduces
`context_length` to 90% of the registry value. This provides an automatic
safety margin when the advertised context window is too aggressive.

### Config Update

Applies via `hermes --profile <name> config set model.context_length <value>`, which:
- Updates `~/.hermes/profiles/<name>/config.yaml` for each discovered profile
- Takes effect at the **next session start** (not mid-session)
- Is a no-op if the value matches the current config

## Model Context Registry

```json
{
  "glm-5.3": 1000000,
  "glm-5.2": 200000,
  "glm-4.5-flash": 128000,
  "glm-4.5-air": 128000,
  "kimi-k2.7-code": 128000,
  "kimi-k3": 128000,
  "kimi-k3:cloud": 200000
}
```

To add a new model, simply add an entry to `model_context_registry.json`.
No code changes needed.

## Cron Integration

Runs every 15 minutes, chained **before** the compression governors:

```bash
python3 dynamic_context_length_governor.py && \
python3 compression_cost_governor.py && \
python3 compression_growth_governor.py
```

The growth governor reads the fresh `context_length` value set by this
governor to compute its threshold bounds dynamically.

## Output

The governor prints JSON to stdout for cron logs:

```json
{
  "detected_model": "glm-5.2",
  "registry_ctx": 200000,
  "current_ctx": 200000,
  "new_ctx": 200000,
  "applied": false,
  "413_count": 0
}
```

## Safety Bounds

| Scenario | Action |
|----------|--------|
| DB missing or empty | Leave config unchanged |
| Model not in registry | Leave config unchanged |
| `hermes config set` fails | Leave config unchanged |
| Value matches current config | No-op (no subprocess call) |
| Value below 128,000 | Clamped to 128,000 |
| >3 413 errors in past hour | Reduce to 90% of registry value |
