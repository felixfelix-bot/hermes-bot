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
# INTAKE-1: quarantine stage store (repo root, git-tracked). New upstream
# models land here as STAGED (or REJECTED for non-chat) — NOT routable, NOT
# advertised. Promotion is gated + human-batched (INTAKE-3).
INTAKE_FILE = BOT / "model_intake.json"

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
        # F4: marketplace aggregator — listing under-reports what it proxies
        # (routstrd's staticProviders include our zai_proxy). Guard must
        # fail-open for these; phantom reports stay informational.
        "catalog_complete": False,
        "type": "ecash",
    },
    "routstrd": {
        "url": "http://127.0.0.1:8008/v1/models",
        "key_env": None,
        "catalog_complete": False,   # F4: same as routstr
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
# NOTE: gemma forms intentionally NOT aliased across separators — ollama's
# native tag IS "gemma4:31b" (verbatim-served), neuralwatt's is "gemma-4-31b"
# (reverse-mapped via zai_proxy name map). Aliasing one to the other created
# a false phantom on ollama (2026-08-30 rerun).
# NOTE: kimi-k2.6 NOT aliased — ollama serves it verbatim (2026-08-30
# catalog) and PROVIDER_MODELS routes it. Aliasing it to kimi-k3 created a
# false phantom and would violate the never-substitute principle.
_CANON_ALIASES = {}


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


def _tag_trans_variants(mid: str) -> str | None:
    """Reverse tag translation for BOTH separator forms (colon + dash).

    ollama uses  deepseek-v4-flash:0731  (colon)
    routstrd uses deepseek-v4-flash-0731 (dash)
    """
    base, sep, tag = mid.partition(":")
    if not sep:
        # try dash-form: split on LAST dash
        base, _, tag = mid.rpartition("-")
        if not _looks_like_date_tag(tag):
            return None
    return _TAG_TRANS.get(mid)


def _looks_like_date_tag(tag: str) -> bool:
    """0731 / 0813 style suffix → True."""
    return bool(re.fullmatch(r"\d{4}", tag or ""))


def to_canonical(raw_id: str, provider: str) -> str:
    """Map an upstream model ID to the canonical short ID used in registries.

    Order of attempts (F1/F2 fixes — correctness before flagging):
      1. exact tag translation (colon + dash forms)
      2. exact reverse provider-name map (zai_proxy._PROVIDER_MODEL_NAMES)
      3. CASE-INSENSITIVE reverse-map match (telnyx serves Kimi-K3 vs
         canonical kimi-k3)
      4. org-prefix strip + canonical-alias check
      5. finally: lowercase passthrough if it matches an alias shape
    """
    mid = raw_id.strip()
    # 1. exact tag translation
    if mid in _TAG_TRANS:
        return _TAG_TRANS[mid]
    # Dash-form equivalence: 'deepseek-v4-flash-0731' → colon form → trans
    if "-" in mid and mid.rsplit("-", 1)[-1].isdigit() and len(mid.rsplit("-", 1)[-1]) == 4:
        base = mid.rsplit("-", 1)[0]
        colon_form = base + ":" + mid.rsplit("-", 1)[1]
        if colon_form_in := _TAG_TRANS.get(colon_form):
            return colon_form_in
    # 2./3. reverse provider-name map — exact then case-insensitive
    try:
        sys.path.insert(0, str(BOT))
        import zai_proxy as _zp
        pmap = _zp._PROVIDER_MODEL_NAMES.get(provider, {})
        # exact
        for canon, prov_id in pmap.items():
            if prov_id == mid:
                return canon
        # case-insensitive
        low = mid.lower()
        for canon, prov_id in pmap.items():
            if prov_id.lower() == low:
                return canon
    except Exception:
        pass
    # 4. org-prefix strip + alias
    changed = True
    while changed:
        changed = False
        for pref in _ORG_PREFIXES:
            if mid.startswith(pref):
                mid = mid[len(pref):]
                changed = True
    if mid in _CANON_ALIASES:
        return _CANON_ALIASES[mid]
    # Case-insensitive alias / tag fallback (e.g. 'Kimi-K3' → 'kimi-k3')
    low = mid.lower()
    for alias_src, canon in _CANON_ALIASES.items():
        if low == alias_src.lower():
            return canon
    if low in {c.lower() for c in _TAG_TRANS}:
        return _TAG_TRANS[[c for c in _TAG_TRANS if c.lower() == low][0]]
    return mid


# F5: non-chat families excluded from missing-rung reporting (they are real
# catalog entries but can never be chat-completion candidates).
_NOISE_PATTERNS = re.compile(
    r"^(BAAI/|Bria|Audio|audio|openai/whisper|whisper|.*embedding.*|.*-tts$|"
    r".*-tts-.*|.*embed$|.*encoder.*|.*reranker.*|.*guard.*|phash.*|"
    r".*-vision-exp$|.*stable-.*|.*diffusion.*|.*sdxl.*|.*flux.*)",
    re.IGNORECASE,
)
# Context-length noise: vendor IDs whose canonical form is not routable chat
_NOISE_EXACT = {"o1", "o1-pro", "o3", "o3-pro", "o3-mini", "o3-mini-high",
                "o4-mini", "o4-mini-high"}


def _is_chat_model(mid: str) -> bool:
    if mid in _NOISE_EXACT:
        return False
    return not _NOISE_PATTERNS.match(mid)


# ── INTAKE-1: quarantine stage store ────────────────────────────────────────
# New upstream models (live probe, unknown to model_context_registry.json AND
# flat_router PROVIDER_MODELS) are written to model_intake.json as STAGED.
# Non-chat modality -> status=rejected immediately. STAGED = quarantine: NOT
# in PROVIDER_MODELS, NOT in /v1/models, requests keep loud-503. Idempotent
# reruns update last_seen only.
def _load_intake() -> dict:
    if INTAKE_FILE.exists():
        try:
            return json.loads(INTAKE_FILE.read_text())
        except Exception:
            return {}
    return {}


def stage_new_models(live: dict, provider_models: dict, ctx_reg: dict,
                     now_iso: str | None = None) -> dict:
    """Stage newly-discovered upstream models into the quarantine store.

    A model is "new" iff it appears in a live (probe_status=ok) provider
    catalog AND is unknown to BOTH flat_router.PROVIDER_MODELS AND
    model_context_registry.json. Non-chat models are staged as rejected.

    Idempotent: existing entries are preserved; only last_seen advances.
    Returns the full updated store (also persisted to INTAKE_FILE).
    """
    now_iso = now_iso or datetime.now(timezone.utc).isoformat()
    store = _load_intake()

    for provider, entry in live.items():
        if entry.get("probe_status") != "ok":
            continue
        routed = set(provider_models.get(provider, set()))
        for mid in entry.get("canonical", []):
            if mid in routed or mid in ctx_reg:
                continue  # known — not new
            rec = store.get(mid)
            if rec is None:
                chat = _is_chat_model(mid)
                store[mid] = {
                    "raw_ids": {provider: mid},
                    "modality": "chat" if chat else "non-chat",
                    "status": "staged" if chat else "rejected",
                    "first_seen": now_iso,
                    "last_seen": now_iso,
                    "missing_since": None,
                    "probes": {},
                    "advertised": False,
                    "decided_by": "auto-rule" if not chat else None,
                    "decided_at": now_iso if not chat else None,
                }
            else:
                # idempotent rerun: preserve first_seen, advance last_seen
                rec["last_seen"] = now_iso
                rec.setdefault("raw_ids", {})[provider] = mid

    INTAKE_FILE.write_text(json.dumps(store, indent=2))
    return store


# ── INTAKE-2: 1-token probe + cross-provider merge + eligibility ─────────────
# Probe staged (chat) models per provider with a max_tokens=1 completion and
# record ts/pass/http/model_field per probe in model_intake.json. The response
# model field MUST match the requested canonical family (catches silent
# substitution at probe time — mismatch = probe FAIL). Budget: ≤1 probe per
# (model, provider) per run. Never probe non-chat/rejected. Eligibility: ≥2
# DISTINCT providers w/ pass=true -> status=eligible; else stays staged
# (probes retried next run, fail evidence kept).
#
# Chat-completion endpoints per provider (base + /chat/completions). Mirrors
# the base URLs in zai_proxy.py. key_env is the manager/.env var holding the
# API key (None = no auth header, e.g. local routstr/routstrd).
CHAT_PROVIDERS: dict[str, dict] = {
    "ollama_cloud": {"base_url": "https://ollama.com/v1", "key_env": "OLLAMA_CLOUD_API_KEY"},
    "ollama_cloud_2": {"base_url": "https://ollama.com/v1", "key_env": "OLLAMA_CLOUD_API_KEY_2"},
    "neuralwatt": {"base_url": "https://api.neuralwatt.com/v1", "key_env": "NEURALWATT_API_KEY"},
    "deepinfra": {"base_url": "https://api.deepinfra.com/v1/openai", "key_env": "DEEPINFRA_API_KEY"},
    "opencode_go": {"base_url": "https://opencode.ai/zen/go/v1", "key_env": "OPENCODE_GO_API_KEY"},
    "ours": {"base_url": "https://api.z.ai/api/coding/paas/v4", "key_env": "ZAI_OUR_KEY"},
    "telnyx": {"base_url": "https://api.telnyx.com/v2/ai", "key_env": "TELNYX_API_KEY"},
}


def _model_field_matches(canonical_family: str, model_field: str) -> bool:
    """True if the provider's response model field belongs to the requested
    canonical family (catches silent substitution at probe time).

    Matching is tolerant of the canonical org-prefix (deepseek/deepseek-v5 vs
    deepseek-v5) and of provider-native tag suffixes (deepseek-v5:0731). A
    genuinely different family (glm-5.3 vs deepseek-v5) or an empty field is
    a mismatch -> probe FAIL.
    """
    fam = (canonical_family or "").strip().lower()
    field = (model_field or "").strip().lower()
    if not fam or not field:
        return False
    # strip org prefix from both sides
    fam_base = fam.split("/")[-1]
    field_base = field.split("/")[-1]
    # strip a date-tag suffix (e.g. deepseek-v5:0731 / deepseek-v5-0731)
    for sep in (":", "-"):
        if sep in field_base:
            head, _, tail = field_base.rpartition(sep)
            if _looks_like_date_tag(tail):
                field_base = head
    return fam_base == field_base


def probe_chat(url: str, key: str | None, provider: str, model_id: str,
               canonical_family: str) -> dict:
    """Send a single max_tokens=1 chat completion to a provider.

    Args:
        url: full chat-completions endpoint (base + /chat/completions)
        key: API key (None -> no Authorization header)
        provider: provider name (for the probe record)
        model_id: provider-native model ID to request
        canonical_family: canonical store key the response model field must
            match (silent-substitution guard)

    Returns a probe record: {ts, pass, http, model_field}. pass is True only
    when http==200 AND the response model field matches the canonical family.
    """
    t0 = time.time()
    body = json.dumps({
        "model": model_id,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }).encode()
    hdrs = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    if key:
        hdrs["Authorization"] = f"Bearer {key}"
    http = None
    model_field = ""
    try:
        req = urllib.request.Request(url, data=body, method="POST", headers=hdrs)
        with urllib.request.urlopen(req, timeout=30) as resp:
            http = resp.status
            data = json.loads(resp.read().decode(errors="ignore"))
        model_field = str(data.get("model", "") or "")
    except urllib.error.HTTPError as he:
        http = he.code
    except Exception:
        http = None
    ok = (http == 200) and _model_field_matches(canonical_family, model_field)
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "pass": ok,
        "http": http,
        "model_field": model_field,
    }


