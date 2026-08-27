# FR-0 Findings — before-state evidence (t_39c85a31, 2026-08-27)

Direct answers to the six task questions. All "verified" claims are backed by
live HTTP probes saved under `evidence/` (raw JSON, no keys).

## Q1. PROVIDER_MODELS snapshot
`~/.hermes/bot/registry_before_2026-08-27.json` — 12 providers, 75
provider×model rows, AST-extracted verbatim (no import side effects),
source sha256 + git commit `efd6344fa066` recorded in `_meta`.

## Q2. Ollama catalog (ollama.com/api/tags, BOTH cloud keys, HTTP 200, identical 18-model catalog)
- **glm-5.3 does NOT exist on Ollama. PHANTOM CONFIRMED.** Catalog has only
  `glm-5.1`, `glm-5.2`, `glm-5.3-flash`. Registry lists glm-5.3 for
  ollama_cloud(+2); it does not 404 only because `_PROVIDER_MODEL_NAMES`
  silently downgrades it to `glm-5.2` at dispatch (zai_proxy.py:698) —
  responses are mislabeled glm-5.2, not glm-5.3.
- **kimi-k3:cloud does NOT exist** — actual tag is `kimi-k3` (no suffix).
  No translation entry → dispatched verbatim → 404.
- **minimax-m3:cloud does NOT exist** — actual tag is `minimax-m3`.
  No translation entry → verbatim → 404.
- **deepseek/deepseek-v4-flash:0731 — the real tag is `deepseek-v4-flash:0731`**
  (dotted + dated, NO slash prefix). Registry's slashed form works only via
  dispatch translation. Same for `deepseek-v4-pro:0813`.
- glm-4.5-flash: **not on Ollama at all** (registry entry phantom, verbatim 404).
- Verified present: glm-5.2, kimi-k2.7-code, gpt-oss:120b, gemma4:31b, qwen3.5:397b.
- 6 verbatim-406/404 paths total (3 models × 2 keys) — see fact_table.json.

## Q3. OpenCode Go catalog (opencode.ai/zen/go/v1/models, HTTP 200, 31 models)
- Bare `deepseek-v4-flash` ✓ and `deepseek-v4-pro` ✓ confirmed (this is why
  the crash funneled 100% of deepseek traffic here).
- `glm-5.3` ✓ native (comment at zai_proxy.py:4786 correct).
- glm-5.2 ✓, kimi-k3 ✓, kimi-k2.7-code ✓.
- Registry ALSO lists slashed `deepseek/deepseek-v4-*` for opencode_go — not
  in their catalog verbatim; works only via translation map (duplication, not breakage).
- Note: default urllib UA gets Cloudflare 1010; must send `User-Agent: Mozilla/5.0`.

## Q4. /v1/models endpoint (zai_proxy.py:6794-6826)
HARDCODED STUB confirmed live (HTTP 200 on 127.0.0.1:9099, byte-identical to code).
Advertises exactly 8 models: glm-5.3, glm-5.2, glm-4.5-flash, glm-4.5-air,
kimi-k2.7-code, kimi-k3:cloud, kimi-k3, minimax-m3:cloud.
- **ZERO deepseek models advertised** (workers can't discover deepseek at all).
- Advertises `kimi-k3:cloud` + `minimax-m3:cloud` — tags that no longer exist
  on Ollama (stale since Ollama re-tagging).
- Registry models NOT advertised: glm-4.5, kimi-k2.5, gpt-5, claude-haiku-4-5,
  minimax-m3, kimi-k3:cloud(routable via telnyx)... full list in fact_table.json.

## Q5. Lookup behavior (grep-verified)
- `select_provider` (flat_router.py:798-807): `model_id = model or "glm-5.2"`;
  candidate filter is `if model_id not in models: continue` — **exact string
  membership only, no normalization, no alias layer**. `deepseek-v4-flash`
  matches ONLY opencode_go → single point of failure confirmed (crash RCA valid).
- `_resolve_model_for_provider` (flat_router.py:856-870): resolves
  `_PROVIDER_MODEL_NAMES` from zai_proxy, returns `mapping[model]` else
  verbatim fallback. Confirmed.
- `_PROVIDER_MODEL_NAMES` (zai_proxy.py:652-707): 8 provider keys — deepinfra,
  telnyx, openrouter, ppq, opencode_go, neuralwatt, ollama_cloud, ollama_cloud_2.
  No keys for ours/friend/routstr/routstrd (verbatim passthrough, matches proxy IDs).

## Q6. Fact table
`FR0_FACT_TABLE.md` / `fact_table.json` — 75 rows (model × provider ×
dispatch-native-name × catalog status). Verified-live coverage: ollama×2,
opencode_go; other providers marked "listed (not probed)" per task scope.

## Impact on FR-1/FR-2 (for the next worker)
1. Plan's alias table needs revision: ollama native names are
   `kimi-k3`/`minimax-m3`/`deepseek-v4-flash:0731`/`deepseek-v4-pro:0813`,
   NOT the `:cloud` forms the plan assumed.
2. Remove/downgrade-map: ollama `glm-5.3` (phantom, currently silent-downgrade),
   ollama `glm-4.5-flash` (phantom, 404).
3. /v1/models stub must eventually advertise canonical IDs (FR-3 audit input).
