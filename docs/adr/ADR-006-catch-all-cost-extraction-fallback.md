# ADR-006: Catch-All Cost Extraction Fallback

## Status

Accepted

## Date

2026-08-24

## Context

The `_extract_cost()` function in `zai_proxy.py` has specific branches for known providers (ollama_cloud, opencode_go, telnyx, neuralwatt) that compute `cost_usd` from the provider's actual billing model. Providers without a specific branch fell through to `return (None, None)` — meaning `cost_usd = NULL` in the `api_calls` table.

This caused **invisible burn**: tokens were consumed but no cost was recorded. The invisible burn detector caught:

- **routstr**: 1.25M tokens, 100% NULL cost_usd — no `_extract_cost()` branch existed.
- **routstrd, deepinfra, ppq, openrouter**: same issue — no specific branches, all NULL.
- **ppq**: 22 calls, 100% NULL (response format changed, existing branch stopped working).
- **opencode_go**: 33% NULL (streaming SSE passes `total_tokens=0`, causing the cost calculation to return None).
- **ollama_cloud_2**: 99% NULL (same streaming issue + branch not extended for the second key).

NULL costs mean:
1. The Kalman filter gets no measurement → base_rate stays at seed/estimate, never converges.
2. Daily spend tracking underreports → cost escalation alerts don't fire.
3. Profitability tracking is impossible — we don't know how much we spent.

## Decision

Add a **catch-all fallback** at the end of `_extract_cost()`:

```python
# After all specific provider branches:
# Catch-all: estimate from rate per token
cost = _rpt_rate(provider) * total_tokens / 1_000_000
source = 'rate_derived_fallback'
return (cost, source)
```

Any provider without a specific `_extract_cost()` branch now gets a cost estimate from `_rpt_rate(provider) × tokens / 1M`, where `_rpt_rate()` returns the provider's listed rate per million tokens (from the Kalman's measured or estimated rate, or from the provider's published pricing).

The cost source is tagged as `rate_derived_fallback` to distinguish it from provider-specific extraction (which uses `actual_billing`, `neuralwatt_corrected`, etc.).

## Invariants

- No provider ever has `NULL` `cost_usd` in `api_calls` — except the known streaming bug where `total_tokens = 0` (SSE streaming doesn't pass token counts). This is a separate issue (streaming SSE fix, not a cost extraction issue).
- The fallback rate is **imprecise but non-null**. It uses the Kalman's measured rate or the published rate, not the provider's actual billing API. It's a safety net, not a precision tool.
- The `rate_derived_fallback` source tag is always set, so downstream consumers can distinguish fallback costs from provider-specific costs.

## Consequences

**Positive:**
- No invisible burn for new providers. Adding a provider to the router without writing a specific `_extract_cost()` branch no longer means NULL costs.
- The Kalman filter always gets a measurement (even imprecise), allowing it to converge over time.
- Daily spend tracking and cost escalation alerts work for all providers, not just the ones with specific branches.
- The `rate_derived_fallback` source tag makes it easy to audit which costs are precise vs estimated.

**Negative:**
- Less accurate than provider-specific branches. The fallback uses a rate × tokens formula, which doesn't account for provider-specific billing models (NeuralWatt's energy-based pricing + cache discounts, Telnyx's cache-aware calculation, opencode_go's $0 marginal cost).
- The fallback rate depends on `_rpt_rate()` being available and correct. If the rate is wrong (stale, misconfigured), the cost estimate is wrong.
- The streaming SSE `total_tokens=0` bug is not fixed by this fallback — it's a separate issue where the token count itself is zero, so `rate × 0 = 0` (not NULL, but still wrong — underreports cost).

## Notes

- Implementation: commit `a41ed60` — catch-all cost extraction fallback live.
- The streaming SSE `total_tokens=0` bug remains open (known issue #1 in `pricing-handover-2026-08-24.md` §5). It causes 33% NULL for opencode_go and 99% NULL for ollama_cloud_2 — the fallback doesn't help because the token count is zero, not missing.
- Provider-specific `_extract_cost()` branches should be written for any provider where cost precision matters (e.g., NeuralWatt with its correction factor, Telnyx with cache-aware pricing). The catch-all is a safety net, not a replacement.
- Related ADRs: ADR-001 (flat router — all 12 providers need cost tracking), ADR-002 (5-tier model — T5 Kalman needs cost measurements to converge).