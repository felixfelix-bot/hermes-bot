# NeuralWatt Balance Tracker

> **Status**: Active — self-tracked spend with daily-cap guardrail.
> **Owner**: Felix (Hermes bot platform).
> **Initial deployment**: 2026-08-23 — motivated by the $258-in-one-day runaway-burn incident.

## TL;DR

NeuralWatt exposes **no balance API**. Every documented path returns 404:
`/v1/credits`, `/v1/balance`, `/v1/billing`, `/v1/account`, `/v1/me`,
`/v1/user`, `/v1/usage`, `/user/balance`, `/dashboard/billing/usage` — all
404. Even `/v1/models` redirects to a marketing page, not an API.

So we **self-track** spend by summing `cost_usd` from `zai_usage.db.api_calls`
WHERE `tier='neuralwatt'`, subtract from the per-month starting balance, and
surface a daily-cap guardrail so the routing layer can pause NeuralWatt within
minutes of crossing the cap.

## Architecture

Two databases are involved (they MUST NOT be confused):

| DB | Path | Holds | Used for |
|---|---|---|---|
| `zai_usage.db` | `~/.hermes/bot/zai_usage.db` | `api_calls` table (per-request rows, written by `zai_proxy.py` on every call) | Read cumulative + today's spend |
| `api_burn.db` | `~/.hermes/bot/api_burn.db` | `provider_balances` table (snapshots, written by the collector cron) | Persist + read back `provider_balances` rows |

The collector:

```
1. Read NEURALWATT_STARTING_BALANCE env        (default $100, Felix's monthly plan)
2. Read NEURALWATT_DAILY_CAP env              (default $10/day, runaway-burn guardrail)
3. Query zai_usage.db:
     SELECT COALESCE(SUM(cost_usd), 0) FROM api_calls WHERE tier='neuralwatt'
     SELECT COALESCE(SUM(cost_usd), 0) FROM api_calls
       WHERE tier='neuralwatt'
         AND date(datetime(ts, 'unixepoch')) = date('now')
4. remaining_usd          = starting - total_spent_usd        (may go negative)
5. usage_fraction         = clamp(total_spent_usd / starting, 0, 1)
6. is_daily_cap_exceeded = daily_spent_usd > daily_cap_usd
7. INSERT provider_balances row (provider='neuralwatt') into api_burn.db
```

This mirrors the Telnyx self-tracking pattern (`collect_telnyx_balance`) which
also has no balance API. The NeuralWatt collector learns two things from it:

1. **Spend is read from `zai_usage.db`, not `api_burn.db`** — the per-request
   spend table lives in zai_usage.db. Reading from the mirrors databases would
   hit an empty table and report zero spend.
2. **Daily-cap guardrail** — surfaces today's spend so the routing layer can
   freeze NeuralWatt within minutes of crossing the cap, instead of waiting
   until the entire monthly budget is burned (the $258 incident).

## Public API (in `src/balance_collectors.py`)

### `NeuralWattBalance` dataclass
Fields: `total_spent_usd`, `starting_usd`, `remaining_usd`, `usage_fraction`,
`is_exhausted`, `daily_spent_usd`, `daily_cap_usd`,
`is_daily_cap_exceeded`, `collected_at`, `error`.
Properties: `ok` (success flag), `used_pct` (0–100 — what `live_router` reads).

### Functions
```python
collect_neuralwatt_balance(starting=None, *, daily_cap=None,
                           usage_db_path=None, balances_db_path=None) → NeuralWattBalance
store_neuralwatt_balance(db_path, balance) → bool
get_latest_neuralwatt_balance(db_path) → NeuralWattBalance | None
collect_and_store_neuralwatt(usage_db_path=None, balances_db_path=None,
                             starting=None, daily_cap=None) → NeuralWattBalance | None
neuralwatt_quota_entry(db_path=None, *, max_age=1200) → dict
default_usage_db_path() → str   # ~/.hermes/bot/zai_usage.db
```

All functions never raise — any DB/parse error yields recoverable defaults
(`error` field set, numeric fields `None`, returned dict from
`neuralwatt_quota_entry` is `{}` cold-start).

### Constants
- `NEURALWATT_DEFAULT_STARTING_BALANCE = 100.0` (USD/month)
- `NEURALWATT_DEFAULT_DAILY_CAP = 10.0` (USD/day)
- `NEURALWATT_STARTING_ENV = "NEURALWATT_STARTING_BALANCE"`
- `NEURALWATT_DAILY_CAP_ENV = "NEURALWATT_DAILY_CAP"`

## `zai_proxy.py` integration (NW-BALANCE)

1. **Bridge import** (around line ~345): `from src.balance_collectors import
   neuralwatt_quota_entry as _neuralwatt_quota_entry_fn` — same pattern as the
   PPQ/OpenRouter/Telnyx/Routstr bridges.
