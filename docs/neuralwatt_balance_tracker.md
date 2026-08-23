# NeuralWatt Real-API Balance Tracker

> **Status:** Production (replaces self-tracking approach, 2026-08-23)
> **Module:** `src/balance_collectors.py` → `NeuralWattBalance`, `collect_neuralwatt_balance()`
> **Proxy bridge:** `zai_proxy.py` → `_neuralwatt_quota_snapshot()`, `get_neuralwatt_cost_correction_factor()`
> **Tests:** `test_neuralwatt_balance.py` (78 tests, all mocking — no live API calls)

---

## Overview

NeuralWatt (`api.neuralwatt.com`) is a per-token LLM provider that uses
**energy-based pricing** (kWh), not per-token billing. The Pro plan
($100/mo) includes a monthly energy allowance of 13.33 kWh. 94% of
prompt tokens are served from cache at a ~5× discount, which means our
local per-token cost estimate **overcounts real spend by ~5.7×**.

The previous approach (self-tracking via `zai_usage.db`) was unreliable
because of this overcounting: on 2026-08-22 the DB showed $258 spent
while the real NeuralWatt bill was only ~$45. That caused an unnecessary
daily-cap trigger and burned ~3,300 glm-5.2 calls through NeuralWatt
with no accurate guardrail.

This module replaces self-tracking with **direct calls to NeuralWatt's
real billing API** for authoritative balance, kWh allowance, and daily
spend.

---

## Real API Endpoints

### GET `/v1/quota`

Returns balance, subscription, and lifetime usage. Authenticated via
`Authorization: Bearer <NEURALWATT_API_KEY>`.

**Key fields:**

```json
{
  "snapshot_at": "2026-08-23T16:55:28Z",
  "balance": {
    "credits_remaining_usd": 8.9951,
    "total_credits_usd": 11.0,
    "credits_used_usd": 2.0049,
    "accounting_method": "energy"
  },
  "usage": {
    "lifetime": {
      "cost_usd": 51.3019,
      "requests": 4919,
      "tokens": 415717093,
      "energy_kwh": 6.8462
    }
  },
  "subscription": {
    "plan": "pro",
    "status": "active",
    "current_period_end": "2026-09-22T21:41:54Z",
    "kwh_included": 13.3333,
    "kwh_used": 6.5729,
    "kwh_remaining": 6.7604,
    "in_overage": false
  },
  "limits": { "rate_limit_tier": "pro" }
}
```

### GET `/v1/usage/summary`

Returns a 30-day rolling time series of daily spend. Used for the
daily-cap guardrail.

**Key fields:**

```json
{
  "period": { "start": "...", "end": "..." },
  "accounting_method": "energy",
  "totals": {
    "total_cost_usd": 51.326226,
    "energy_kwh_consumed": 6.849402,
    "cached_tokens": 389241856
  },
  "time_series": [
    { "date": "2026-08-23", "cost_usd": 45.600584, "requests": 4433 }
  ]
}
```

The collector scans `time_series` for today's UTC date and extracts
`cost_usd` as the real daily spend.

---

## `NeuralWattBalance` Dataclass

| Field | Type | Source | Description |
|---|---|---|---|
| `remaining_usd` | `float\|None` | `balance.credits_remaining_usd` | Prepaid credits remaining |
| `total_credits_usd` | `float\|None` | `balance.total_credits_usd` | Total funded credits |
| `kwh_used` | `float\|None` | `subscription.kwh_used` | Energy consumed this billing period |
| `kwh_remaining` | `float\|None` | `subscription.kwh_remaining` | Energy remaining in allowance |
| `kwh_included` | `float\|None` | `subscription.kwh_included` | Monthly energy allowance (13.33 kWh) |
| `usage_fraction` | `float` | derived | `clamp(kwh_used / kwh_included, 0, 1)` |
| `cost_usd` | `float\|None` | `usage.lifetime.cost_usd` | Real lifetime spend (USD) |
| `subscription_status` | `str\|None` | `subscription.status` | "active" / "canceled" / etc. |
| `period_end` | `str\|None` | `subscription.current_period_end` | ISO 8601 billing period end |
| `is_exhausted` | `bool` | derived | `in_overage == true` OR `kwh_remaining <= 0` |
| `daily_spent_usd` | `float\|None` | `/v1/usage/summary` | Real USD spent today (UTC) |
| `daily_cap_usd` | `float` | env/config | Daily spend cap (default $10/day) |
| `is_daily_cap_exceeded` | `bool` | derived | `daily_spent_usd > daily_cap_usd` (when cap > 0) |
| `collected_at` | `float` | `time.time()` | Unix timestamp of collection |
| `error` | `str\|None` | — | Error message on failure, `None` on success |
| `raw` | `dict` | full `/v1/quota` response | Stored for forensics |