def run_intake_probes(store: dict, providers: dict, ctx_reg: dict, keys: dict,
                      now_iso: str | None = None) -> dict:
    """Probe every staged chat model across providers and compute eligibility.

    For each store entry with status=='staged' and modality=='chat', send at
    most ONE probe per (model, provider) this run (budget). Non-chat/rejected
    entries are never probed. Probe records are appended to rec['probes'][provider]
    (fail evidence kept). If ≥2 DISTINCT providers pass -> status=eligible
    (decided_by=auto-rule). Otherwise the entry stays staged for the next run.

    Returns the full updated store (also persisted to INTAKE_FILE).
    """
    now_iso = now_iso or datetime.now(timezone.utc).isoformat()
    for mid, rec in store.items():
        if rec.get("status") != "staged" or rec.get("modality") != "chat":
            continue  # never probe non-chat / rejected / already decided
        passes = 0
        for provider, cfg in providers.items():
            key = keys.get(cfg.get("key_env")) if cfg.get("key_env") else None
            if not key:
                continue  # no key -> cannot probe this provider
            # provider-native ID to request (raw upstream id if known)
            model_id = rec.get("raw_ids", {}).get(provider, mid)
            url = cfg["base_url"].rstrip("/") + "/chat/completions"
            probe_rec = probe_chat(url, key, provider, model_id, mid)
            rec.setdefault("probes", {})[provider] = probe_rec
            if probe_rec.get("pass"):
                passes += 1
        if passes >= 2:
            rec["status"] = "eligible"
            rec["decided_by"] = "auto-rule"
            rec["decided_at"] = now_iso
    INTAKE_FILE.write_text(json.dumps(store, indent=2))
    return store


