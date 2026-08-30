#!/usr/bin/env python3
"""catalog_drift_check.py — Periodic live-catalog probe + drift report.

Checks every API provider's LIVE model catalog against the three local
registries that can drift:
  1. flat_router.PROVIDER_MODELS      (routing candidates)
  2. zai_proxy._PROVIDER_MODEL_NAMES  (dispatch-time name translation)
  3. model_context_registry.json      (context lengths)

Drift categories:
  phantom          — in PROVIDER_MODELS but upstream doesn't list it
                     (route → 404 → key_health poison cascade)
  missing_rung     — upstream serves it, we don't route it (503 risk)
  translation_gap  — routable model missing from _PROVIDER_MODEL_NAMES
  context_gap      — routable model missing from model_context_registry.json

Outputs:
  1. Evidence snapshot  ~/.hermes/bot/evidence/catalog-drift/<date>/<provider>.json
     (FR-0 probe format: url, http_status, elapsed_s, body)
  2. Report             ~/.hermes/bot/evidence/catalog-drift/<date>/drift-report.json
  3. State file         ~/.hermes/bot/live_catalog_state.json (read by flat_router
     runtime phantom guard — fail-open if stale)
  4. Signal alert (via send-viz-signal.sh mechanism) — ONLY on new drift
     signatures (deduped vs last state)

Usage:
  catalog_drift_check.py [--report-only]   # use last snapshots, recompute report
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
BOT = HOME / ".hermes" / "bot"
EVIDENCE = BOT / "evidence" / "catalog-drift"
STATE_FILE = BOT / "live_catalog_state.json"
PREV_STATE_FILE = BOT / "live_catalog_prev_state.json"

# ── Provider probe definitions ───────────────────────────────────────────────
# key_env: manager/.env variable name  (needed for subscription-scoped truth)
# catalog_url: OpenAI-style /v1/models endpoint
# tags_url: richer metadata endpoint (ollama only)
# provider_type: 'quota' (flat sub, probe is free) | 'per_token' | 'ecash'
# catalog_complete: False → upstream listing is known-incomplete (z.ai serves
#   glm-4.6v unlisted) → runtime guard treats snapshot as advisory-only
PROVIDERS: dict[str, dict] = {
    "ollama_cloud": {
        "url": "https://ollama.com/v1/models",
        "key_env": "OLLAMA_CLOUD_API_KEY",
        "catalog_complete": True,
        "type": "quota",
    },
    "ollama_cloud_2": {
        "url": "https://ollama.com/v1/models",
        "key_env": "OLLAMA_CLOUD_API_KEY_2",
        "catalog_complete": True,
        "type": "quota",
    },
    "neuralwatt": {
        "url": "https://api.neuralwatt.com/v1/models",
        "key_env": "NEURALWATT_API_KEY",
        "catalog_complete": True,
        "type": "per_token",
    },
    "deepinfra": {
        "url": "https://api.deepinfra.com/v1/models",
        "key_env": "DEEPINFRA_API_KEY",
        "catalog_complete": True,
        "type": "per_token",
    },
    "opencode_go": {
        "url": "https://opencode.ai/zen/go/v1/models",
        "key_env": "OPENCODE_GO_API_KEY",
        "catalog_complete": True,
        "type": "quota",
    },
    "ours": {
        "url": "https://api.z.ai/api/coding/paas/v4/models",
        "key_env": "ZAI_OUR_KEY",
        "catalog_complete": False,   # glm-4.6v served-but-unlisted
        "allowlist_extra": ["glm-4.6v", "glm-4.5-flash"],  # live-verified 08-27/08-30
        "type": "quota",
    },
    "telnyx": {
        "url": "https://api.telnyx.com/v2/ai/models",
        "key_env": "TELNYX_API_KEY",
        "catalog_complete": True,
        "type": "per_token",
        "json_mode": "telnyx",
    },
    "routstr": {
        "url": "http://127.0.0.1:8009/v1/models",
        "key_env": None,
        "catalog_complete": True,
        "type": "ecash",
    },
    "routstrd": {
        "url": "http://127.0.0.1:8008/v1/models",
        "key_env": None,
        "catalog_complete": True,
        "type": "ecash",
    },
}

# Providers with known-dead keys — excluded from probing, reported as skip
SKIP_UNTIL_ROTATED = ["ppq", "openrouter", "friend"]

# ── Canonicalization (upstream ID → canonical short ID) ─────────────────────
# Reverses zai_proxy._PROVIDER_MODEL_NAMES + generic org-prefix stripping.
_ORG_PREFIXES = ("zai-org/", "z-ai/", "deepseek-ai/", "deepseek/", "moonshotai/",
                 "openai/", "anthropic-ai/", "anthropic/", "MiniMaxAI/", "nvidia/", "meta-llama/")
# Forward translation map (canonical → provider) from zai_proxy to reverse.
_TAG_TRANS = {
    "deepseek-v4-flash:0731": "deepseek/deepseek-v4-flash",
    "deepseek-v4-pro:0813":   "deepseek/deepseek-v4-pro",
}
# Canonical aliases (mirror flat_router.MODEL_ALIASES semantics)
_CANON_ALIASES = {
    "gemma4:31b": "deepseek/gemma-4-31b",     # ollama tag → canonical gemma
    "gemma-4-31b": "deepseek/gemma-4-31b",
    "kimi-k2.6": "kimi-k3",                    # K2.6 → closest canonical (report-only)
    "glm-5.3-flash": "glm-5.3-flash",          # already canonical
}


def _load_env_keys() -> dict[str, str]:
    keys: dict[str, str] = {}
    for env in (HOME / ".hermes/profiles/manager/.env", HOME / ".hermes/bot/.env",
                HOME / ".hermes/.env"):
        if not env.exists():
            continue
        for line in env.read_text(errors="ignore").splitlines():
            m = re.match(r"^([A-Z_0-9]+)=(.*)$", line.strip())
            if m:
                vals = m.group(2).split("#", 1)[0].strip().strip("'\"")
                keys.setdefault(m.group(1), vals)
    return keys


def to_canonical(raw_id: str, provider: str) -> str:
    """Map an upstream model ID to the canonical short ID used in registries."""
    mid = raw_id.strip()
    # Reverse tag translations (ollama tagged deepseek forms)
    if mid in _TAG_TRANS:
        return _TAG_TRANS[mid]
    # Reverse provider-name maps (import from zai_proxy if importable)
    try:
        sys.path.insert(0, str(BOT))
        import zai_proxy as _zp
        pmap = _zp._PROVIDER_MODEL_NAMES.get(provider, {})
        for canon, prov_id in pmap.items():
            if prov_id == mid:
                return canon
    except Exception:
        pass
    # Strip org prefixes
    changed = True
    while changed:
        changed = False
        for pref in _ORG_PREFIXES:
            if mid.startswith(pref):
                mid = mid[len(pref):]
                changed = True
    return mid


def probe(url: str, key: str | None, provider: str) -> dict:
    """Fetch a provider catalog. Returns FR-0-style probe record."""
    t0 = time.time()
    rec: dict = {"url": url, "provider": provider}
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode()
            rec["http_status"] = resp.status
        data = json.loads(body)
        ids = [m.get("id", "?") for m in data.get("data", [])]
        rec["models"] = ids
        # Pricing enrichment (best-effort: OpenAI-style pricing.prompt)
        prices = {}
        for m in data.get("data", []):
            pr = m.get("pricing") or {}
            if isinstance(pr, dict) and isinstance(pr.get("prompt"), (int, float)):
                prices[m.get("id")] = pr["prompt"]
        if prices:
            rec["pricing"] = prices
    except Exception as e:
        rec["http_status"] = None
        rec["error"] = str(e)
        rec["models"] = []
    rec["elapsed_s"] = round(time.time() - t0, 2)
    return rec


def main() -> int:
    now = time.time()
    keys = _load_env_keys()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    outdir = EVIDENCE / stamp
    outdir.mkdir(parents=True, exist_ok=True)

    live: dict[str, dict] = {}
    for name, cfg in PROVIDERS.items():
        if name in SKIP_UNTIL_ROTATED:
            live[name] = {"probe_status": "skipped_dead_key", "canonical": []}
            continue
        key = keys.get(cfg["key_env"]) if cfg.get("key_env") else None
        rec = probe(cfg["url"], key, name)
        # Save snapshot (FR-0 format)
        snap = dict(rec)
        snap["fetched_at"] = datetime.now(timezone.utc).isoformat()
        (outdir / f"{name}.json").write_text(json.dumps(snap, indent=2))

        if rec.get("http_status") == 200 and rec.get("models"):
            canonical = sorted({to_canonical(i, name) for i in rec["models"]})
            live[name] = {
                "probe_status": "ok",
                "raw_count": len(rec["models"]),
                "canonical": canonical,
                "catalog_complete": cfg["catalog_complete"],
                "allowlist_extra": cfg.get("allowlist_extra", []),
                "pricing": rec.get("pricing", {}),
            }
        else:
            live[name] = {
                "probe_status": rec.get("http_status") or "error",
                "canonical": [],
                "error": rec.get("error", "empty catalog"),
            }

    # ── Registry comparison ─────────────────────────────────────────────────
    sys.path.insert(0, str(BOT))
    try:
        from flat_router import PROVIDER_MODELS
    except Exception as e:
        print(f"FATAL: cannot import flat_router: {e}", file=sys.stderr)
        return 2
    try:
        import zai_proxy as zp
        name_maps = zp._PROVIDER_MODEL_NAMES
    except Exception:
        name_maps = {}

    # Context registry
    ctx_reg: dict[str, int] = {}
    try:
        ctx_reg = {str(k): int(v) for k, v in
                   json.loads((BOT / "model_context_registry.json").read_text()).items()}
    except Exception:
        pass

    drift = {"phantoms": {}, "missing_rungs": {}, "translation_gaps": {}, "context_gaps": {}}
    for name, cfg in PROVIDER_MODELS.items():
        live_entry = live.get(name, {})
        if live_entry.get("probe_status") != "ok":
            continue
        raw_set = set(live_entry.get("canonical", []))
        extra = set(live_entry.get("allowlist_extra", []))
        routed = cfg
        if live_entry.get("catalog_complete"):
            phantoms = sorted(routed - raw_set - extra)
            if phantoms:
                drift["phantoms"][name] = phantoms
        missing = sorted(raw_set - set(routed))
        if missing:
            drift["missing_rungs"][name] = missing
        # Translation gaps: routable models that have no entry in the
        # provider's name map (only meaningful for providers WITH a map)
        for m in routed:
            if name in name_maps and m not in name_maps[name]:
                drift["translation_gaps"].setdefault(name, []).append(m)
        # Context gaps: routable models missing from the context registry
        cgaps = sorted(m for m in routed if m not in ctx_reg and ":"
                       not in m and "/" not in m)
        if cgaps:
            drift["context_gaps"][name] = cgaps

    # Write live_catalog_state.json (runtime phantom guard input)
    state = {"fetched_at": now, "providers": {}}
    for name, entry in live.items():
        if entry.get("probe_status") == "ok":
            state["providers"][name] = {
                "models": entry["canonical"],
                "fetched_at": now,
                "catalog_complete": entry.get("catalog_complete", True),
                "allowlist_extra": entry.get("allowlist_extra", []),
            }
    prev = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    STATE_FILE.write_text(json.dumps(state, indent=2))
    # Keep previous for drift-signature dedup
    PREV_STATE_FILE.write_text(json.dumps(prev, indent=2))

    # Drift signature: only alert when the ROUTABLE side changed vs previous
    # (i.e. our registries' relationship to upstream changed). Compare
    # missing_rungs/phantoms to the previous report, not the raw catalogs.
    prev_report = {}
    try:
        prev_reports = sorted(EVIDENCE.glob("*/drift-report.json"))
        if prev_reports and prev_reports[-1].parent != outdir:
            prev_report = json.loads(prev_reports[-1].read_text())
    except Exception:
        pass
    new_drift = (drift != prev_report)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "drift": drift,
        "is_new_vs_previous": new_drift,
        "probe_summary": {k: v.get("probe_status") for k, v in live.items()},
    }
    (outdir / "drift-report.json").write_text(json.dumps(report, indent=2))

    # Signal alert — only when drift CHANGED vs previous report
    if new_drift:
        lines = [f"🔍 CATALOG DRIFT — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"]
        for provider, ph in drift["phantoms"].items():
            lines.append(f"  ⚠️ {provider} PHANTOM (we route, upstream doesn't): {', '.join(ph)}")
        for provider, mr in drift["missing_rungs"].items():
            shown = mr[:8]
            extra_n = len(mr) - len(shown)
            lines.append(f"  💡 {provider} missing rungs ({len(mr)}): {', '.join(shown)}"
                         + (f" +{extra_n} more" if extra_n > 0 else ""))
        for provider, cg in drift["context_gaps"].items():
            shown = cg[:8]
            extra_n = len(cg) - len(shown)
            lines.append(f"  📏 {provider} context-registry gaps ({len(cg)}): {', '.join(shown)}"
                         + (f" +{extra_n} more" if extra_n > 0 else ""))
        msg = "\n".join(lines)
        try:
            subprocess.run(
                ["bash", str(HOME / ".hermes/bot/scripts/send-viz-signal.sh"),
                 "--message", msg],
                capture_output=True, timeout=60)
        except Exception:
            pass
        print(msg)
    else:
        print(f"[drift] no new drift vs previous report ({stamp})")

    # Console summary
    n_ph = sum(len(v) for v in drift["phantoms"].values())
    n_mr = sum(len(v) for v in drift["missing_rungs"].values())
    n_cg = sum(len(v) for v in drift["context_gaps"].values())
    print(f"[catalog-drift] probes={len(live)} phantoms={n_ph} "
          f"missing_rungs={n_mr} context_gaps={n_cg} new={new_drift}")
    return 0


if __name__ == "__main__":
    sys.exit(main())