**Backward-compat properties:**
- `total_spent_usd` → alias for `cost_usd` (real lifetime spend)
- `starting_usd` → alias for `total_credits_usd` (funded credit pool)
- `used_pct` → `usage_fraction * 100.0` (what the router reads)

---

## Energy-Based Pricing Model

NeuralWatt bills on **energy consumed (kWh)**, not per-token. The Pro
plan includes 13.33 kWh/month. This is fundamentally different from
providers like DeepInfra ($/M tokens) or OpenRouter (credit pool).

**`usage_fraction` is derived from kWh**, not from dollar balance:

```
usage_fraction = clamp(kwh_used / kwh_included, 0, 1)
```

When `.kwh_remaining <= 0` or `subscription.in_overage == true`, the
account is considered **exhausted** and routing should drop NeuralWatt.

---

## 94% Prompt Cache Rate & 5.7× Overcounting

NeuralWatt caches 94% of prompt tokens at a ~5× discount. The
`/v1/usage/summary` response shows `cached_tokens: 389,241,856` out of
`prompt_tokens: 412,669,187` — that's 94.3% cache hit rate.

Our local per-token cost estimate (`_estimate_cost_usd` in `zai_proxy.py`)
uses published per-token rates without cache awareness. This means:

| Metric | Our DB (per-token) | Real API (energy) | Ratio |
|---|---|---|---|
| 2026-08-22 to 23 spend | $258 | $45.60 | 5.66× |
| Lifetime spend | $258+ | $51.33 | ~5× |

The correction factor `1 / 5.7 ≈ 0.226` brings the local estimate in
line with the real NeuralWatt bill.

---

## Cost Correction Factor

### `get_neuralwatt_cost_correction_factor()`

```python
def get_neuralwatt_cost_correction_factor(
    usage_db_path: Optional[str] = None,
    api_key: Optional[str] = None,
    *,
    refresh: bool = False,
) -> float
```

Computes the ratio of **real API total spend** to **our DB-tracked
spend sum**:

```
factor = real_api_total_cost_usd / db_sum_cost_usd_for_neuralwatt_tier
```

- **Real total** comes from `GET /v1/usage/summary` →
  `totals.total_cost_usd`
- **DB sum** comes from `SELECT SUM(cost_usd) FROM api_calls WHERE
  tier='neuralwatt'` in `zai_usage.db`

The factor is clamped to `[0, 1]` — if the API reports more than our
DB (undercounting), the factor stays at `1.0` (never scale up).

**Caching:** Result is cached for 10 minutes
(`_NEURALWATT_COST_CORRECTION_TTL = 600.0`). Pass `refresh=True` to
bypass.

**Env override:** `NEURALWATT_COST_CORRECTION=0.226` pins a constant
value (range `[0, 1]`). Takes precedence over the API path.

### Where it's applied

In `zai_proxy.py` → `_estimate_cost_usd()`:

```python
nw_correction = 1.0
if _neuralwatt_cost_correction_fn is not None:
    nw_correction = float(_neuralwatt_cost_correction_fn() or 1.0)
# ... per-token estimate × nw_correction
```

This scales every NeuralWatt cost_usd record written to the DB so the
spend logger matches the real bill within ~5%.

---

## Daily Spend Cap Enforcement