# ── INTAKE-3: tier wall + measured-price gate (advertise flag) ──────────────
# A promoted_routing model may be advertised on /v1/models ONLY if BOTH:
#   (a) ≥1 healthy NON-z.ai provider serves it (z.ai-backed models are NEVER
#       public — z.ai ToS quota-resale; zai/ours/friend/manager/worker* keys
#       all count as z.ai), AND
#   (b) a measured price exists (real_price_tracker n≥50). Seed/estimated
#       prices NEVER advertise; unmeasured stays false and is auto-retried on
#       later runs.
def is_zai_provider(name: str) -> bool:
    """True if the provider key is z.ai-backed (tier wall — NEVER public).

    z.ai keys: 'zai', 'ours', 'friend', 'manager', and any 'worker*' key.
    Everything else (neuralwatt, opencode_go, deepinfra, ppq, openrouter,
    telnyx, ollama_cloud, routstr, routstrd, ...) is a genuine external
    provider that may back a public advertisement.
    """
    n = (name or "").strip().lower()
    if n in {"zai", "ours", "friend", "manager"}:
        return True
    if n.startswith("worker"):
        return True
    return False


def refresh_advertised_flags(store: dict, healthy_providers: set,
                             measured_models: set, now_iso: str | None = None) -> dict:
    """Recompute the `advertised` flag for every promoted_routing entry.

    advertised=True ONLY IF (a) ≥1 healthy NON-z.ai provider serves the model
    AND (b) the model has a measured price (in measured_models). Non-promoted
    entries are untouched. Unmeasured / z.ai-only models stay advertised=False
    (auto-retried on later runs). Returns the updated store (also persisted).
    """
    now_iso = now_iso or datetime.now(timezone.utc).isoformat()
    for mid, rec in store.items():
        if rec.get("status") != "promoted_routing":
            continue
        providers = set(rec.get("raw_ids", {}).keys())
        healthy_non_zai = any(
            p in healthy_providers and not is_zai_provider(p) for p in providers)
        measured = mid in measured_models
        rec["advertised"] = bool(healthy_non_zai and measured)
        rec.setdefault("decided_at", now_iso)
    INTAKE_FILE.write_text(json.dumps(store, indent=2))
    return store