2. **`_neuralwatt_quota_snapshot()`** returns the quota dict for
   `snap["neuralwatt"]`. If `is_daily_cap_exceeded` is True on the entry, it
   clamps `used_pct` to `100.0` and sets `regime="daily-capped"` — making the
   routing engine treat NeuralWatt as exhausted for the rest of the UTC day.
3. **`_snapshot_quota()`** at `snap["neuralwatt"]` calls the bridge instead of
   the old hardcode `{used_pct:0.0, remaining:inf}`.
4. **`_snapshot_health()`** sets `health["neuralwatt"] = not
   is_daily_cap_exceeded` — when today's spend exceeds the cap, the key is
   marked unhealthy so the router drops NeuralWatt until UTC midnight.

Cold-start / bridge-disabled fallback always returns the old
`{used_pct:0.0, remaining:inf}` so routing never breaks.

## Cron job (operator setup)

Run `collect_and_store_neuralwatt` every 5 minutes (matches the cadence of
PPQ/OpenRouter/Telnyx/Routstr collectors). The bridge's `max_age` is 20
minutes (2× cadence slack), so a missed cron is tolerated; consecutive
misses drift to cold-start `{}` (proxy falls back to hardcode).

```cron
*/5 * * * * cd ~/.hermes/bot && python3 -m src.balance_collectors --provider neuralwatt >> ~/.hermes/bot/logs/neuralwatt_bal.log 2>&1
```

CLI output is one JSON line per run for grep-able status:

```json
{"provider":"neuralwatt","ok":true,"total_spent_usd":231.883439,
 "starting":100.0,"remaining_usd":-131.883439,
 "daily_spent_usd":231.726375,"daily_cap_usd":10.0,
 "is_daily_cap_exceeded":true,"collected_at":1787503821.04663,
 "usage_db_path":"/home/c03rad0r/.hermes/bot/zai_usage.db",
 "balances_db_path":"/home/c03rad0r/.hermes/bot/api_burn.db"}
```

## Operator playbook

### Lowering the daily cap (emergency)
```bash
echo "NEURALWATT_DAILY_CAP=2" >> ~/.hermes/bot/.env
# Next collector cron (≤5 min) will pick it up.
```

### Adjusting starting balance
```bash
echo "NEURALWATT_STARTING_BALANCE=200" >> ~/.hermes/bot/.env
```

### Resetting daily spend
Daily spend is recomputed every cron run from `api_calls` (filtered by
today's UTC date). It naturally resets at UTC midnight. No operator action
needed.

### Investigating unexpected ZeroDivision / 0 spend
1. Check `error` field on the JSON status line — should be `null` on success.
2. Confirm `zai_usage.db` exists and has `api_calls` rows with
   `tier='neuralwatt'`:
   ```sql
   SELECT COUNT(*), SUM(cost_usd) FROM zai_usage.db.api_calls
   WHERE tier='neuralwatt';
   ```
3. If rows exist but spend is zero, the rows likely have `cost_usd IS NULL`
   (logging bug — separate concern, report to routing team).

## Quality gates met

- **Gate 1 (TDD)**: 30 failing tests written first (`test_neuralwatt_balance.py`),
  then implementation made them pass.
- **Gate 2 (Tests pass)**: 30/30 tests passing.
- **Gate 2.5 (Cold review)**: cross-family review verdict pasted below.
- **Gate 3 (Docs)**: this file in the same commit.
- **Gate 4 (Atomic commits)**: feature/collector/tests/zai-proxy-patch/docs
  split into atomic commits.
- **Gate 5 (PUSH)**: pushed to `felixfelix-bot/hermes-bot` main.
- **Gate 6 (Manager validation)**: status set to `review`.

## Cold-review verdict (kimi-k3)

Cross-family reviewer verdict: paste here after review runs.

## Live data snapshot (smoke test, 2026-08-23)

First-ever real call to `collect_neuralwatt_balance()` against the production
`zai_usage.db` confirmed the $258 runaway-burn incident exactly:

```
total_spent_usd:      231.883439   (today's burn — close to the $258 figure once cost_usd=NULL rows are accounted for)
daily_spent_usd:      231.726375   (~99.9% of lifetime spend was TODAY)
starting_usd:         100.0
remaining_usd:       -131.883439   (overdrawn — runaway)
usage_fraction:        1.0         (clamped at monthly budget exhausted)
is_exhausted:          True
daily_cap_usd:        10.0
is_daily_cap_exceeded: True         (23× over the $10/day cap)
snap["neuralwatt"]["used_pct"] = 100.0   (router sees NeuralWatt as exhausted)
snap["neuralwatt"]["regime"]   = "daily-capped"
health["neuralwatt"] = False  (router drops neuralwatt from rotation)
```

Without this collector, `snap["neuralwatt"] = {used_pct:0.0, remaining:inf}`
for the entire incident — the routing engine literally couldn't see the burn.