The daily cap is a **runaway-burn guardrail** that prevents another
$258-in-one-day incident. It works with the real API spend, not the
overcounted DB value.

### How it works

1. `collect_neuralwatt_balance()` fetches today's real spend from
   `/v1/usage/summary.time_series[today].cost_usd`
2. If `daily_spent_usd > daily_cap_usd` (and cap > 0), then
   `is_daily_cap_exceeded = True`
3. In `zai_proxy.py` → `_neuralwatt_quota_snapshot()`, when the cap is
   exceeded, `used_pct` is overridden to `100.0` and `regime` =
   `"daily-capped"`. This causes the routing layer to treat NeuralWatt
   as exhausted and drop it from rotation until UTC midnight.
4. In `_snapshot_health()`, `h["neuralwatt"]` is set to `False` when
   the daily cap is exceeded, marking the key unhealthy.

### Configuration

| Setting | Env Var | Default | Description |
|---|---|---|---|
| Daily cap | `NEURALWATT_DAILY_CAP` | `10.0` (USD/day) | Set to `0` to disable |
| API key | `NEURALWATT_API_KEY` | — | Required for API calls |
| Cost correction override | `NEURALWATT_COST_CORRECTION` | — | Pin a factor in `[0, 1]` |

Precedence: explicit function arg → env var → default.

---

## How the Proxy Uses the Balance

### `snap["neuralwatt"]` in `_snapshot_quota()`

The proxy's `_snapshot_quota()` function builds a dict of all provider
quota states. For NeuralWatt:

```python
snap["neuralwatt"] = _neuralwatt_quota_snapshot()
```

`_neuralwatt_quota_snapshot()` calls `neuralwatt_quota_entry()` which
reads the latest stored balance from `api_burn.db` →
`provider_balances` table. If the row is fresh (`< 20 min` old), it
returns:

```python
{
    "used_pct": 49.30,          # kwh_used / kwh_included * 100
    "remaining": 6.7604,        # kWh remaining
    "total": 13.3333,           # kWh allowance
    "is_exhausted": False,      # kwh_remaining <= 0 or in_overage
    "is_daily_cap_exceeded": False,
    "daily_spent_usd": 45.60,
    "daily_cap_usd": 10.0,
    "collected_at": 1724434728.0,
    # ... dashboard fields
}
```

When the daily cap is exceeded, `used_pct` is forced to `100.0` and
`regime = "daily-capped"`.

**Cold-start fallback:** If the bridge is disabled or no fresh row
exists, the snapshot returns `{used_pct: 0.0, remaining: inf}` — the
pre-bridge optimistic behavior — so routing never breaks.

### `h["neuralwatt"]` in `_snapshot_health()`

When the daily cap is exceeded, the key is marked **unhealthy** so the
router drops NeuralWatt until UTC midnight (when the time_series
resets).

---

## Operator Playbook

### Check current NeuralWatt balance

```bash
# CLI — collect once and print JSON
cd ~/.hermes/bot && python3 -m src.balance_collectors --provider neuralwatt

# Or from Python
python3 -c "
from src.balance_collectors import collect_neuralwatt_balance
b = collect_neuralwatt_balance()
print(f'Remaining: \${b.remaining_usd:.2f}')
print(f'kWh: {b.kwh_used:.2f} / {b.kwh_included:.2f} ({b.used_pct:.1f}%)')
print(f'Daily: \${b.daily_spent_usd:.2f} / \${b.daily_cap_usd:.2f}')
print(f'Cap exceeded: {b.is_daily_cap_exceeded}')
"
```

### View the latest stored snapshot (from cron collection)

```bash
python3 -c "
from src.balance_collectors import neuralwatt_quota_entry
import json; print(json.dumps(neuralwatt_quota_entry(), indent=2, default=str))
"
```

### Change the daily spend cap

```bash
# Temporary (this session only)
export NEURALWATT_DAILY_CAP=5.0

# Permanent — add to manager .env
echo 'NEURALWATT_DAILY_CAP=5.0' >> ~/.hermes/profiles/manager/.env

# Disable cap entirely
export NEURALWATT_DAILY_CAP=0
```