# ── INTAKE-4: removal grace + PROMOTION BATCH/REMOVALS digest ──────────────
# A model absent from ALL live provider catalogs is put in a 7-day grace
# window (missing_since set, status=grace). A grace alert line is raised in
# the drift report. After >7 days the entry is DROPPED from the store — UNLESS
# it is a promoted_routing entry, which is always KEPT and instead listed in
# the human digest (REMOVALS section) for MANUAL registry removal. Routing
# entries are NEVER auto-removed (removing one would silently break dispatch
# for a routable model). A model that reappears during grace has its
# pre-grace status restored automatically.
GRACE_DAYS = 7


def _present_in_any_ok_catalog(live: dict, mid: str) -> bool:
    """True if `mid` appears in any OK-probed provider catalog.

    Providers whose probe failed/skipped (probe_status != 'ok') are ignored —
    a failed probe does NOT prove absence. If no provider produced an OK
    catalog at all, absence cannot be established, so the model is treated as
    present (never enters grace from an all-dead run).
    """
    ok_seen = False
    for entry in live.values():
        if entry.get("probe_status") != "ok":
            continue
        ok_seen = True
        if mid in (entry.get("canonical") or []):
            return True
    return not ok_seen  # no OK catalog -> cannot prove absence


def _iso_days_elapsed(older: str | None, newer: str) -> float:
    """Days between two ISO timestamps (0.0 if either is unparseable)."""
    try:
        a = datetime.fromisoformat(older)
        b = datetime.fromisoformat(newer)
        return (b - a).total_seconds() / 86400.0
    except Exception:
        return 0.0


