# Dox / Identity-Association Log (Provider Keys & Endpoints)

**Purpose:** Track, per API key and endpoint, whether it is associated with a doxable identity — @embedsmart.de email or a GitHub profile — and the resulting routing decision. The opposite of "clean": UNKNOWN association = treat as doxable (exclude to be safe), per operator directive.

**Critical rule (opsec):** NEVER write a raw API key, secret, or full email into this log. Use key NAME (e.g. `ollama_cloud_4`) + a redacted suffix (last 4 chars) only. This file must be committable.

**Maintained by:** manager profile, opsec consultant (deleg_3653c5e6).

## Identity association status

| Provider key | Email assoc. | GitHub assoc. | Status | Evidence / source |
|---|---|---|---|---|
| `ollama_cloud` | UNKNOWN | UNKNOWN | **EXCLUDED** (operator: don't wire key #1) | operator-directed 2026-09-08 |
| `ollama_cloud_2` | UNKNOWN | UNKNOWN | **EXCLUDED** (UNKNOWN → safe) | zai_proxy.py L710 comment; no email documented |
| `ollama_cloud_3` | UNKNOWN (alias stoic_herschel_499) | UNKNOWN | **EXCLUDED** (UNKNOWN → safe) | zai_proxy.py L711, .env:516-518 |
| `ollama_cloud_4` | **YES** (`...@embedsmart.de`) | UNKNOWN | **EXCLUDED** (doxed) | zai_proxy.py L716 (ollama4@embedsmart.de), .env:568 |
| `openrouter` (re-enabled 2026-09-06) | **YES** (`felix@embedsmart.de`) | **YES** (`felixfelix-bot`) | **EXCLUDED** (doxed + ToS-resale-banned) | zai-proxy-management/references/telnyx-openrouter-api-details.md:81 |
| `telnyx` | **YES** (`telnyx@embedsmart.de`) | UNKNOWN | **EXCLUDED** (operator, 2026-09-08 — account-doxed to embedsmart per audit) | telnyx-inference-provider.md:128, telnyx-openrouter-api-details.md:15; operator-directed |
| `deepinfra` | No (audit) | No (audit) | **EXCLUDED** (operator override — believes doxed despite audit CLEAN; operator decision is authoritative) | audit deleg_3653c5e6 (CLEAN); operator override 2026-09-08 |
| `deepseek` | No | No | **CLEAN — WIRE-IN** (operator: "no need to exclude deepseek") | audit; operator 2026-09-08 |
| `chutes` | UNKNOWN (alias solar_pearl_672) | UNKNOWN (embedsmart signup provenance unverified) | **LOW-RISK** — WIRE-IN | .env:563; audit flags provenance |
| `neuralwatt` | No | No | **CLEAN — WIRE-IN** | audit |
| `ppq` | No | No | **CLEAN — WIRE-IN** (key dead/disabled) | PPQ_API_KEY disabled in .env |
| `opencode_go` | No | No | **CLEAN — WIRE-IN** | audit |
| `ours` / `friend` (z.ai) | No | No | **CLEAN — WIRE-IN** (resale-allowed for ops) | audit; operator policy 2026-09-07 |

## Nostr identities (routstr sell lane + publisher)
| Name | Association | Verdict |
|---|---|---|
| routstrd sell-node npub (`npub10p6yvm…`) | Isolated, NOT linked to main identity | **CLEAN (compartmented)** |
| Kalman pricing publisher npub (`npub1q2pk0674…`) | committed to `felixfelix-bot/merchant-routing-engine` | **DOXED-TO-GITHUB** (publishes routing telemetry + npub → correlated to GitHub profile) |
| Operator main npub (`npub1ftjlarsn…`) | in `felixfelix-bot/tollgate-infrastructure-kit` + `handover-for-franchovy-agent.md` | **DOXED-TO-GITHUB** (personal identity ↔ GitHub on public repos) |

## Endpoint association status

| Endpoint | @embedsmart.de? | GitHub? | Status |
|---|---|---|---|
| api.deepinfra.com/v1/openai | no | no | EXCLUDED (operator override) |
| api.deepseek.com | no | no | WIRE-IN |
| api.neuralwatt.com/v1 | no | no | WIRE-IN |
| api.ppq.ai/v1 | no | no | WIRE-IN |
| api.telnyx.com/v2/ai | no | no | WIRE-IN |
| api.z.ai/api/coding/paas/v4 | no | no | WIRE-IN |
| llm.chutes.ai/v1 | no | no | WIRE-IN |
| ollama.com/v1 | no | no | ollama keys EXCLUDED anyway (identity above) |
| opencode.ai/zen/go/v1 | no | no | WIRE-IN |
| openrouter.ai/api/v1 | no | no | EXCLUDED (operator + ToS) |

*Endpoints are all standard public provider APIs — none embed an @embedsmart.de or GitHub identity in the URL itself. Identity risk lives in the ACCOUNT (key) metadata, not the endpoint URL.*

## Decisions
- **Exclude to be safe (rule):** any key with UNKNOWN email association is treated as doxable and excluded, per operator directive (avoid doxed endpoints). Reversible via `rm` of the disable flag — but the dox status is permanent knowledge.
- **Re-inclusion:** only with a documented, verified non-dox status. Never re-include a confirmed-doxed key.
- **OPERATOR OVERRIDE 2026-09-09 (Felix, in-chat): re-enabled doxed lanes for INTERNAL use.** All `.key_disabled_ollama_cloud{,_2,_3,_4}`, `.key_disabled_openrouter`, `.key_disabled_telnyx` flags removed + proxy restarted (verified candidates: ollama lanes #1 for glm-5.2/5.3, openrouter candidate for deepseek). Rationale: free included quota (ollama 96% headroom) was idle while ~$40/day flowed to PAYGO. **CONSTRAINT:** these lanes are INTERNAL-ONLY. They must stay OUT of the routstr/sold chain (public customers). The caller-class sold gate does NOT yet filter doxed lanes (only quota-exhaustion 429s) — a doxed-lane allowlist filter in select_provider (plan C3, inference-routing-remediation-2026-09-09.md) is REQUIRED before any sold traffic may flow again. Until C3 lands, routstr public node should not serve through these lanes.
- **This log is the durable source of truth** for provisioning new keys: a new key flagged UNKNOWN-email goes into `EXCLUDED` until proven non-doxed.

---
*Populated 2026-09-08. See deleg_3653c5e6 opsec audit for the full grounding. Never commit secrets to this file. Updated 2026-09-09 (operator internal-use override).*
- **OPERATOR ACTION 2026-09-09 (Felix): routstr-from-home tunnel TERMINATED.** `zai-proxy-reverse-tunnel.service` (autossh `-R 0.0.0.0:9099:127.0.0.1:9097` → VPS2 23.182.128.51), `tag-sidecar.service` (9097 sold-tagging), and the crash-looping `routstr-forward-tunnel.service` were all `stop --now` + `disable`d. Home IP no longer serves public/sold traffic. Residual risk closed: doxed lanes (re-enabled internally) can no longer be reached by sold traffic through the tunnel. C3 (doxed-lane sold-filter in select_provider) remains required as defense-in-depth before ANY routstr re-enable.

- **OPERATOR ACTION 2026-09-09 (Felix): ollama pool list RESTORED.** Root cause of the "ollama 0% traffic" outage: the 2026-09-08 P0-1 edit emptied `_OLLAMA_CLOUD_KEYS` in zai_proxy.py (flags were rm'd but the list was never restored). Restored all 4 entries (ollama_cloud, _2, _3, _4) + proxy restarted. Verified: end-to-end probe returns X-Provider=ollama_cloud_4; shadow optimizer prices all 4 lanes ~$0.0155/M; deepseek-flash/pro + glm-5.3 flowing through oc4. Same constraint as before: INTERNAL-ONLY — C3 sold-filter + dead reverse tunnel keep these doxed lanes out of the routstr/sold chain.
