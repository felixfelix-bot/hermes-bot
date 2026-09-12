# routstrd ↔ zai_proxy routing loop — root cause, fix, evidence

**Date:** 2026-09-12/13 · **Where:** CobradorWave (`zai-proxy.service`, `routstrd.service`)
**Status:** FIXED live in `~/.hermes/bot/zai_proxy.py`; guard verified; bleed stopped.

## Symptom

`zai_usage.db` (`~/.hermes/bot/zai_usage.db`, table `api_calls`) showed the same
request logged many times per second, attributed to the `routstrd` upstream:

- worst group: **35 identical calls inside 0.7 s** (same 67,627-token prompt,
  same 67,584 cached tokens, every one HTTP 200, each 90–180 s long)
- one hour: **561 extra duplicate calls of 1655 (34 %)**, 104 duplicate groups
- estimated cost **$11.07/hr**, 192.5M tok/hr (see caveat below)
- 4-core / 7 GB host pushed to 971 MB free + 9.6 GB swap → the token-bleed
  guard tripped its ESTOP (`.dispatch_frozen`), which in turn blocked *all*
  kanban dispatch (PAE-7/8/9 sat idle)

The duplicates are **inbound** requests (the proxy logs one row per inbound
request, all 200), so this was never a proxy retry-after-failure.

## Root cause — a two-node loop

Two configs pointed at each other:

1. `~/.hermes/bot/zai_proxy.py` registers the local routstr node as an
   **upstream provider**:
   `"routstrd": {"base_url": "http://localhost:8008/v1", ...}`
2. `~/.routstrd/config.json` lists the proxy in routstrd's provider pool:
   `"staticProviders": ["http://localhost:9099"]`
   and `src/daemon/http/index.ts` has a **"localhost provider passthrough"**
   (`ZAIPROXY_BASE = "http://localhost:9099"`) that forwards requests straight
   back to the proxy, bypassing Cashu payment.

So one buyer request became:

```
buyer → routstrd(:8008) → zai_proxy(:9099) → routstrd(:8008) → zai_proxy(:9099) → …
```

with each hop billed and logged. routstrd copies incoming headers, so a marker
header round-trips and re-entry is detectable.

## Fix (deployed)

In `zai_proxy.py`:

- `ZAI_HOP_HEADER = "X-Zai-Router-Hop"`, `ZAI_MAX_HOPS` (env `ZAI_MAX_HOPS`,
  default 2).
- `Handler._proxy()` reads the inbound hop count and **hard-rejects
  `hop >= ZAI_MAX_HOPS`** with `429 {"type":"routing_loop"}` + `Retry-After: 5`.
- `_try_external_single()` and `_try_external_failover()` **never select the
  `routstrd` upstream for a re-entrant request** (`hop >= 1`).
- every outbound provider call is stamped `X-Zai-Router-Hop: hop+1`
  (7 call sites).

Legitimate traffic is untouched: a first bounce (`hop == 1`) is still served,
so routstrd's passthrough feature keeps working — the loop just cannot close.

## Evidence (measured)

| | before | after |
|---|---|---|
| duplicate calls / window | 227 extra of 584 (39 %) | **0 of 238 (0 %)** |
| burn rate | 192.5M tok/hr, $11.07/hr | **58.0M tok/hr, $0.24/hr** |
| memory | free 971 MB, swap 9.6 GB | free 1526 MB, swap 8.3 GB |

Live guard test — `scripts/verify-loop-guard.sh` (3/3 PASS):

```
PASS re-entrant hop=2 refused (429 routing_loop)
PASS first bounce hop=1 still served (200)
PASS normal request unaffected (200)
```

## Caveats / follow-ups

- Cost numbers in `api_calls` are `rate_derived_fallback` / `cached_rate_derived`
  (our estimates, not provider-reported). 98 % of the loop's prompt tokens were
  cache hits, so the **dollar** figure overstates real spend; the call count,
  duration and memory pressure are measured facts.
- `.dispatch_frozen` and the bleed guard's `frozen: true` were **not** cleared
  by hand — the guard needs clean streaks, and its `efficiency`/`bloat`
  detectors were still breaching when the loop was cut.
- `zai_proxy.py` in this repo is **behind the live file** (~830 lines of local
  drift). The guard is published as `patches/routstrd-loop-guard.patch` against
  the live file; the drift itself is a separate cleanup task.
- Optional hardening in routstrd: skip the localhost passthrough when the
  inbound request already carries `X-Zai-Router-Hop >= 1`.
- `inflight_dedup.py` (same branch) is an independent, default-OFF safety net
  that sheds duplicate *callers* (any origin), not just this loop.