def apply_removal_grace(store: dict, live: dict, now_iso: str | None = None) -> tuple[dict, list]:
    """Mark/evict absent models via a 7-day removal grace window.

    For every store entry:
      - absent from ALL OK-probed catalogs  -> enter grace (missing_since set,
        status=grace, prior status saved in pre_grace_status).
      - present in >=1 OK catalog during grace -> grace cleared, pre-grace
        status restored (a transient blip must not kill a promoted model).
      - grace elapsed > GRACE_DAYS:
          * non-promoted (pre_grace_status != promoted_routing) -> entry
            dropped from the store.
          * promoted_routing -> entry KEPT and canonical added to the returned
            `manual_removals` list (human digest surface). NEVER auto-removed.

    Returns (updated_store, manual_removals). The store is also persisted.
    """
    now_iso = now_iso or datetime.now(timezone.utc).isoformat()
    manual_removals: list[str] = []
    for mid in list(store.keys()):
        rec = store[mid]
        if _present_in_any_ok_catalog(live, mid):
            if rec.get("status") == "grace":
                # transient disappearance — restore the real (pre-grace) status
                rec["status"] = rec.pop("pre_grace_status", None) or "staged"
                rec["missing_since"] = None
            continue
        # absent from all live catalogs
        if rec.get("status") != "grace":
            rec["pre_grace_status"] = rec.get("status")
            if not rec.get("missing_since"):
                rec["missing_since"] = now_iso
            rec["status"] = "grace"
        # eviction after the grace window elapsed
        if _iso_days_elapsed(rec.get("missing_since"), now_iso) > GRACE_DAYS:
            if rec.get("pre_grace_status") == "promoted_routing":
                # never auto-remove a routing entry — flag for human digest
                manual_removals.append(mid)
            else:
                del store[mid]
    INTAKE_FILE.write_text(json.dumps(store, indent=2))
    return store, manual_removals