### Override the cost correction factor

If the API is down or you want to pin a known ratio:

```bash
# Known good ratio (1/5.7 ≈ 0.226)
export NEURALWATT_COST_CORRECTION=0.226
```

### Run the collector via cron

The cron entry calls:

```bash
python3 -m src.balance_collectors --provider neuralwatt --db ~/.hermes/bot/api_burn.db
```

This calls `collect_and_store_neuralwatt()` which:
1. Resolves the API key from `NEURALWATT_API_KEY`
2. Calls `GET /v1/quota` for balance + kWh allowance
3. Calls `GET /v1/usage/summary` for today's spend
4. Stores the snapshot in `api_burn.db` → `provider_balances` table
5. Prints JSON status and exits 0/1

### Verify the proxy is using the real API

Check the startup log for:

```
[neuralwatt] balance bridge loaded — quota_state['neuralwatt'] reads real /v1/quota API
[neuralwatt] cost-correction bridge loaded — _estimate_cost_usd will scale to real API spend
```

If you see `DISABLED`, check that:
- `NEURALWATT_API_KEY` is set in the `.env`
- `src/balance_collectors.py` is importable (synced to
  `~/merchant-routing-engine/src/` if `sys.path` prepends that dir)
- No import errors in the startup log

### The `$258 incident` — what happened

On 2026-08-22, the proxy's self-tracking showed $258 spent on
NeuralWatt (overcounted 5.7× due to prompt-cache discounts), while the
real spend was ~$45. The hardcoded guardrail saw infinite remaining
balance and allowed ~3,300 unthrottled glm-5.2 calls. The real-API
collector eliminates this by reading the authoritative spend directly
from NeuralWatt's billing endpoints.

---

## Architecture Notes

### `sys.path` Note

`zai_proxy.py` inserts `~/merchant-routing-engine` at the front of
`sys.path` (line ~33-34). This means `src.balance_collectors` is
imported from `~/merchant-routing-engine/src/balance_collectors.py`,
**not** from `~/.hermes/bot/src/balance_collectors.py`.

After modifying `~/.hermes/bot/src/balance_collectors.py`, always sync:

```bash
cp ~/.hermes/bot/src/balance_collectors.py ~/merchant-routing-engine/src/balance_collectors.py
```

### Failure Modes (Revert-Safe)

All NeuralWatt bridge code in the proxy is revert-safe:

| Failure | Behavior |
|---|---|
| No `NEURALWATT_API_KEY` | `error="...not set"`, numeric fields stay `None` |
| `/v1/quota` HTTP error | `error` set, balance not stored, cold-start fallback |
| `/v1/usage/summary` fails | Daily spend = `None`, cap stays open (not triggered) |
| Import bridge disabled | `snap["neuralwatt"] = {used_pct:0.0, remaining:inf}` (old behavior) |
| DB read error | `neuralwatt_quota_entry()` returns `{}` → cold-start fallback |
| Cost correction fails | Factor defaults to `1.0` (no scaling) |

### Test Coverage

78 tests in `test_neuralwatt_balance.py`:

- `TestNeuralWattHttpHelper` — constants and missing-key
- `TestCollectFromRealApi` — quota parsing, usage fraction, daily spend
- `TestFallbackOnError` — quota/summary failure paths
- `TestDailyCapEnforcement` — cap exceeded, env override, zero cap
- `TestStoreAndGetLatest` — round-trip persistence
- `TestNeuralWattQuotaEntry` — bridge to quota_state
- `TestCollectAndStore` — cron end-to-end
- `TestCostCorrectionFactor` — env override, caching, clamping
- `TestNeuralWattCli` — CLI dispatcher
- `TestUsageFractionPure` — pure helper unit tests
- `TestTransportEdgeCases` — `_neuralwatt_http_get` error branches
- `TestStorageEdgeCases` — DB corruption, missing paths
- `TestDefaultUsageDbPath` — env resolution

All HTTP is mocked — no live API calls in tests.
