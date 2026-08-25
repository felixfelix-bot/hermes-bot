# ADR-0012: Key-state transition alerts with full situation overview

**Date:** 2026-08-25
**Status:** ACCEPTED

## Context

There was no detection of available→unavailable key transitions. `_mark_key_failure` overwrote `_zai_key_health[name]` in place without comparing to the previous state. Existing anomaly logging fired on every failure (noisy, not transition-specific). The user had no visibility into why traffic was routing to expensive providers when cheap providers should have been available.

## Decision

1. **Track health transitions**: `_key_down_since[name]` records when a key first goes unhealthy. `_key_alerted[name]` prevents duplicate alerts per down period.

2. **Recovery tracking**: `_mark_key_healthy` clears `_key_down_since` and fires an INFO `KEY_RECOVERED` anomaly.

3. **15-minute sustained-down check** in `_refresh_loop` (every 5 min): for any key down ≥900s (15 min) and not yet alerted, fire a `CRITICAL` `KEY_SUSTAINED_DOWN` anomaly.

4. **Comprehensive overview** in the alert body:
   - Today's burn per provider (daily_spend table)
   - Last 1h burn rate per provider (api_calls table: calls, tokens, latency)
   - Key states: healthy/unhealthy/missing, with failure type, backoff, failure count
   - Quota/allowance pressure: session/weekly used_pct for ollama, allowance_remaining for Go
   - Effective routing chains for common models (glm-5.2, deepseek-v4-flash, kimi-k3)
   - Action hints: rotate dead keys, check transient failures, wait for quota reset

5. **Three delivery channels**:
   - `_log_anomaly("CRITICAL", "KEY_SUSTAINED_DOWN", ...)` → `anomaly_events` table → `anomaly-notify.sh` cron → human-gate digest
   - `print(overview, flush=True)` → journald (immediate)
   - `espeak-ng "Alert: key X unavailable for N minutes"` → voice notification

6. **Fires once per down period** (tracked by `_key_alerted`). Resets when the key recovers.

## Consequences

- The user is notified within ~5-15 minutes when a key goes down and stays down
- The overview shows the full picture: what's burning, what's down, what's absorbing traffic, what to do
- No alert spam: one alert per down period, recovery anomaly on restore
- Voice notification ensures the user hears it even if not watching the terminal