def format_digest(store: dict, measured_models: set, manual_removals: list) -> str:
    """Build the drift-cron digest stdout ('PROMOTION BATCH:' + 'REMOVALS:').

    'PROMOTION BATCH:' lists eligible chat models with provider breadth and
    measured-price status (y/n). 'REMOVALS:' lists promoted_routing models past
    their grace window that need MANUAL registry removal. Each section is
    OMITTED when empty, and the whole digest is "" when both are empty — so the
    drift cron keeps its empty-stdout-when-clean contract.
    """
    lines: list[str] = []
    eligible = sorted(
        m for m, r in store.items()
        if r.get("status") == "eligible" and r.get("modality") == "chat")
    if eligible:
        lines.append("PROMOTION BATCH:")
        for mid in eligible:
            rec = store[mid]
            breadth = len(rec.get("raw_ids", {}) or {})
            price = "y" if mid in measured_models else "no"
            lines.append(f"  {mid}  providers={breadth}  measured={price}")
    if manual_removals:
        lines.append("REMOVALS:")
        for mid in sorted(set(manual_removals)):
            lines.append(f"  {mid}  (promoted_routing past grace window — "
                         f"MANUAL registry removal required, never auto-removed)")
    return "\n".join(lines)


def format_grace_alert(graced_ids: list) -> str:
    """Grace alert line for the drift report (list of canonicals now in grace)."""
    if not graced_ids:
        return ""
    return (f"⚠️ intake GRACE ({GRACE_DAYS}d removal timer) — absent from all catalogs: "
            f"{', '.join(sorted(graced_ids))}")


def measured_models() -> set:
    """Best-effort set of models with a measured price (real_price_tracker data).

    Mirrors scripts/model_promote.py._measured_models(). Flattens the nested
    ``{provider: {model: $/M, '_default': $/M}}`` shape and returns the model
    names (dropping ``_default``). Never raises — an empty set on any failure
    (which makes every eligible model 'unmeasured' for the digest, and no
    promoted model becomes advertised).
    """
    measured: set = set()
    try:
        sys.path.insert(0, str(BOT))
        import real_price_tracker as rpt  # type: ignore
        got = rpt.get_all_trailing_rates_per_model()
        if isinstance(got, dict):
            for prov in (got or {}).values():
                if isinstance(prov, dict):
                    measured.update(m for m in prov if m != "_default")
    except Exception:
        pass
    return measured


def healthy_provider_names(frame_providers: dict) -> set:
    """Set of provider names currently healthy per flat_router.

    Best-effort. Mirrors scripts/model_promote.py._healthy_providers(). Falls
    back to the `live` probe map (providers with probe_status=='ok') when
    flat_router cannot be fully introspected.
    """
    healthy: set = set()
    try:
        from flat_router import PROVIDER_MODELS, _is_provider_healthy  # type: ignore
        for name in PROVIDER_MODELS:
            try:
                if _is_provider_healthy(name):
                    healthy.add(name)
            except Exception:
                pass
    except Exception:
        pass
    if not healthy:
        # fallback: any provider that just probed OK is presumed healthy
        healthy = {n for n, e in frame_providers.items()
                   if e.get("probe_status") == "ok"}
    return healthy


def probe(url: str, key: str | None, provider: str) -> dict:
    """Fetch a provider catalog. Returns FR-0-style probe record.

    F6: retries once on transient 403/5xx (baseline opencode_go probe hit a
    spurious 403 that a manual retry cleared).
    """
    t0 = time.time()
    rec: dict = {"url": url, "provider": provider}
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    statuses = []
    data = None
    err = None
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = resp.read().decode()
                statuses.append(resp.status)
            data = json.loads(body)
            err = None
            break
        except Exception as e:
            err = str(e)
            statuses.append(None)
            if attempt == 0:
                time.sleep(2)

    rec["elapsed_s"] = round(time.time() - t0, 2)
    rec["attempts"] = statuses
    if data is None:
        rec["http_status"] = None
        rec["error"] = err
        rec["models"] = []
        return rec

    rec["http_status"] = statuses[-1]
    entries = data.get("data", data if isinstance(data, list) else [])
    ids = [m.get("id", "?") for m in entries]
    rec["models"] = ids
    # Pricing + context enrichment (best-effort, per model)
    prices = {}
    ctx = {}
    for m in entries:
        mid = m.get("id", "?")
        pr = m.get("pricing") or {}
        if isinstance(pr, dict) and isinstance(pr.get("prompt"), (int, float)):
            prices[mid] = pr["prompt"]
        # context fields vary: context_length / context_window / top_provider.context_length
        cval = (m.get("context_length") or m.get("context_window")
                or (m.get("top_provider") or {}).get("context_length"))
        if isinstance(cval, (int, float)) and cval > 0:
            ctx[to_canonical(mid, provider)] = int(cval)
    if prices:
        rec["pricing"] = prices
    if ctx:
        rec["context_lengths_canonical"] = ctx
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
            chat_canonical = sorted(c for c in canonical if _is_chat_model(c))
            live[name] = {
                "probe_status": "ok",
                "raw_count": len(rec["models"]),
                "canonical": canonical,
                "chat_canonical": chat_canonical,
                "catalog_complete": cfg["catalog_complete"],
                "allowlist_extra": cfg.get("allowlist_extra", []),
                "pricing": rec.get("pricing", {}),
                "context_lengths": rec.get("context_lengths_canonical", {}),
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

    # INTAKE-1: stage newly-discovered upstream models into the quarantine
    # store (model_intake.json). New = live probe, unknown to BOTH
    # PROVIDER_MODELS and model_context_registry.json. Non-chat -> rejected.
    # STAGED = quarantine: not routable, not advertised, requests keep loud-503.
    intake = stage_new_models(live, PROVIDER_MODELS, ctx_reg)
    new_staged = [m for m, r in intake.items() if r.get("status") == "staged"]
    new_rejected = [m for m, r in intake.items() if r.get("status") == "rejected"]
    if new_staged or new_rejected:
        print(f"[catalog-drift] intake staged={len(new_staged)} "
              f"rejected={len(new_rejected)} "
              f"staged_ids={','.join(new_staged)[:120]}")

    # INTAKE-2: probe staged chat models (max_tokens=1) across providers and
    # promote to eligible when ≥2 DISTINCT providers pass. Non-chat/rejected
    # are never probed. Fail evidence is kept in rec['probes'] for retry.
    intake = run_intake_probes(intake, CHAT_PROVIDERS, ctx_reg, keys)
    newly_eligible = [m for m, r in intake.items() if r.get("status") == "eligible"]
    if newly_eligible:
        print(f"[catalog-drift] intake eligible={len(newly_eligible)} "
              f"eligible_ids={','.join(newly_eligible)[:120]}")

    # INTAKE-3/INTAKE-4: refresh advertised flags (tier wall + measured-price
    # gate) and apply the 7-day removal grace window. A model absent from ALL
    # live catalogs enters grace; after >7d non-promoted entries are dropped
    # from the store, while promoted_routing entries are listed for MANUAL
    # registry removal (never auto-removed). The digest stdout carries the
    # 'PROMOTION BATCH:' and 'REMOVALS:' sections (omitted when empty so the
    # drift cron stays empty-stdout-when-clean).
    measured = measured_models()
    intake = refresh_advertised_flags(
        intake, healthy_providers=healthy_provider_names(live),
        measured_models=measured)
    intake, manual_removals = apply_removal_grace(intake, live)
    graced_ids = [m for m, r in intake.items() if r.get("status") == "grace"]
    digest = format_digest(intake, measured_models=measured,
                           manual_removals=manual_removals)
    grace_alert = format_grace_alert(graced_ids)
    if grace_alert:
        print(grace_alert)
    if digest:
        print(digest)

    drift = {"phantoms": {}, "missing_rungs": {}, "translation_gaps": {}, "context_gaps": {}}
    for name, cfg in PROVIDER_MODELS.items():
        live_entry = live.get(name, {})
        if live_entry.get("probe_status") != "ok":
            continue
        raw_set = set(live_entry.get("canonical", []))
        extra = set(live_entry.get("allowlist_extra", []))
        routed = cfg
        if live_entry.get("catalog_complete"):
            phantoms = []
            for m in routed:
                if m in raw_set or m in extra:
                    continue
                # Translated-model exemption: if the provider's name map
                # resolves m to a native ID that IS in the live catalog
                # (e.g. telnyx kimi-k2.7-code → moonshotai/Kimi-K2.5),
                # the model is servable — not a phantom.
                native = name_maps.get(name, {}).get(m)
                # native may itself be an alias of a raw entry — compare
                # canonically against the raw set
                if native and to_canonical(native, name) in raw_set:
                    continue
                phantoms.append(m)
            if phantoms:
                drift["phantoms"][name] = sorted(phantoms)
        missing = sorted(c for c in (raw_set - set(routed)) if _is_chat_model(c))
        if missing:
            drift["missing_rungs"][name] = missing
        # Translation gaps (F3): flag a routable model ONLY when the provider's
        # raw catalog does NOT contain the canonical name verbatim (meaning a
        # translation is genuinely required) AND no map entry exists. Verbatim
        # providers (catalog serves canonical names directly) are exempt.
        raw_models = set(live_entry.get("canonical", []))
        for m in routed:
            if name in name_maps and m not in name_maps[name] and m not in raw_models:
                drift["translation_gaps"].setdefault(name, []).append(m)
        # Context gaps: routable models missing from the context registry
        cgaps = sorted(m for m in routed if m not in ctx_reg and ":"
                       not in m and "/" not in m)
        if cgaps:
            drift["context_gaps"][name] = cgaps

    # Write live_catalog_state.json (runtime phantom guard input).
    # Translated-servable models (routed canonical → mapped native → present
    # in raw catalog) are added to allowlist_extra so the runtime guard
    # passes them (D1 fix: telnyx kimi-k2.7-code → moonshotai/Kimi-K2.5).
    state = {"fetched_at": now, "providers": {}}
    for name, entry in live.items():
        if entry.get("probe_status") == "ok":
            allowlist = list(entry.get("allowlist_extra", []))
            if entry.get("catalog_complete"):
                raw_set = set(entry["canonical"])
                pmap = name_maps.get(name, {})
                for m in PROVIDER_MODELS.get(name, set()):
                    if m in raw_set or m in allowlist:
                        continue
                    native = pmap.get(m)
                    if native and to_canonical(native, name) in raw_set:
                        allowlist.append(m)
            state["providers"][name] = {
                "models": entry["canonical"],
                "fetched_at": now,
                "catalog_complete": entry.get("catalog_complete", True),
                "allowlist_extra": sorted(set(allowlist)),
            }
    prev = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    STATE_FILE.write_text(json.dumps(state, indent=2))
    # Keep previous for drift-signature dedup
    PREV_STATE_FILE.write_text(json.dumps(prev, indent=2))

    # G2 groundwork: collect context-length suggestions observed upstream for
    # models missing from model_context_registry.json
    ctx_suggestions: dict[str, int] = {}
    for name, entry in live.items():
        if entry.get("probe_status") != "ok":
            continue
        for model_id, clen in (entry.get("context_lengths") or {}).items():
            if model_id not in ctx_reg and _is_chat_model(model_id):
                existing = ctx_suggestions.get(model_id)
                # prefer larger observed value (provider-specific truncation)
                ctx_suggestions[model_id] = max(existing or 0, int(clen))
    if ctx_suggestions:
        print(f"[catalog-drift] context-suggestions available for: "
              f"{', '.join(sorted(ctx_suggestions))[:120]}")

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
    if ctx_suggestions:
        report["context_suggestions"] = ctx_suggestions
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