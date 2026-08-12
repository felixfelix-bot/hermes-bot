#!/usr/bin/env python3
"""zai_proxy — local reverse proxy for z.ai that auto-rotates API keys.

ContextVM-pattern: a local service that fetches + caches external data (key quotas)
and serves routing decisions transparently. Hermes points base_url here; the proxy
picks the best key per request + retries on 429.

Endpoints:
  POST /* → forwarded to z.ai (with the healthiest key; retries on 429)
  GET  /quota → both keys' cached quotas + which is active
  GET  /health → simple liveness check

Usage logging (separate SQLite DB at ~/.hermes/bot/zai_usage.db, WAL mode):
  api_calls      — one row per request (tokens, model, key, status, duration,
                   cache/ollama/ppq hit flags)
  key_decisions  — one row per key-selection decision (chosen key, reason, both
                   quota percentages, availability flags)
Logging never raises — all write paths are wrapped to swallow errors so a
logging failure can never break a proxied request.
"""
from __future__ import annotations
import json, os, sqlite3, sys, threading, time, urllib.request, urllib.error
from datetime import datetime, timezone, date as _date
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# ── Shadow mode (Phase 2) — price-first optimizer running read-only ──────────
# Import bridge: logs shadow routing decisions alongside live best_key() picks.
# Wrapped so a missing repo or import error NEVER breaks production routing.
_shadow_hook = None
try:
    _MRE_PATH = os.path.expanduser("~/merchant-routing-engine")
    if _MRE_PATH not in sys.path:
        sys.path.insert(0, _MRE_PATH)
    from src.shadow_hook import ShadowHook
    # ── Token audit (Phase 2.5.4) ──────────────────────────────────────
    # Extracted into src/token_audit.py so the billed-vs-actual mismatch
    # check is unit-testable.  Falls back to a local stub below if missing.
    from src.token_audit import audit_token_count as _audit_token_count
    # ── Converged rates (Phase 3.0) ────────────────────────────────────
    # Load converged Kalman base rates from historical daily_spend data at
    # startup instead of using static seed costs.  This gives the shadow
    # optimizer an immediately-converged cost model.  Falls back to seeds
    # (inside the ShadowHook constructor) on any failure.
    _converged_rates: dict[str, float] | None = None
    try:
        from scripts.feed_historical_costs import load_historical_rates
        _converged_rates = load_historical_rates()
        if _converged_rates:
            print(f"[shadow] Converged rates loaded:", flush=True)
            for _p, _r in sorted(_converged_rates.items()):
                print(f"[shadow]   {_p:15s}  ${_r:.6f}/M", flush=True)
    except Exception as _ce:
        print(f"[shadow] converged-rate load failed — using seed costs: {_ce}", flush=True)
        _converged_rates = None
    _shadow_hook = ShadowHook(
        db_path=os.path.expanduser("~/.hermes/bot/zai_usage.db"),
        converged_rates=_converged_rates,
    )
    print(f"[shadow] ShadowHook initialized — logging to zai_usage.db", flush=True)
except Exception as _e:
    print(f"[shadow] DISABLED — {_e}", flush=True)
    _shadow_hook = None
    _converged_rates = None

# ── Dispatch gate (P5.1) ─────────────────────────────────────────────────────
# Pure three-dimension decision fn (hardware → quota-margin → price) extracted
# into src/dispatch_gate.py so it is unit-testable.  Falls back to None on any
# import error — the endpoint then degrades to a coarse candidate check.
_evaluate_dispatch = None
try:
    from src.dispatch_gate import evaluate_dispatch as _evaluate_dispatch
except Exception as _dge:
    print(f"[dispatch_gate] DISABLED — {_dge}", flush=True)
    _evaluate_dispatch = None

# ── Ollama Cloud quota tracker (EUv2-5) ──────────────────────────────────────
# Real quota regime from cumulative token usage in zai_usage.db.
# Falls back to "included" (no penalty) on any failure — never breaks routing.
_ollama_quota_status = None
try:
    from src.ollama_quota_tracker import get_quota_status as _get_quota_status
    from src.ollama_quota_tracker import DEFAULT_SESSION_LIMIT as _OC_SESSION_LIMIT
except Exception as _oqe:
    print(f"[ollama_quota] DISABLED — {_oqe}", flush=True)
    _get_quota_status = None
    _OC_SESSION_LIMIT = 500_000_000  # fallback default

# ── Cost extraction (RP-2) ───────────────────────────────────────────────────
# Parses the real $ cost from each provider's API response body. Falls back to
# None (no per-call cost extraction) on any import error — the proxy's
# _extract_cost wrapper then zeroes flat-rate providers and estimates ollama.
_extract_cost_module = None
try:
    from src.cost_extraction import extract_cost as _ce_extract_cost
    _extract_cost_module = _ce_extract_cost
except Exception as _cee:
    print(f"[cost_extraction] DISABLED — {_cee}", flush=True)
    _extract_cost_module = None

# ── Real price tracker (RP-4) ────────────────────────────────────────────────
# Replaces ALL hardcoded rate constants with real measured rates from
# real_price_tracker.get_rate_with_fallback(). The tracker resolves:
#   1. Real measured cost_usd data from the DB
#   2. Ollama billing API (for ollama_cloud)
#   3. LAST_RESORT_RATES (clearly-marked estimates)
# Every import failure degrades gracefully to the inline _FALLBACK_RATES below.
_rpt_get_rate = None
try:
    from src.real_price_tracker import get_rate_with_fallback as _rpt_get_rate
    print("[real_price_tracker] loaded — cost estimation uses measured rates", flush=True)
except Exception as _rpte:
    print(f"[real_price_tracker] DISABLED — {_rpte}", flush=True)
    _rpt_get_rate = None

# Kill switch: set OLLAMA_EXTRA_USAGE_ENABLED=false to disable regime-based pricing
_OLLAMA_EXTRA_USAGE_ENABLED = os.environ.get("OLLAMA_EXTRA_USAGE_ENABLED", "false").lower() in ("1", "true", "yes")

# Cache the quota status to avoid DB queries on every snapshot call.
# Updated by _snapshot_quota() at most every _OLLAMA_QUOTA_CACHE_TTL seconds.
_ollama_quota_cache: dict | None = None
_ollama_quota_cache_ts: float = 0.0
_OLLAMA_QUOTA_CACHE_TTL = 30.0  # seconds

def _get_ollama_quota_status() -> dict:
    """Get cached or fresh ollama_cloud quota status. Thread-safe.

    Returns a dict with: regime, session_used_pct, weekly_used_pct,
    session_tokens, weekly_tokens. Falls back to an 'included' default
    on any error so routing is never broken.
    """
    global _ollama_quota_cache, _ollama_quota_cache_ts
    if _get_quota_status is None or not _OLLAMA_EXTRA_USAGE_ENABLED:
        return {
            "regime": "included",
            "session_used_pct": 0.0,
            "weekly_used_pct": 0.0,
            "session_tokens": 0,
            "weekly_tokens": 0,
        }
    now = time.time()
    if _ollama_quota_cache is not None and (now - _ollama_quota_cache_ts) < _OLLAMA_QUOTA_CACHE_TTL:
        return _ollama_quota_cache
    try:
        status = _get_quota_status(str(USAGE_DB))
        _ollama_quota_cache = status
        _ollama_quota_cache_ts = now
        return status
    except Exception:
        if _ollama_quota_cache is not None:
            return _ollama_quota_cache
        return {
            "regime": "included",
            "session_used_pct": 0.0,
            "weekly_used_pct": 0.0,
            "session_tokens": 0,
            "weekly_tokens": 0,
        }

def _probe_hardware(hardware_req: str) -> dict:
    """Probe physical hardware state for the dispatch gate (Dimension 1).

    Only runs when ``hardware_req != "none"``.  Fault-tolerant: missing files,
    failed udevadm/ssh calls, and parse errors all degrade to safe defaults
    (absent / unknown / unreachable).  Sources per IMPL-SPEC v2:
      - board presence: ``ls /dev/ttyACM*``
      - board identity: ``udevadm`` serial of the first ttyACM device
      - lock status: ``~/.hermes/peripheral_locks/board-lock-monitor.json``
      - DQ05 reachability: ``ssh -o ConnectTimeout=3 dq05 true``
    """
    import glob as _glob, subprocess as _sp
    if hardware_req == "none":
        return {"required": "none"}
    state: dict = {}
    if hardware_req in ("board", "dual_board"):
        acm: list = []
        try:
            acm = sorted(_glob.glob("/dev/ttyACM*"))
            state["board_present"] = len(acm) > 0
            state["board_count"] = len(acm)
        except Exception:
            state["board_present"] = False
            state["board_count"] = 0
        # Board identity (udevadm serial of first device).
        try:
            if acm:
                out = _sp.run(
                    ["udevadm", "info", "-q", "property", "-n", acm[0]],
                    capture_output=True, text=True, timeout=3,
                ).stdout
                for _line in out.splitlines():
                    if _line.startswith("ID_SERIAL_SHORT="):
                        state["board_id"] = _line.split("=", 1)[1]
                        break
        except Exception:
            pass
        # Lock status from the board-lock monitor JSON.
        try:
            import json as _json
            _lp = os.path.expanduser(
                "~/.hermes/peripheral_locks/board-lock-monitor.json")
            with open(_lp) as _f:
                _lmon = _json.load(_f) or {}
            _locks = _lmon.get("locks", []) or []
            _board_locks = [l for l in _locks
                            if str(l.get("resource", "")).startswith("board")]
            _free = [l for l in _board_locks if l.get("status") == "free"]
            _held = [l for l in _board_locks if l.get("status") == "locked"]
            state["lock_status"] = (
                "free" if _free else ("held" if _held else "unknown"))
            state["queue_depth"] = len(_held)
            state["estimated_wait_minutes"] = sum(
                int(l.get("age_minutes", 0) or 0) for l in _held)
        except Exception:
            state.setdefault("lock_status", "unknown")
    elif hardware_req == "dq05":
        # Lightweight reachability probe — 3s connect timeout, no shell.
        try:
            _r = _sp.run(
                ["ssh", "-o", "ConnectTimeout=3", "-o", "BatchMode=yes",
                 "dq05", "true"],
                capture_output=True, timeout=6)
            state["dq05_reachable"] = (_r.returncode == 0)
        except Exception:
            state["dq05_reachable"] = False
    return state

# ── Token-audit fallback (Phase 2.5.4) ──────────────────────────────────────
# If the src import above failed, define a never-raising stub so the request
# path's audit call is still safe.  Real logic lives in src/token_audit.py.
#
# IMPORTANT (false-positive fix): `billed_tokens` MUST be the provider's
# completion_tokens — NOT total_tokens.  The estimate is derived from
# len(response_buffer)//4, and the response buffer contains ONLY the completion
# text (the prompt is never echoed back).  Passing total_tokens (prompt +
# completion) makes the billed count always much larger than the completion-only
# estimate, which guarantees a spurious >20% mismatch on any request with a
# non-trivial prompt.
if "_audit_token_count" not in globals():

    def _audit_token_count(billed_tokens, response_buffer, threshold=0.20):
        try:
            _buf = response_buffer if response_buffer is not None else b""
            _actual = len(_buf) // 4
            _billed = int(billed_tokens or 0)
            if _billed <= 0 or _actual <= 0:
                return (_actual, False, 0.0)
            _rate = abs(_billed - _actual) / max(_billed, 1)
            return (_actual, _rate > threshold, _rate)
        except Exception:
            return (0, False, 0.0)

# ── LiveRouter (Phase 1.2) — Kalman-driven failover selection ───────────────
# LiveRouter wraps the RoutingOptimizer for LIVE failover routing.  It is
# ONLY called when BOTH z.ai keys are exhausted (best_key() Phase 4 sets
# chosen = None).  Normal ours/friend routing is completely unaffected.
#
# Kill switch: touch ~/.hermes/bot/.enable_live_routing to enable.
#             rm    ~/.hermes/bot/.enable_live_routing to disable.
# No restart needed — the flag is checked on every failover call.
#
# Safety: every LiveRouter call is wrapped in try/except.  If LiveRouter
# fails (import error, exception, no provider found), best_key() returns
# None and the existing hardcoded ollama → ppq → openrouter chain runs.
_LIVE_ROUTER = None
_LIVE_ROUTING_FLAG = os.path.expanduser("~/.hermes/bot/.enable_live_routing")
try:
    from src.live_router import LiveRouter as _LiveRouterCls
    _LIVE_ROUTER = _LiveRouterCls(
        db_path=os.path.expanduser("~/.hermes/bot/zai_usage.db"),
        converged_rates=_converged_rates,
    )
    print(f"[live] LiveRouter initialized — failover selection ready "
          f"(kill switch: {_LIVE_ROUTING_FLAG})", flush=True)
except Exception as _le:
    print(f"[live] LiveRouter DISABLED — {_le}", flush=True)
    _LIVE_ROUTER = None

# ── PPQ credit-balance bridge (P3-PPQ) ───────────────────────────────────────
# quota_state['ppq'] used to be hardcoded {'used_pct': 0.0} — PPQ credit
# depletion never reached the pricing engine. This imports the bridge fn from
# the merged collector (src.balance_collectors.ppq_quota_entry), which reads
# the newest 'ppq' row from provider_balances in api_burn.db (written by
# the every-5min balance_collectors --provider ppq cron). _snapshot_quota()
# calls _ppq_quota_snapshot() instead of the old hardcoded dict. Revert-safe:
# any failure → the old optimistic {'used_pct': 0.0} so routing never breaks.
_ppq_quota_entry_fn = None
try:
    from src.balance_collectors import ppq_quota_entry as _ppq_quota_entry_fn
    print("[ppq] balance bridge loaded — quota_state['ppq'] reads real credit balance",
          flush=True)
except Exception as _pqe:
    print(f"[ppq] balance bridge DISABLED — {_pqe}", flush=True)
    _ppq_quota_entry_fn = None

# ── OpenRouter credit-balance bridge (T1T3) ──────────────────────────────────
# quota_state['openrouter'] was hardcoded {used_pct:0.0, remaining:inf} — credit
# depletion never reached the pricing engine. Mirrors the PPQ bridge above:
# imports openrouter_quota_entry from the merged balance_collectors module
# (reads newest 'openrouter' row from provider_balances, written by the
# every-5min balance_collectors --provider openrouter cron). Revert-safe: any
# failure → old optimistic {used_pct:0.0, remaining:inf} so routing never
# breaks. REVERT: delete this block + restore the one-line hardcode
# `snap["openrouter"] = {"used_pct": 0.0, "remaining": float("inf")}`
# in _snapshot_quota().
_openrouter_quota_entry_fn = None
try:
    from src.balance_collectors import openrouter_quota_entry as _openrouter_quota_entry_fn
    print("[openrouter] balance bridge loaded — quota_state['openrouter'] reads real credit balance",
          flush=True)
except Exception as _oqe:
    print(f"[openrouter] balance bridge DISABLED — {_oqe}", flush=True)
    _openrouter_quota_entry_fn = None

# ── config ──────────────────────────────────────────────────────────────────
def _load_keys():
    """Load keys from the manager .env (gitignored, never in repo)."""
    keys = {}
    for ep in [Path.home()/".hermes/profiles/manager/.env", Path.home()/".hermes/.env"]:
        if ep.exists():
            for line in ep.read_text(errors="ignore").splitlines():
                line = line.strip()
                if line.startswith("ZAI_API_KEY=") and "ZAI_OUR_KEY" not in line and "friend" not in keys:
                    keys["friend"] = line.split("=",1)[1].split("#")[0].strip().strip("'").strip('"')
                elif line.startswith("ZAI_OUR_KEY=") and "ours" not in keys:
                    keys["ours"] = line.split("=",1)[1].split("#")[0].strip().strip("'").strip('"')
    return keys

KEYS = _load_keys()
# Per-window lock thresholds: a key is "locked" when ANY window's used_pct
# meets/exceeds its threshold for that key name.  Burst protection on the short
# window, quota preservation on the weekly window for the friend key.
LOCK_THRESHOLDS = {
    "5-hour":  {"ours": 90, "friend": 80},   # burst protection; switch off friend earlier (80%)
    "weekly":  {"ours": 60, "friend": 80},   # proactive: switch off ours at 60% (40% buffer)
    "monthly": {"ours": 95, "friend": 95},   # tools limit (high — rarely hit)
}

# Cost-aware routing tie-breaker. Cheapest key wins when both are unlocked
# AND healthy. NOTE: cost is a TIE-BREAKER only — Kalman exhaustion prediction
# and per-window lock thresholds remain the primary signals in best_key().
#
#   ours          1.0   — base rate (z.ai subscription); CHEAPEST when healthy.
#                         Subscription may be cancelled → mark dead with:
#                         touch ~/.hermes/bot/.key_disabled_ours
#   friend        1.21  — z.ai courtesy key (21% premium over base rate).
#   ollama_cloud  1.0   — flat-rate cloud ($100/mo, rate from real_price_tracker). Preferred
#                         during z.ai peak hours (UTC 6-10) or when z.ai is dead.
#   ppq           — pay-per-token; most expensive, last-resort failover only.
_KEY_COST_MULTIPLIER = {"ours": 1.0, "friend": 1.21, "ollama_cloud": 1.0}
UPSTREAM   = "https://api.z.ai/api/coding/paas/v4"
QUOTA_URL  = "https://api.z.ai/api/monitor/usage/quota/limit"
CACHE_TTL  = 300                                # 5 min
PORT       = 9099
STATE_FILE = Path.home() / ".hermes" / "bot" / "zai_proxy_state.json"

# ── external failover providers ─────────────────────────────────────────────
def _load_external_keys():
    """Load PPQ, OpenRouter, Ollama Cloud, DeepInfra, and Telnyx keys from .env."""
    keys = {}
    for ep in [Path.home()/".hermes/profiles/manager/.env", Path.home()/".hermes/.env",
               Path.home()/".hermes/bot/.env"]:
        if ep.exists():
            for line in ep.read_text(errors="ignore").splitlines():
                line = line.strip()
                if line.startswith("PPQ_API_KEY=") and "ppq" not in keys:
                    keys["ppq"] = line.split("=",1)[1].split("#")[0].strip().strip("'").strip('"')
                elif line.startswith("OPENROUTER_API_KEY=") and "openrouter" not in keys:
                    keys["openrouter"] = line.split("=",1)[1].split("#")[0].strip().strip("'").strip('"')
                elif line.startswith("OLLAMA_CLOUD_API_KEY=") and "ollama_cloud" not in keys:
                    keys["ollama_cloud"] = line.split("=",1)[1].split("#")[0].strip().strip("'").strip('"')
                elif line.startswith("DEEPINFRA_API_KEY=") and "deepinfra" not in keys:
                    keys["deepinfra"] = line.split("=",1)[1].split("#")[0].strip().strip("'").strip('"')
                elif line.startswith("DEEPINFRA_STARTING_BALANCE=") and "deepinfra_balance" not in keys:
                    keys["deepinfra_balance"] = line.split("=",1)[1].split("#")[0].strip().strip("'").strip('"')
                elif line.startswith("TELNYX_API_KEY=") and "telnyx" not in keys:
                    keys["telnyx"] = line.split("=",1)[1].split("#")[0].strip().strip("'").strip('"')
                elif line.startswith("TELNYX_STARTING_BALANCE=") and "telnyx_balance" not in keys:
                    keys["telnyx_balance"] = line.split("=",1)[1].split("#")[0].strip().strip("'").strip('"')
    return keys

_EXTERNAL_KEYS = _load_external_keys()

# Ollama Cloud — primary provider (same tier as z.ai, not just failover)
OLLAMA_CLOUD_KEY = _EXTERNAL_KEYS.get("ollama_cloud", "")
OLLAMA_CLOUD_BASE = "https://ollama.com/v1"

# DeepInfra — preferred external failover (prompt caching reduces effective cost)
DEEPINFRA_KEY = _EXTERNAL_KEYS.get("deepinfra", "")
DEEPINFRA_BASE = "https://api.deepinfra.com/v1/openai"
DEEPINFRA_STARTING_BALANCE = float(_EXTERNAL_KEYS.get("deepinfra_balance", "5.0") or "5.0")

# Telnyx — Kimi K3 failover provider (demo endpoint needs no API key)
# Demo endpoint: POST https://telnyx.com/api/inference (10 req/min, SSE streaming)
# Production API: https://api.telnyx.com/v2/ai (requires account + API key)
TELNYX_KEY = _EXTERNAL_KEYS.get("telnyx", "")
TELNYX_BASE = "https://api.telnyx.com/v2/ai"
TELNYX_DEMO_URL = "https://telnyx.com/api/inference"
TELNYX_STARTING_BALANCE = float(_EXTERNAL_KEYS.get("telnyx_balance", "10.0") or "10.0")

# Startup diagnostics — print key/balance status like other external providers
print(f"[telnyx] key={'loaded' if TELNYX_KEY else 'MISSING'} "
      f"suffix={TELNYX_KEY[-4:] if TELNYX_KEY else 'N/A'} "
      f"starting_balance=${TELNYX_STARTING_BALANCE:.2f}", flush=True)

# Models that have Telnyx fallback when Ollama Cloud fails
_TELNYX_FALLBACK_MODELS = {"kimi-k2.7-code", "kimi-k3:cloud"}

# Provider priority for failover sort (lower = tried first).
# DeepInfra preferred over PPQ because of prompt-caching discounts.
_PROVIDER_PRIORITY = {"deepinfra": 0, "ppq": 1, "openrouter": 2, "telnyx": 3}

# Per-provider model name translation.
# PPQ/OpenRouter use canonical short IDs (e.g., "deepseek/deepseek-v4-pro")
# but DeepInfra expects case-sensitive dotted form (e.g., "deepseek-ai/DeepSeek-V4-Pro").
# Any provider not in this dict uses ext_model verbatim.
_PROVIDER_MODEL_NAMES = {
    "deepinfra": {
        "deepseek/deepseek-v4-pro":   "deepseek-ai/DeepSeek-V4-Pro",
        "deepseek/deepseek-v4-flash": "deepseek-ai/DeepSeek-V4-Flash",
        "glm-5.2":                    "zai-org/GLM-5.2",
    },
    "telnyx": {
        "kimi-k3":         "moonshotai/Kimi-K3",
        "kimi-k2.5":       "moonshotai/Kimi-K2.5",
        "glm-5.2":         "zai-org/GLM-5.2",
        "minimax-m3":      "MiniMaxAI/MiniMax-M3-MXFP8",
        "kimi-k3:cloud":   "moonshotai/Kimi-K3",
        "kimi-k2.7-code":  "moonshotai/Kimi-K2.5",  # K2.5 closest to K2.7 on Telnyx
    },
}

EXTERNAL_PROVIDERS = {
    "deepinfra": {
        "base_url": DEEPINFRA_BASE,
        "key": DEEPINFRA_KEY,
    },
    "telnyx": {
        "base_url": TELNYX_BASE,
        "key": TELNYX_KEY,
    },
    "ppq": {
        "base_url": "https://api.ppq.ai/v1",
        "key": _EXTERNAL_KEYS.get("ppq", ""),
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "key": _EXTERNAL_KEYS.get("openrouter", ""),
    },
}

# Fallback models — chosen based on the requesting profile's quality tier.
# Manager (glm-5.2): quality floor at deepseek-v4-pro (55.4% SWE-bench).
#   NEVER falls back to flash — returns error instead of low-quality output.
# Workers (glm-4.5-flash): cheapest available is fine (output gets vetted).
MANAGER_FALLBACK_MODEL = "glm-5.2"
WORKER_FALLBACK_MODEL = "deepseek/deepseek-v4-flash"

# z.ai peak hours: Beijing 14:00-18:00 = UTC 6-10. During peak, z.ai burns 3x quota.
# Ollama Cloud has no peak pricing — prefer it during these hours.
_PEAK_HOURS_UTC = {6, 7, 8, 9, 10}

def _is_peak_hour() -> bool:
    """Check if current UTC hour is a z.ai peak hour."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).hour in _PEAK_HOURS_UTC

# ── provider funding tracker ────────────────────────────────────────────────
# Tracks which providers have credits remaining. A 402 response marks a
# provider unfunded for 1 hour (credits may be replenished). The failover
# logic only tries funded providers, sorted by cost.
_UNFUNDED_RETRY_SECONDS = 3600  # retry unfunded provider after 1 hour

_provider_health: dict[str, dict] = {}


def _is_provider_funded(name: str) -> bool:
    """Check if a provider has credits. Unfunded providers are retried
    after _UNFUNDED_RETRY_SECONDS."""
    h = _provider_health.get(name)
    if not h or h.get("funded", True):
        return True
    return time.time() >= h.get("retry_after", 0)


def _mark_unfunded(name: str) -> None:
    """Mark a provider as out of credits (after receiving 402)."""
    _provider_health[name] = {
        "funded": False,
        "last_402": time.time(),
        "retry_after": time.time() + _UNFUNDED_RETRY_SECONDS,
    }


def _mark_funded(name: str) -> None:
    """Mark a provider as funded again (successful response)."""
    _provider_health[name] = {"funded": True}


# ── z.ai key health tracker ─────────────────────────────────────────────────
# Same pattern as _provider_health, but for z.ai keys. When a key returns
# an empty response or 429, it's marked exhausted with BINARY EXPONENTIAL
# BACKOFF: each consecutive failure doubles the retry-after delay (capped
# at 1 hour). best_key() skips exhausted keys. When both are exhausted,
# the proxy fails over to external providers (PPQ/OpenRouter).
#
# Manual override: drop a flag file ~/.hermes/bot/.key_disabled_<name> to
# force a key to be treated as unhealthy (e.g. a cancelled subscription).
# Re-enable with: rm ~/.hermes/bot/.key_disabled_<name>

# Exponential backoff ramp for QUOTA-EXHAUSTION failures (429 / empty response).
# Spec: 2s→4s→8s→16s→32s→60s (capped). A single 429 blocks a key for only 2s so
# the other key / external failover covers traffic immediately; repeated 429s
# escalate up to the 60s cap.
_BACKOFF_SEQUENCE = (2, 4, 8, 16, 32, 60)

# Dead key (401/403) — auth failure, likely revoked/cancelled. Flat 1h: a dead
# key will not recover by retrying quickly, so park it for an hour.
_DEAD_KEY_BACKOFF_SECONDS = 3600

# Upstream server error (500/502/503/504) — transient, not the key's fault.
# Medium flat backoff.
_SERVER_ERROR_BACKOFF_SECONDS = 30

# Legacy aliases — kept so any external script referencing them still resolves.
_BACKOFF_BASE_SECONDS = 30
_BACKOFF_CAP_SECONDS    = 3600

# Log a KEY_DEAD anomaly after this many consecutive failures of any type —
# surfaces persistently-failing keys (e.g. a cancelled subscription) to dashboards.
_KEY_DEAD_THRESHOLD     = 7

_zai_key_health: dict[str, dict] = {}


def _disabled_flag_path(name: str) -> Path:
    """Filesystem flag path used to manually disable key *name*."""
    return Path.home() / ".hermes" / "bot" / f".key_disabled_{name}"


def _is_manually_disabled(name: str) -> bool:
    """True iff the operator has touched ~/.hermes/bot/.key_disabled_<name>.

    Lightweight check (no logging) — safe to call inside loops (e.g. the retry
    order filter). ``_is_key_healthy`` does its own check + dashboard log, so
    prefer this helper anywhere that would otherwise spam key_decisions.
    Fails OPEN: a filesystem error is treated as 'not disabled'."""
    try:
        return _disabled_flag_path(name).exists()
    except Exception:
        return False


def _backoff_for_failure(failure_count: int) -> float:
    """Exponential backoff (seconds) for the Nth consecutive *exhaustion* failure
    (1-indexed): returns 2,4,8,16,32 then 60 for all subsequent failures."""
    if failure_count <= 0:
        return 0.0
    idx = min(failure_count - 1, len(_BACKOFF_SEQUENCE) - 1)
    return float(_BACKOFF_SEQUENCE[idx])


def _ensure_anomaly_table() -> None:
    """Ensure the anomaly_events table exists. Swallows all errors.

    Uses the SHARED monitoring schema (severity/category/title/detail) that the
    bot's anomaly detector also writes to. On systems where the table already
    exists this is a defensive no-op (CREATE IF NOT EXISTS)."""
    try:
        _usage_db().execute(
            "CREATE TABLE IF NOT EXISTS anomaly_events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "ts REAL NOT NULL,"
            "severity TEXT NOT NULL,"
            "category TEXT NOT NULL,"
            "title TEXT,"
            "detail TEXT,"
            "alerted INTEGER DEFAULT 0,"
            "resolved INTEGER DEFAULT 0)")
        _usage_db().execute(
            "CREATE INDEX IF NOT EXISTS idx_anomaly_ts ON anomaly_events(ts)")
    except Exception:
        pass


def _log_anomaly(severity: str, category: str, title: str,
                 detail: str, key_name: str | None = None) -> None:
    """Insert one row into the shared anomaly_events table.

    Writes to the monitoring schema (severity/category/title/detail). ``detail``
    is stored as a JSON object so dashboards can parse key_name + extras.
    Swallows all errors.
    """
    try:
        _ensure_anomaly_table()
        payload: dict = {"detail": detail}
        if key_name is not None:
            payload["key_name"] = key_name
        _usage_db().execute(
            "INSERT INTO anomaly_events (ts, severity, category, title, detail) "
            "VALUES (?,?,?,?,?)",
            (time.time(), severity, category, title, json.dumps(payload)))
    except Exception:
        pass


def _is_key_healthy(name: str) -> bool:
    """Check if a z.ai key has quota remaining.

    Returns False immediately if a manual-disable flag file exists:
        ~/.hermes/bot/.key_disabled_<name>
    Re-enable by removing the file, e.g.:
        rm ~/.hermes/bot/.key_disabled_ours
    """
    # Manual disable via flag file — checked first, overrides everything.
    try:
        if (Path.home() / ".hermes" / "bot" / f".key_disabled_{name}").exists():
            _log_key_decision(chosen_key=None,
                              reason=f"manually_disabled_{name}")
            return False
    except Exception:
        pass

    h = _zai_key_health.get(name)
    if not h or h.get("healthy", True):
        return True
    return time.time() >= h.get("retry_after", 0)


def _mark_key_failure(name: str, error_type: str = "exhausted") -> None:
    """Record one failure for *name* and arm the appropriate backoff window.

    error_type selects the backoff strategy (req 2 — dead-key detection):
      "exhausted" (429 / empty) → exponential ramp 2→60s (req 1)
      "dead"     (401/403)      → flat 1h (key likely revoked)
      "server"   (500/502/503/504) → flat 30s (transient upstream issue)

    Bumps the consecutive-failure counter (reset on success), mirrors the new
    state to the ``key_health`` table (req 4), and logs backoff/KEY_DEAD
    anomalies for dashboards. Never raises — a logging failure must never break
    a proxied request."""
    try:
        now = time.time()
        prev = _zai_key_health.get(name, {})
        failures = int(prev.get("consecutive_failures", 0)) + 1
        if error_type == "dead":
            backoff = _DEAD_KEY_BACKOFF_SECONDS
        elif error_type == "server":
            backoff = _SERVER_ERROR_BACKOFF_SECONDS
        else:  # "exhausted" — 429 / empty response
            backoff = _backoff_for_failure(failures)
        retry_after = now + backoff
        disabled = _is_manually_disabled(name)
        _zai_key_health[name] = {
            "healthy": False,
            "last_empty": now,
            "retry_after": retry_after,
            "consecutive_failures": failures,
            "backoff_seconds": backoff,
            "last_error_type": error_type,
            "last_failure_ts": now,
            "backoff_until": retry_after,
            "disabled_manually": disabled,
        }
        # Mirror per-key state to the key_health table (req 4 — circuit breaker
        # state: failure_count, last_failure_ts, last_error_type, backoff_until,
        # disabled_manually). _log_key_health is defined near the other _log_*
        # helpers below; forward reference is resolved at call time.
        _log_key_health(name, _zai_key_health[name])
        # Anomaly logging (dashboard visibility). _log_anomaly signature is
        # (severity, category, title, detail, key_name=None) — the logger
        # JSON-encodes detail+key_name into the shared anomaly_events table.
        _log_anomaly("WARN", "key_backoff",
                     f"{name} {error_type} failure #{failures}",
                     f"backoff {backoff}s; error_type={error_type}",
                     key_name=name)
        # A definitive auth failure means the key is dead right now — surface it
        # immediately rather than waiting for the N-failure threshold.
        if error_type == "dead":
            _log_anomaly("CRITICAL", "KEY_DEAD",
                         f"{name} marked dead (auth failure 401/403)",
                         f"backoff {_DEAD_KEY_BACKOFF_SECONDS}s; error_type=dead",
                         key_name=name)
        elif failures == _KEY_DEAD_THRESHOLD:
            _log_anomaly("CRITICAL", "KEY_DEAD",
                         f"{name} reached {failures} consecutive failures",
                         f"backoff {backoff}s; likely dead",
                         key_name=name)
    except Exception:
        pass


def _mark_key_exhausted(name: str) -> None:
    """Backward-compat shim — record a quota-exhaustion failure (429 / empty).

    Preserved so existing call sites (and the ollama_cloud 429 path) keep
    working; routes into the circuit breaker with error_type='exhausted'."""
    _mark_key_failure(name, error_type="exhausted")


def _mark_key_dead(name: str) -> None:
    """Record an auth failure (401/403) — long flat backoff, key may be revoked."""
    _mark_key_failure(name, error_type="dead")


def _mark_key_server_error(name: str) -> None:
    """Record a 5xx upstream error — medium flat backoff (not the key's fault)."""
    _mark_key_failure(name, error_type="server")


def _mark_key_healthy(name: str) -> None:
    """Mark a key healthy (successful response) and reset its failure counter.

    Resets consecutive_failures to 0 so the next exhaustion starts fresh from
    the minimum backoff. A manually-disabled key is kept disabled even on a
    success — the flag file is the operator's explicit override and is not
    auto-cleared here (remove the file to re-enable). Mirrors the reset to the
    key_health table. Never raises."""
    try:
        disabled = _is_manually_disabled(name)
        prev = _zai_key_health.get(name, {})
        _zai_key_health[name] = {
            "healthy": not disabled,
            "consecutive_failures": 0,
            "last_error_type": None,
            "last_failure_ts": prev.get("last_failure_ts", 0),
            "backoff_until": 0 if not disabled else prev.get("backoff_until", 0),
            "backoff_seconds": 0,
            "disabled_manually": disabled,
            # legacy fields
            "last_empty": prev.get("last_empty", 0),
            "retry_after": 0 if not disabled else prev.get("retry_after", 0),
        }
        _log_key_health(name, _zai_key_health[name])
    except Exception:
        pass


def _mark_unfunded(name: str) -> None:
    """Mark a provider as out of credits (after receiving 402)."""
    _provider_health[name] = {
        "funded": False,
        "last_402": time.time(),
        "retry_after": time.time() + _UNFUNDED_RETRY_SECONDS,
    }


def _mark_funded(name: str) -> None:
    """Mark a provider as funded again (successful response)."""
    _provider_health[name] = {"funded": True}


def _get_provider_cost(name: str, model_id: str) -> float:
    """Look up the combined cost per 1M tokens for a model on a provider.
    Reads from model_matrix.json if available; then real_price_tracker (RP-4);
    finally falls back to PPQ_PRICING dict. Returns 999.0 if unknown."""
    # Try model_matrix.json first (live pricing)
    try:
        matrix_path = BOT / "model_matrix.json"
        if matrix_path.exists():
            import json as _json
            matrix = _json.loads(matrix_path.read_text())
            key = f"{name}/{model_id}"
            entry = matrix.get("models", {}).get(key, {})
            if entry:
                keys = entry.get("keys", {})
                for k in keys.values():
                    return k.get("cost_per_1m_offpeak", k.get("cost_per_1m_combined", 999.0))
    except Exception:
        pass
    # RP-4: Try real_price_tracker (measured rates)
    if _rpt_get_rate is not None:
        try:
            tracked = _rpt_get_rate(name, model_id)
            if tracked is not None and tracked < 999.0:
                return tracked
        except Exception:
            pass
    # Last-resort fallback to known pricing
    from model_matrix import PPQ_PRICING
    pricing = PPQ_PRICING.get(model_id, PPQ_PRICING.get(model_id.lower(), (0.14, 0.28)))
    return pricing[0] + pricing[1]

# Model tier map: tier name → z.ai model name (cheapest first).
# The X-Model-Tier request header selects one of these tiers to rewrite the
# model field in the proxied request body.  Absent header = no rewrite.
MODEL_TIER_MAP: dict[str, str] = {
    "flash": "glm-4.5-flash",
    "air":   "glm-4.5-air",
    "mid":   "glm-4.5",
    "heavy": "glm-5.2",
}

# ── usage logging DB (separate from response_cache.db) ──────────────────────
USAGE_DB = Path.home() / ".hermes" / "bot" / "zai_usage.db"
_usage_db_conn: sqlite3.Connection | None = None
_usage_db_lock = threading.Lock()

quota_cache: dict[str, tuple[list[dict], float]] = {}   # name → (windows, ts)

# ── Phase 2.4: Pace windows for LiveRouter ──────────────────────────────────
# Computed in _refresh_loop() from quota_cache + LiveRouter's ConsumptionKalman
# burn rates. Stored here so best_key() can pass them to select_failover() on
# the next failover call. Thread-safe reads via `lock`.
_pace_windows: dict[str, list[tuple[float, float, float, float, float]]] = {}
lock = threading.Lock()

# ── Shadow mode snapshot helpers ────────────────────────────────────────────
def _ppq_quota_snapshot() -> dict:
    """quota_state['ppq'] from the latest collected PPQ credit balance (P3-PPQ).

    Delegates to the extracted ``ppq_quota_entry`` (reads provider_balances in
    api_burn.db). Cold-start contract: no/stale row → ``{}`` (passes through)
    so LiveRouter's ``_compute_ppq_pressure`` applies conservative
    ``cold_start_pressure`` (Task 4) instead of the old optimistic 1.0 — a PPQ
    endpoint we have no fresh data for must not look artificially cheap.
    Only falls back to ``{'used_pct': 0.0}`` when the bridge import is disabled
    or raises. Never raises.
    """
    if _ppq_quota_entry_fn is None:
        return {"used_pct": 0.0, "remaining": float("inf")}
    try:
        entry = _ppq_quota_entry_fn()
        # Pass {} (cold-start marker) through unchanged; only fall back on a
        # genuinely bad (non-dict) return.
        return entry if isinstance(entry, dict) else {
            "used_pct": 0.0, "remaining": float("inf"),
        }
    except Exception:
        return {"used_pct": 0.0, "remaining": float("inf")}


def _openrouter_quota_entry_snapshot() -> dict:
    """quota_state['openrouter'] from the latest collected balance (T1T3).

    Mirrors _ppq_quota_snapshot: delegates to the extracted
    ``openrouter_quota_entry`` (reads provider_balances in api_burn.db). Returns
    the cold-start ``{}`` marker when there is no/stale row, which the proxy
    maps to the optimistic ``{used_pct:0.0, remaining:inf}`` below. Never raises.
    """
    if _openrouter_quota_entry_fn is None:
        return {"used_pct": 0.0, "remaining": float("inf")}
    try:
        entry = _openrouter_quota_entry_fn()
        return entry if isinstance(entry, dict) else {
            "used_pct": 0.0, "remaining": float("inf"),
        }
    except Exception:
        return {"used_pct": 0.0, "remaining": float("inf")}


def _snapshot_quota() -> dict:
    """Snapshot current quota state for all providers. Thread-safe."""
    snap = {}
    try:
        with lock:
            for name in ("ours", "friend"):
                wins = quota_cache.get(name, ([], 0.0))[0]
                pct = _max_pct(wins)
                snap[name] = {
                    "used_pct": float(pct),
                    "remaining": max(0.0, 2_000_000 * (1.0 - pct / 100.0)),
                    "total": 2_000_000,
                }
        # Ollama Cloud — real quota from ollama_quota_tracker (EUv2-5)
        oc_status = _get_ollama_quota_status()
        oc_used_pct = max(oc_status["session_used_pct"], oc_status["weekly_used_pct"])
        # Use the session limit for remaining/total display
        oc_total = _OC_SESSION_LIMIT
        oc_remaining = max(0.0, oc_total * (1.0 - oc_used_pct / 100.0))
        snap["ollama_cloud"] = {
            "used_pct": float(oc_used_pct),
            "remaining": oc_remaining,
            "total": oc_total,
            "regime": oc_status["regime"],
            "session_used_pct": oc_status["session_used_pct"],
            "weekly_used_pct": oc_status["weekly_used_pct"],
            "session_tokens": oc_status["session_tokens"],
            "weekly_tokens": oc_status["weekly_tokens"],
        }
        # Per-token providers — effectively unlimited
        snap["ppq"] = _ppq_quota_snapshot()  # P3-PPQ: real credit balance
        snap["openrouter"] = _openrouter_quota_entry_snapshot()  # T1T3: real credit balance
    except Exception:
        pass
    return snap

def _snapshot_health() -> dict:
    """Snapshot health state for all providers. Thread-safe read."""
    h = {}
    try:
        for name in ("ours", "friend"):
            h[name] = _is_key_healthy(name)
        h["ollama_cloud"] = _is_key_healthy("ollama_cloud")
        h["ppq"] = _is_key_healthy("ppq")
        h["openrouter"] = True
    except Exception:
        pass
    return h

# ── proactive burn-rate prediction (Phase 3) ─────────────────────────────────
# Import the burn predictor.  Wrapped so a broken burn_predictor.py never crashes
# the proxy — if the import fails, proactive switching is silently disabled and
# the proxy falls back to reactive (lock-based) key selection.
_predict_exhaustion = None
_route_request = None
try:
    sys.path.insert(0, os.path.dirname(__file__))
    from burn_predictor import predict_exhaustion as _predict_exhaustion
    from burn_predictor import route_request as _route_request
except Exception:
    pass

# ── shadow-mode decision tap (Phase 2.1, ADR-014) ────────────────────────────
# READ-ONLY tap: logs what the price-first RoutingOptimizer WOULD have chosen
# alongside the live best_key() pick, so the two strategies can be compared
# after a soak period. NEVER affects routing — every failure is swallowed and
# `_shadow_logger`/`_shadow_optimizer` stay None on any import error, leaving
# production routing 100% unchanged.
#
# NOTE on deviation from the task body template: the body called
# `RoutingOptimizer(config_path=...)` and `log_decision(live_key=...,
# shadow_decision=...)`, but those signatures DO NOT EXIST. RoutingOptimizer
# has no config loader (providers are registered via add_provider, each backed
# by a PriceKalman + ConsumptionKalman), and ShadowLogger.log_decision takes
# positional fields (ts, live_provider, live_model, shadow_provider,
# shadow_model, shadow_cost, tokens, reason, live_cost). Pasting the body
# verbatim would raise TypeError on import and silently disable the tap forever
# (the TEST row-count check would never increase). The construction below
# mirrors tests/test_integration.py::_three_provider_optimizer and the topology
# in config/providers.yaml; the tap below maps route()'s return dict to
# log_decision()'s real signature. Static seeded Kalman rates are used because
# no config->optimizer loader exists yet (out of scope for a read-only tap).
_shadow_logger = None
_shadow_optimizer = None
try:
    _SHADOW_REPO = '/home/c03rad0r/merchant-routing-engine'
    if _SHADOW_REPO not in sys.path:
        sys.path.insert(0, _SHADOW_REPO)
    from src.shadow_logger import ShadowLogger as _ShadowLogger
    from src.routing_optimizer import RoutingOptimizer as _RoutingOptimizer
    from src.price_kalman import PriceKalman as _ShadowPriceKalman
    from src.consumption_kalman import ConsumptionKalman as _ShadowConsumptionKalman

    def _shadow_pk(rate):
        kf = _ShadowPriceKalman(initial_rate=rate, process_noise=1e-6,
                                measurement_noise=1e-4)
        kf.update(rate)
        return kf

    _shadow_optimizer = _RoutingOptimizer(peak_hours_utc=(6, 10), peak_mult=3.0)
    # zai_ours — flat-rate subscription, high tier, cheapest off-peak, peak window
    _shadow_optimizer.add_provider(
        "zai_ours", _shadow_pk(0.068), _ShadowConsumptionKalman(),
        quota_remaining=1_000_000, model_tier="high", quota_total=2_000_000,
        peak_hours_utc=(6, 10), peak_mult=3.0,
    )
    # zai_friend — derived +21% premium over ours (ADR-005), high tier
    _shadow_optimizer.add_provider(
        "zai_friend", _shadow_pk(0.068 * 1.21), _ShadowConsumptionKalman(),
        quota_remaining=1_000_000, model_tier="high", quota_total=2_000_000,
        peak_hours_utc=(6, 10), peak_mult=3.0,
    )
    # ollama_cloud — flat-rate $100/mo, standard tier, NO peak window
    _shadow_optimizer.add_provider(
        "ollama_cloud", _shadow_pk(0.40), _ShadowConsumptionKalman(),
        quota_remaining=500_000, model_tier="standard", quota_total=1_000_000,
    )
    # ppq_external — per-token, low tier, most expensive, last resort
    _shadow_optimizer.add_provider(
        "ppq_external", _shadow_pk(0.80), _ShadowConsumptionKalman(),
        quota_remaining=10_000_000, model_tier="low", quota_total=20_000_000,
    )
    # deepinfra — per-token, low tier (same models as PPQ), preferred external
    # due to prompt-caching discounts. No peak window.
    try:
        import sys as _sys
        _sys.path.insert(0, '/home/c03rad0r/.hermes/bot')
        from zai_proxy import _get_deepinfra_balance as _gdb
        _di_balance = _gdb() * 1_000_000  # USD → token-equiv at $1.30/M
    except Exception:
        _di_balance = 5.0 * 1_000_000
    _shadow_optimizer.add_provider(
        "deepinfra", _shadow_pk(1.30), _ShadowConsumptionKalman(),
        quota_remaining=_di_balance, model_tier="low",
        quota_total=DEEPINFRA_STARTING_BALANCE * 1_000_000,
    )
    # telnyx — per-token, low tier (expensive per-token), last resort.
    # Seed rate: 5.40/M = blended kimi-k3 cost: (2.70*3 + 13.50*1) / 4
    _shadow_optimizer.add_provider(
        "telnyx", _shadow_pk(5.40), _ShadowConsumptionKalman(),
        quota_remaining=TELNYX_STARTING_BALANCE * 1_000_000,
        model_tier="low",
        quota_total=TELNYX_STARTING_BALANCE * 1_000_000,
    )
    # Defaults to ~/.hermes/bot/zai_usage.db (config/providers.yaml :: shadow_mode.db_path)
    _shadow_logger = _ShadowLogger()
except Exception:
    _shadow_logger = None
    _shadow_optimizer = None

# ── Phase 2.2: Routing Advisor (optimizer-first, hot-swappable) ──────────────
# Wraps the shadow optimizer + best_key() into the RoutingAdvisor decision
# layer (src/routing_advisor.py). This is the half-step between shadow mode
# (log only) and primary mode (replace best_key entirely): when the feature
# flag is OFF, best_key() is used exactly as before — zero behaviour change.
# When ON, the optimizer is consulted FIRST and best_key() is the fallback on
# any failure. The advisor NEVER raises — every failure degrades to best_key().
#
# Hot-swap toggle (no restart needed — checked per request):
#   touch ~/.hermes/bot/.optimizer_advisor_mode   → ENABLE
#   rm    ~/.hermes/bot/.optimizer_advisor_mode   → DISABLE
# The ROUTING_ADVISOR_ENABLED env var (1/true/yes/on) is honoured too.
_routing_advisor = None
_ADVISOR_FLAG = os.path.expanduser("~/.hermes/bot/.optimizer_advisor_mode")
try:
    if _shadow_optimizer is not None:
        from src.routing_advisor import (
            RoutingAdvisor as _RoutingAdvisorCls,
            AdvisorDecision as _AdvisorDecision,
        )

        def _best_key_adapter():
            """Adapt the production best_key() (returns str|None) to the
            AdvisorDecision contract the advisor expects. Resolved at call
            time so best_key() (defined later in this module) is in scope."""
            _k = best_key()
            _prov = ("ours" if _k == "ours"
                     else "friend" if _k == "friend"
                     else "fallback")
            return _AdvisorDecision(provider=_prov, model="", key=_k,
                                    source="best_key")

        class _ProxyRoutingAdvisor(_RoutingAdvisorCls):
            """RoutingAdvisor that ALSO honours the .optimizer_advisor_mode
            file marker, so operators can hot-swap without touching env vars."""
            def enabled(self):
                if os.path.exists(_ADVISOR_FLAG):
                    return True
                return super().enabled()

        _routing_advisor = _ProxyRoutingAdvisor(
            _shadow_optimizer, _best_key_adapter,
            env_var="ROUTING_ADVISOR_ENABLED")
except Exception:
    _routing_advisor = None

def _shadow_live_label(chosen_key):
    """Map the proxy's key name ('ours'/'friend') into the optimizer's
    provider namespace ('zai_ours'/'zai_friend') so the agreement comparison
    in ShadowLogger.log_decision is meaningful."""
    return {"ours": "zai_ours", "friend": "zai_friend"}.get(chosen_key, "zai")

# ── Model tier router DISABLED — model selection is now profile-level ──
# Each profile (manager, workers) sets its own model in config.yaml.
# Manager: always GLM-5.2 (user-facing, high quality)
# Workers: glm-4.5-flash (background, bounded tasks)
# The proxy passes through whatever model the profile requests.
_select_model_tier = None

# ── Kalman-backed rate-limit predictor (unlimited retries) ───────────────────
# Models 429 inter-arrival times to predict recovery.  Falls back to capped
# exponential backoff when insufficient data.  A broken import never crashes
# the proxy — _rate_limit_predictor stays None and old backoff is used.
_rate_limit_predictor = None
try:
    from rate_limit_predictor import RateLimitPredictor as _RLP_cls
    _rate_limit_predictor = _RLP_cls()
except Exception:
    pass

_PROACTIVE_COOLDOWN_SECONDS = 300          # 30-min hysteresis after a switch
_PROACTIVE_PREDICTION_TTL   = 60            # cache predictions for 60 s
_proactive_switch_state     = {"key": None, "until": 0.0}
_prediction_cache: dict[str, tuple[list[dict], float]] = {}
_prediction_cache_lock = threading.Lock()


def _fetch_predictions(key_name: str) -> list[dict]:
    """Call predict_exhaustion directly (uncached).  Returns [] if the predictor
    is unavailable or errors — callers treat [] as "no prediction, skip logic"."""
    if _predict_exhaustion is None:
        return []
    try:
        return _predict_exhaustion(key_name)
    except Exception:
        return []


def _get_predictions(key_name: str) -> list[dict]:
    """Cached wrapper around predict_exhaustion — avoids a per-request HTTP
    roundtrip to /quota.  NOTE: predict_exhaustion does a self-HTTP GET to
    /quota internally, so this must NEVER be called while holding ``lock``
    (deadlock) or from inside the /quota handler with a cold cache (recursion)."""
    now = time.time()
    with _prediction_cache_lock:
        cached = _prediction_cache.get(key_name)
        if cached and (now - cached[1]) < _PROACTIVE_PREDICTION_TTL:
            return cached[0]
    preds = _fetch_predictions(key_name)
    with _prediction_cache_lock:
        _prediction_cache[key_name] = (preds, now)
    return preds


def _get_cached_predictions(key_name: str) -> list[dict]:
    """Return cached predictions ONLY — never triggers a fetch.  Safe to call
    inside the /quota handler (avoids self-HTTP recursion deadlock)."""
    with _prediction_cache_lock:
        cached = _prediction_cache.get(key_name)
        return cached[0] if cached else []


def _will_exhaust(predictions: list[dict]) -> dict | None:
    """Return the first window predicted to exhaust, ignoring 'Insufficient data'
    entries (which carry a non-empty ``note``).  Returns None if no window is
    predicted to exhaust or there is insufficient data."""
    for p in predictions:
        if p.get("will_exhaust") and not p.get("note"):
            return p
    return None


def _can_proactive_switch() -> bool:
    """Hysteresis: once a proactive switch happens, don't switch back for
    _PROACTIVE_COOLDOWN_SECONDS (30 min)."""
    return not (_proactive_switch_state["key"] is not None
                and time.time() < _proactive_switch_state["until"])


def _usage_db() -> sqlite3.Connection:
    """Lazy WAL-mode connection to the usage DB; creates schema on first call.
    Double-checked-locked singleton. Returns the shared autocommit connection."""
    global _usage_db_conn
    if _usage_db_conn is not None:
        return _usage_db_conn
    with _usage_db_lock:
        if _usage_db_conn is not None:
            return _usage_db_conn
        conn = sqlite3.connect(str(USAGE_DB), timeout=10, isolation_level=None,
                               check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("""CREATE TABLE IF NOT EXISTS api_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            key_name TEXT,
            key_suffix TEXT,
            model TEXT,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            total_tokens INTEGER,
            tier TEXT,
            cache_hit INTEGER DEFAULT 0,
            ollama_hit INTEGER DEFAULT 0,
            ppq_hit INTEGER DEFAULT 0,
            status_code INTEGER,
            error TEXT,
            duration_ms INTEGER,
            cost_usd REAL DEFAULT NULL,
            cost_source TEXT DEFAULT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS key_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            chosen_key TEXT,
            reason TEXT,
            ours_pct INTEGER,
            friend_pct INTEGER,
            ours_available INTEGER,
            friend_available INTEGER
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_api_calls_ts ON api_calls(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_api_calls_key_model ON api_calls(key_name, model)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_key_decisions_ts ON key_decisions(ts)")
        conn.execute("""CREATE TABLE IF NOT EXISTS model_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            key_name TEXT,
            model TEXT,
            original_model TEXT,
            tier TEXT,
            base_tier TEXT,
            hint TEXT,
            reason TEXT,
            peak INTEGER,
            hours_left REAL,
            active_key TEXT
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_model_decisions_ts ON model_decisions(ts)")
        # ── circuit-breaker state (one row per key, upserted) ───────────────
        # Tracks failure_count, last_failure_ts, last_error_type, backoff_until,
        # disabled_manually for each key. Written by _log_key_health on every
        # state change. PK=key_name so it always reflects the LATEST state.
        conn.execute("""CREATE TABLE IF NOT EXISTS key_health (
            key_name           TEXT PRIMARY KEY,
            healthy            INTEGER NOT NULL,
            failure_count      INTEGER NOT NULL DEFAULT 0,
            last_failure_ts    REAL,
            last_error_type    TEXT,
            backoff_until      REAL,
            disabled_manually  INTEGER NOT NULL DEFAULT 0,
            backoff_seconds    INTEGER DEFAULT 0,
            updated_ts         REAL NOT NULL
        )""")
        # ── provider telemetry (Phase 2.5.1) ───────────────────────────────
        # One row per proxied request: success/fail, latency, token-mismatch.
        _ensure_telemetry_table(conn)
        _usage_db_conn = conn
    return _usage_db_conn


def _parse_usage(response_buffer: bytes) -> dict:
    """Extract the `usage` object from a z.ai response buffer.

    Handles non-streaming plain-JSON responses and streaming SSE `data: {...}`
    buffers. Returns {} if nothing usable is found. Never raises."""
    if not response_buffer:
        return {}
    # Non-streaming: whole buffer is one JSON object
    try:
        obj = json.loads(response_buffer)
        if isinstance(obj, dict) and isinstance(obj.get("usage"), dict):
            return obj["usage"]
    except Exception:
        pass
    # Streaming: scan each `data:` line for an embedded usage object
    try:
        for line in response_buffer.decode("utf-8", "ignore").splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]" or not payload:
                continue
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("usage"), dict):
                return obj["usage"]
    except Exception:
        pass
    return {}


def _classify_response(response_buffer: bytes, error_text: str | None) -> tuple:
    """Classify an upstream response buffer for provider telemetry.

    Returns (response_received, response_valid, error_type):
      * response_received — buffer was non-empty
      * response_valid    — buffer held a usable completion (JSON or SSE with
                            a ``choices`` payload)
      * error_type        — 'none' on success; the upstream ``error_text`` for
                            known failures (HTTP/proxy/network errors); 'api_error'
                            for provider error bodies; 'parse_error' ONLY for a
                            genuinely unparseable 200 body.

    Mirrors _parse_usage's SSE handling. Preserves the real ``error_text``
    instead of clobbering it with 'parse_error' when the buffer is non-JSON —
    e.g. a DNS/connection failure writes a plain-text ``'proxy error: ...'``
    body that should be reported as the connection error it is, not as a
    generic parse_error. Never raises.
    """
    resp_received = len(response_buffer) > 0
    if not resp_received:
        return (False, False, error_text or "no_response")
    try:
        rj = json.loads(response_buffer)
    except Exception:
        rj = None
    if isinstance(rj, dict):
        if "choices" in rj:
            return (True, True, "none")
        if "error" in rj:
            return (True, False, "api_error")
        # Valid JSON but no choices/error — keep the upstream error_text if any.
        return (True, False, error_text or "none")
    # Not single-JSON — likely SSE streaming format. Scan data: lines for a
    # choices/error payload (mirrors _parse_usage).
    try:
        found_valid = False
        found_error = False
        for _line in response_buffer.decode("utf-8", "ignore").splitlines():
            _line = _line.strip()
            if not _line.startswith("data:"):
                continue
            _payload = _line[5:].strip()
            if _payload == "[DONE]" or not _payload:
                continue
            try:
                _cj = json.loads(_payload)
            except Exception:
                continue
            if isinstance(_cj, dict) and "choices" in _cj:
                found_valid = True
                break
            if isinstance(_cj, dict) and "error" in _cj:
                found_error = True
        if found_valid:
            return (True, True, "none")
        if found_error:
            return (True, False, "api_error")
        # Genuinely unparseable body (non-JSON, non-SSE). Preserve the real
        # error_text when we have one (network/DNS/HTTP failures) so the
        # 'parse_error' bucket is not contaminated by known connection errors.
        return (True, False, error_text or "parse_error")
    except Exception:
        return (True, False, error_text or "parse_error")


def _extract_model(body: bytes):
    """Best-effort extraction of the `model` field from a request body."""
    if not body:
        return None
    try:
        obj = json.loads(body)
        if isinstance(obj, dict):
            return obj.get("model")
    except Exception:
        pass
    return None


def _log_api_call(*, key_name=None, key_suffix=None, model=None,
                  prompt_tokens=0, completion_tokens=0, total_tokens=0,
                  tier=None, cache_hit=0, ollama_hit=0, ppq_hit=0,
                  status_code=None, error=None, duration_ms=None,
                  cost_usd=None, cost_source=None):
    """Log one API call event. Swallows all errors — logging must never break a request.

    cost_usd / cost_source (RP-2): the real $ cost of this call and how it was
    determined ('measured' from the response, 'estimated' from a rate model,
    'flat_rate' for subscriptions). Both default to NULL when unknown.
    """
    try:
        _usage_db().execute(
            "INSERT INTO api_calls (ts, key_name, key_suffix, model, prompt_tokens, "
            "completion_tokens, total_tokens, tier, cache_hit, ollama_hit, ppq_hit, "
            "status_code, error, duration_ms, cost_usd, cost_source) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), key_name, key_suffix, model, prompt_tokens, completion_tokens,
             total_tokens, tier, cache_hit, ollama_hit, ppq_hit, status_code, error,
             duration_ms, cost_usd, cost_source))
    except Exception:
        # Fallback: if cost_usd/cost_source columns are absent (pre-RP-1 DB),
        # retry without them so we don't lose the whole row.
        try:
            _usage_db().execute(
                "INSERT INTO api_calls (ts, key_name, key_suffix, model, prompt_tokens, "
                "completion_tokens, total_tokens, tier, cache_hit, ollama_hit, ppq_hit, "
                "status_code, error, duration_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), key_name, key_suffix, model, prompt_tokens, completion_tokens,
                 total_tokens, tier, cache_hit, ollama_hit, ppq_hit, status_code, error,
                 duration_ms))
        except Exception:
            pass


def _log_key_decision(*, chosen_key, reason, ours_pct=0, friend_pct=0,
                      ours_available=0, friend_available=0):
    """Log one key-selection decision. Swallows all errors."""
    try:
        _usage_db().execute(
            "INSERT INTO key_decisions (ts, chosen_key, reason, ours_pct, friend_pct, "
            "ours_available, friend_available) VALUES (?,?,?,?,?,?,?)",
            (time.time(), chosen_key, reason, ours_pct, friend_pct,
             ours_available, friend_available))
    except Exception:
        pass


# ── P3.4 Fix 2: routing_live_decisions table ────────────────────────────────
# Same schema as routing_shadow_decisions (so the two strategies can be
# compared in one query) PLUS a ``pace_mults`` column (JSON text) capturing
# the per-provider pace multipliers LiveRouter actually used. Mirrors the
# inline CREATE-TABLE-then-INSERT pattern of _log_rate_limit / _log_key_decision.
_ROUTING_LIVE_DECISIONS_SQL = """\
CREATE TABLE IF NOT EXISTS routing_live_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    live_provider TEXT,
    live_model TEXT,
    shadow_provider TEXT,
    shadow_model TEXT,
    shadow_cost REAL,
    live_cost REAL,
    tokens INTEGER,
    agree INTEGER,
    reason TEXT,
    pace_mults TEXT
);
"""


def _log_live_decision(*, provider, model=None, fallback=None,
                       fallback_model=None, reason="", pace_mults=None):
    """Log one LIVE LiveRouter failover decision to ``routing_live_decisions``.

    Column mapping (deliberate reuse of the shadow schema for direct
    comparison): ``live_provider``/``live_model`` = the provider LiveRouter
    chose and we routed to; ``shadow_provider``/``shadow_model`` = the
    fallback LiveRouter considered (second-cheapest viable); ``pace_mults``
    = JSON of the per-provider pace multipliers used (Fix 2).

    ``pace_mults`` may be a dict (JSON-encoded) or an already-serialised
    string. Never raises — logging must not break the hot failover path.
    """
    try:
        db = _usage_db()
        db.execute(_ROUTING_LIVE_DECISIONS_SQL)
        if pace_mults is None:
            pace_json = None
        elif isinstance(pace_mults, str):
            pace_json = pace_mults
        else:
            pace_json = json.dumps(pace_mults, default=str)
        db.execute(
            "INSERT INTO routing_live_decisions "
            "(ts, live_provider, live_model, shadow_provider, shadow_model, "
            " shadow_cost, live_cost, tokens, agree, reason, pace_mults) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), provider, model, fallback, fallback_model,
             None, None, 0, 1, reason if reason is not None else "", pace_json))
    except Exception:
        pass


def _consult_live_router():
    """Consult LiveRouter for a failover pick. Returns
    ``(provider, model, fallback, fallback_model)`` or ``(None, None, None,
    None)`` when disabled / unavailable / no viable pick. Never raises — any
    failure yields all-None so the caller falls through to the hardcoded
    ollama->external failover chain.

    This is the single shared entry point used by BOTH the best_key() Phase 5
    gate AND the request-handler retry-loop terminal fallback (P3.4 Fix 1).
    Centralising it here fixes the latent tuple-unpack bug (the old gate did
    ``_provider, _fallback = select_failover(...)`` then used ``_provider`` —
    a ``(provider, model)`` tuple — as the provider string, so the pick was
    never routable) and ensures the retry-loop bypass path actually engages
    LiveRouter under real dual-key-exhaustion.

    Kill switch: ``_LIVE_ROUTING_FLAG`` (``.enable_live_routing``) must exist.
    Side effect: logs the decision to ``routing_live_decisions`` (Fix 2).
    """
    if _LIVE_ROUTER is None or not os.path.exists(_LIVE_ROUTING_FLAG):
        return (None, None, None, None)
    try:
        _pw = None
        with lock:
            _pw = dict(_pace_windows) if _pace_windows else None
        (pick, pick_model), (fb, fb_model) = _LIVE_ROUTER.select_failover(
            quota_state=_snapshot_quota(),
            health_state=_snapshot_health(),
            peak=_is_peak_hour(),
            pace_windows=_pw,
        )
        if not pick:
            return (None, None, None, None)
        # Capture the ACTUAL pace multipliers LiveRouter used (single source
        # of truth — computed inside select_failover under its lock).
        try:
            pace_mults = _LIVE_ROUTER.last_pace_mults
        except Exception:
            pace_mults = None
        _log_live_decision(provider=pick, model=pick_model,
                           fallback=fb, fallback_model=fb_model,
                           reason=f"live_kalman_failover_{pick}",
                           pace_mults=pace_mults)
        return (pick, pick_model, fb, fb_model)
    except Exception:
        return (None, None, None, None)


def _log_rate_limit(*, key_used=None, attempt=0, duration_ms=None):
    try:
        _usage_db().execute(
            "CREATE TABLE IF NOT EXISTS rate_limit_samples ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "ts REAL NOT NULL,"
            "key_name TEXT,"
            "attempt_num INTEGER,"
            "duration_ms INTEGER,"
            "retry_after_estimate INTEGER DEFAULT 0)",
        )
        _usage_db().execute(
            "INSERT INTO rate_limit_samples (ts, key_name, attempt_num, duration_ms) VALUES (?,?,?,?)",
            (time.time(), key_used, attempt, duration_ms))
    except Exception:
        pass


def _log_model_decision(*, key_name=None, model=None, original_model=None,
                        tier=None, base_tier=None, hint=None, reason=None,
                        peak=0, hours_left=None, active_key=None):
    """Log one model-tier decision. Swallows all errors."""
    try:
        _usage_db().execute(
            "INSERT INTO model_decisions (ts, key_name, model, original_model, "
            "tier, base_tier, hint, reason, peak, hours_left, active_key) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), key_name, model, original_model,
             tier, base_tier, hint, reason, peak, hours_left, active_key))
    except Exception:
        pass


def _log_key_health(name: str, state: dict) -> None:
    """Upsert the current per-key circuit-breaker state into ``key_health``.

    One row per key (PRIMARY KEY = key_name) — queryable for dashboards and
    post-mortems. Called from _mark_key_failure / _mark_key_healthy on every
    state transition. Swallows all errors — never breaks a request."""
    try:
        _usage_db().execute(
            "INSERT INTO key_health (key_name, healthy, failure_count, "
            "last_failure_ts, last_error_type, backoff_until, "
            "disabled_manually, backoff_seconds, updated_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(key_name) DO UPDATE SET "
            "healthy=excluded.healthy, failure_count=excluded.failure_count, "
            "last_failure_ts=excluded.last_failure_ts, "
            "last_error_type=excluded.last_error_type, "
            "backoff_until=excluded.backoff_until, "
            "disabled_manually=excluded.disabled_manually, "
            "backoff_seconds=excluded.backoff_seconds, "
            "updated_ts=excluded.updated_ts",
            (name,
             1 if state.get("healthy") else 0,
             int(state.get("consecutive_failures", 0)),
             state.get("last_failure_ts"),
             state.get("last_error_type"),
             state.get("backoff_until", 0),
             1 if state.get("disabled_manually") else 0,
             int(state.get("backoff_seconds", 0)),
             time.time()))
    except Exception:
        pass


# ── provider telemetry (Phase 2.5.1) ────────────────────────────────────────
# One row per proxied request: success/fail, latency, token-mismatch (fraud
# signal).  This is the data foundation for CPVO (cost-per-valid-output) and
# quality probes.  NEVER raises — telemetry failure is silent and must never
# break request handling.

_TELEMETRY_SCHEMA = """CREATE TABLE IF NOT EXISTS provider_telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    provider TEXT NOT NULL,
    response_received INTEGER,
    response_valid INTEGER,
    latency_ms INTEGER,
    error_type TEXT,
    billed_tokens INTEGER,
    actual_tokens INTEGER,
    token_mismatch INTEGER,
    model TEXT
)"""


def _ensure_telemetry_table(conn: sqlite3.Connection | None) -> None:
    """Create the provider_telemetry table if it doesn't exist.

    Idempotent — safe to call on every request or at startup.  Swallows all
    errors so a schema migration failure never breaks request handling.

    Phase 4.5b: also adds the ``model`` column to legacy DBs that predate it
    (idempotent ALTER; the duplicate-column error is swallowed) so the
    model-aware CPVO calculator (``cpvo_calculator.py``) can track quality
    per ``(provider, model)`` pair.
    """
    if conn is None:
        return
    try:
        conn.execute(_TELEMETRY_SCHEMA)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_telemetry_ts "
            "ON provider_telemetry(ts)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_telemetry_provider "
            "ON provider_telemetry(provider)"
        )
        # Ensure legacy DBs (created before the model column was added to
        # _TELEMETRY_SCHEMA) get the column too.  Idempotent: the
        # OperationalError on a duplicate column is expected and swallowed.
        try:
            conn.execute(
                "ALTER TABLE provider_telemetry ADD COLUMN model TEXT"
            )
        except Exception:
            pass
    except Exception:
        pass


def _log_provider_telemetry(
    *,
    conn: sqlite3.Connection | None,
    provider: str | None,
    response_received: bool | None,
    response_valid: bool | None,
    latency_ms: int | None,
    error_type: str | None,
    billed_tokens: int | None,
    actual_tokens: int | None,
    token_mismatch: bool | None,
    model: str | None = None,
) -> None:
    """Insert one telemetry row.  NEVER raises — telemetry failure is silent.

    Called from the _proxy() finally block after every request completes.
    One INSERT per request using the existing shared DB connection.

    Phase 4.5b: ``model`` is the model that served the request.  When present
    it lets ``cpvo_calculator.CPVOCalculator`` track quality per
    ``(provider, model)`` pair.  Defaults to ``None`` for backward
    compatibility (legacy callers / pre-existing rows stay NULL).
    """
    if conn is None:
        return
    try:
        _ensure_telemetry_table(conn)
        conn.execute(
            "INSERT INTO provider_telemetry "
            "(ts, provider, response_received, response_valid, "
            "latency_ms, error_type, billed_tokens, actual_tokens, "
            "token_mismatch, model) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(),
             provider or "unknown",
             int(response_received) if response_received is not None else 0,
             int(response_valid) if response_valid is not None else 0,
             int(latency_ms) if latency_ms is not None else 0,
             error_type or "none",
             int(billed_tokens) if billed_tokens is not None else 0,
             int(actual_tokens) if actual_tokens is not None else 0,
             int(token_mismatch) if token_mismatch is not None else 0,
             model),
        )
    except Exception:
        pass


# ── global spend cap (runaway-loop circuit breaker) ─────────────────────────
# Tracks cumulative daily spend across ALL providers (z.ai, PPQ, OpenRouter).
# When the daily cap for a tier is exceeded, the proxy returns 503 — preventing
# runaway agent loops from burning unlimited tokens.
#
# z.ai models are $0/1M (subscription). External failover models have real
# per-token cost. The cap protects against the expensive external path.

_SPEND_CAP_MANAGER = float(os.environ.get("SPEND_CAP_MANAGER", "10.0"))
_SPEND_CAP_WORKER  = float(os.environ.get("SPEND_CAP_WORKER", "3.0"))

# ── Cost per 1M tokens (RP-4: real_price_tracker is the source of truth) ──────
# Hardcoded rate constants have been replaced by real_price_tracker.
# get_rate_with_fallback() resolves: real data → Ollama API → LAST_RESORT_RATES.
# The values below are EMERGENCY FALLBACKS for when the tracker module fails to
# import — they mirror LAST_RESORT_RATES in src/real_price_tracker.py.
_FALLBACK_OLLAMA_CLOUD_BASE = 0.0155    # was 0.024 (35% wrong); measured = 0.0155
_FALLBACK_OLLAMA_CLOUD_EXTRA = 0.15     # above-quota rate
_FALLBACK_RATES: dict[str, float] = {
    "ollama_cloud": _FALLBACK_OLLAMA_CLOUD_BASE,
    "friend":       0.001,    # shared z.ai subscription → marginal $0
    "ours":         0.001,    # z.ai subscription → marginal $0
    "deepinfra":    1.30,
}


def _rpt_rate(provider: str, model: str | None = None) -> float:
    """Get a $/M rate from real_price_tracker, falling back to inline constants.

    Resolution chain: real_price_tracker.get_rate_with_fallback() →
    inline _FALLBACK_RATES → 0.0 (safe zero for unknown providers).
    Never raises.
    """
    if _rpt_get_rate is not None:
        try:
            return _rpt_get_rate(provider, model)
        except Exception:
            pass
    return _FALLBACK_RATES.get(provider, 0.0)


def _spend_tier(key_name: str | None) -> str:
    """Classify a request by key type for cost tracking.
    Key types: ours (z.ai subscription), friend (courtesy key),
    ollama_cloud (flat-rate), deepinfra (pay-per-use with prompt caching)."""
    if key_name in ("ours", "friend"):
        return key_name
    elif key_name == "ollama_cloud":
        return "ollama_cloud"
    elif key_name == "deepinfra":
        return "deepinfra"
    return "unknown"


def _get_ollama_cloud_cost_per_1m() -> float:
    """Dynamic cost per 1M tokens for ollama_cloud based on quota regime.

    RP-4: Rates are sourced from real_price_tracker.get_rate_with_fallback()
    which uses real measured cost_usd data, falling back to LAST_RESORT_RATES.
    - included:  real measured rate (≈ $0.0155/M)
    - extra:     above-quota rate (≈ $0.15/M, above PPQ $0.14/M → optimizer reroutes)
    - exhausted: float('inf') (effectively removes from routing)
    """
    regime = _get_ollama_quota_status()["regime"]
    if regime == "extra":
        if _rpt_get_rate is not None:
            try:
                return _rpt_get_rate("ollama_cloud_extra")
            except Exception:
                pass
        return _FALLBACK_OLLAMA_CLOUD_EXTRA
    elif regime == "exhausted":
        return float("inf")
    return _rpt_rate("ollama_cloud")


def _estimate_cost_usd(key_name: str | None, total_tokens: int) -> float:
    """Estimate USD cost for a request based on key type. Returns 0.0 for unknown/free keys.

    RP-4: Rates are sourced from real_price_tracker.get_rate_with_fallback()
    which uses real measured cost_usd data from the DB, falling back to
    LAST_RESORT_RATES estimates. For ollama_cloud, applies dynamic pricing
    based on the current quota regime (included/extra/exhausted).
    """
    if not key_name or total_tokens <= 0:
        return 0.0
    if key_name == "ollama_cloud":
        cost_per_1m = _get_ollama_cloud_cost_per_1m()
    else:
        cost_per_1m = _rpt_rate(key_name)
    if cost_per_1m == float("inf"):
        return float("inf")
    return (total_tokens / 1_000_000) * cost_per_1m


def _record_spend(key_name: str | None, model: str | None, total_tokens: int,
                  actual_cost: float | None = None) -> None:
    """Record spend for today. Called from the finally block of every request.

    When actual_cost is provided (e.g., from DeepInfra's estimated_cost field),
    it is used directly instead of computing from the tracker. This
    captures prompt-caching discounts and real-time pricing changes.
    """
    try:
        tier = _spend_tier(key_name)
        cost = actual_cost if actual_cost is not None else _estimate_cost_usd(key_name, total_tokens)
        today = _date.today().isoformat()
        _usage_db().execute(
            "INSERT INTO daily_spend (date, tier, spend_usd, call_count, token_count) "
            "VALUES (?,?,?,1,?) ON CONFLICT(date, tier) "
            "DO UPDATE SET spend_usd = spend_usd + excluded.spend_usd, "
            "call_count = call_count + 1, "
            "token_count = token_count + excluded.token_count",
            (today, tier, cost, total_tokens))
    except Exception:
        pass


# ── DeepInfra local credit balance tracking (no billing API available) ───────
def _init_deepinfra_balance():
    """Initialize DeepInfra balance table with starting balance if not exists."""
    try:
        db = _usage_db()
        db.execute(
            "CREATE TABLE IF NOT EXISTS deepinfra_balance ("
            "id INTEGER PRIMARY KEY CHECK (id = 1),"
            "balance_usd REAL NOT NULL,"
            "last_updated REAL NOT NULL,"
            "total_deducted REAL DEFAULT 0.0,"
            "total_requests INTEGER DEFAULT 0)")
        row = db.execute("SELECT balance_usd FROM deepinfra_balance WHERE id=1").fetchone()
        if not row:
            db.execute(
                "INSERT INTO deepinfra_balance (id, balance_usd, last_updated) VALUES (1, ?, ?)",
                (DEEPINFRA_STARTING_BALANCE, time.time()))
        db.commit()
    except Exception:
        pass


def _deduct_deepinfra_balance(cost: float) -> float:
    """Deduct actual cost from local DeepInfra balance. Returns remaining balance.

    When balance drops below $1.0, marks DeepInfra as unfunded so the failover
    system skips to the next provider (PPQ). Mirrors the existing 402 handler.
    """
    if cost <= 0:
        return _get_deepinfra_balance()
    try:
        db = _usage_db()
        db.execute(
            "UPDATE deepinfra_balance SET "
            "balance_usd = balance_usd - ?, "
            "last_updated = ?, "
            "total_deducted = total_deducted + ?, "
            "total_requests = total_requests + 1 WHERE id=1",
            (cost, time.time(), cost))
        db.commit()
        row = db.execute("SELECT balance_usd FROM deepinfra_balance WHERE id=1").fetchone()
        remaining = row[0] if row else 0.0
        if remaining < 1.0:
            _mark_unfunded("deepinfra")
        return remaining
    except Exception:
        return DEEPINFRA_STARTING_BALANCE


def _get_deepinfra_balance() -> float:
    """Get current DeepInfra balance. Returns starting balance on error."""
    try:
        row = _usage_db().execute("SELECT balance_usd FROM deepinfra_balance WHERE id=1").fetchone()
        return row[0] if row else DEEPINFRA_STARTING_BALANCE
    except Exception:
        return DEEPINFRA_STARTING_BALANCE


# Initialize balance on module load
if DEEPINFRA_KEY:
    _init_deepinfra_balance()


def _extract_cost(provider: str | None, response_buffer: bytes | bytearray,
                  total_tokens: int = 0) -> tuple[float | None, str | None]:
    """Extract the real USD cost for one API call (RP-2).

    Returns ``(cost_usd, cost_source)`` or ``(None, None)``. Never raises.

    Resolution order:
      1. **Measured** — if the provider returns cost in the response body
         (openrouter ``usage.cost``, deepinfra ``usage.estimated_cost``, ppq
         multi-path probe), parse it via src/cost_extraction.py. Source =
         'measured'.
      2. **Flat-rate** — ours/friend (z.ai subscription): marginal cost is $0.
         Source = 'flat_rate'.
      3. **Estimated** — ollama_cloud (flat-rate, but compute an estimated
         per-call cost from the current quota regime rate × tokens so the
         real_price_tracker has a non-zero signal). Source = 'estimated'.
      4. **Unknown** — provider is None/unknown or cost can't be determined.
         Returns (None, None).
    """
    try:
        if not provider:
            return (None, None)
        # 1. Paid providers: parse real cost from the response body.
        if _extract_cost_module is not None:
            cost, source = _extract_cost_module(provider, bytes(response_buffer))
            if cost is not None:
                return (cost, source)
        # 2. z.ai flat-rate subscription — marginal cost is always $0.
        if provider in ("ours", "friend"):
            return (0.0, "flat_rate")
        # 3. ollama_cloud flat-rate — estimate from regime rate × tokens.
        if provider == "ollama_cloud":
            rate = _get_ollama_cloud_cost_per_1m()
            if rate == float("inf"):
                # Exhausted regime — no meaningful cost; let it stay NULL.
                return (None, None)
            return ((total_tokens / 1_000_000) * rate, "estimated")
        # 4. Unknown / unhandled provider.
        return (None, None)
    except Exception:
        return (None, None)


def _check_spend_cap(key_name: str | None) -> tuple[bool, float, float]:
    """Check if the daily spend cap allows this request.

    Returns (allowed, current_spend, cap).
    Fails OPEN — if the DB is unreachable, always allows the request.
    """
    try:
        tier = _spend_tier(key_name)
        # Use manager cap for: ollama_cloud, friend (used for manager-tier work),
        # deepinfra (preferred external failover). Default: worker cap.
        if tier in ("ollama_cloud", "friend", "deepinfra"):
            cap = _SPEND_CAP_MANAGER  # Manager-tier work, generous allowance
        else:  # ours or unknown
            cap = _SPEND_CAP_WORKER    # Default worker cap
        
        today = _date.today().isoformat()
        row = _usage_db().execute(
            "SELECT spend_usd FROM daily_spend WHERE date=? AND tier=?",
            (today, tier)).fetchone()
        current = row[0] if row else 0.0
        return (current < cap, current, cap)
    except Exception:
        return (True, 0.0, 0.0)


def _init_spend_table() -> None:
    """Create the daily_spend table if it doesn't exist."""
    try:
        _usage_db().execute(
            "CREATE TABLE IF NOT EXISTS daily_spend ("
            "date TEXT NOT NULL, "
            "tier TEXT NOT NULL, "
            "spend_usd REAL DEFAULT 0, "
            "call_count INTEGER DEFAULT 0, "
            "token_count INTEGER DEFAULT 0, "
            "PRIMARY KEY (date, tier))")
    except Exception:
        pass


_init_spend_table()


# ── quota polling (background thread) ───────────────────────────────────────

# Mapping from z.ai limit unit codes to human names + hour durations.
# Observed from the z.ai /api/monitor/usage/quota/limit endpoint:
#   TOKENS_LIMIT unit=3 (hour),   number=N → N-hour token window
#   TOKENS_LIMIT unit=6 (week),   number=N → N-week token window (168 h each)
#   TIME_LIMIT   unit=5 (month),  number=N → N-month tool-call window (720 h each)
_UNIT_META = {
    # (type, unit) → (label_for_single, hours_per_unit)
    ("TOKENS_LIMIT", 3): ("hour",   1),
    ("TOKENS_LIMIT", 6): ("weekly", 168),
    ("TIME_LIMIT",   5): ("monthly", 720),
}


def _parse_limit_entry(entry: dict) -> dict | None:
    """Parse a single ``limits[]`` entry from the z.ai quota API into a window dict.

    Returns ``{name, type, used_pct, resets_at, window_hours}`` or *None* if the
    entry is unrecognised (skipped, not counted as an error).
    """
    entry_type = entry.get("type", "")
    unit   = entry.get("unit", 0)
    number = entry.get("number", 0)
    pct    = int(entry.get("percentage", 0))
    reset_ms = entry.get("nextResetTime", 0)
    resets_at = int(reset_ms / 1000) if reset_ms else 0

    meta = _UNIT_META.get((entry_type, unit))
    if meta is None:
        return None                      # unknown window type — skip
    label, hours_per_unit = meta
    window_hours = number * hours_per_unit

    # Friendly names for the common single-unit windows
    if entry_type == "TOKENS_LIMIT" and unit == 3 and number == 5:
        name = "5-hour"
    elif number == 1:
        name = label if label not in ("hour",) else f"{number}-hour"
    else:
        name = f"{number}{label[0]}" if label != "hour" else f"{number}-hour"

    return {"name": name, "type": entry_type, "used_pct": pct,
            "resets_at": resets_at, "window_hours": window_hours}


def _fetch_quota_windows(key: str) -> list[dict]:
    """Fetch **all** quota windows for *key* from the z.ai monitoring API.

    Returns a list of window dicts (see :func:`_parse_limit_entry`).
    On network / parse error returns a single sentinel window with
    ``used_pct=999`` so the caller treats the key as locked.
    """
    try:
        req = urllib.request.Request(QUOTA_URL, headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        limits = data.get("data", {}).get("limits", [])
        windows = [w for w in (_parse_limit_entry(L) for L in limits) if w]
        return windows if windows else [
            {"name": "unknown", "type": "TOKENS_LIMIT",
             "used_pct": 0, "resets_at": 0, "window_hours": 0}]
    except Exception:
        return [{"name": "error", "type": "TOKENS_LIMIT",
                 "used_pct": 999, "resets_at": 0, "window_hours": 0}]


def _max_pct(windows: list[dict]) -> int:
    """Max ``used_pct`` across *windows* (backward-compat with lock logic)."""
    if not windows:
        return 0
    return max(w.get("used_pct", 0) for w in windows)


def is_key_locked(key_name: str, windows: list[dict]):
    """A key is locked if ANY window exceeds its fixed threshold.

    Proportional overage is handled as a cost penalty in the Kalman router
    (burn_predictor.py), NOT as a hard lock here. This lets the system keep
    working when both keys are slightly ahead of schedule.

    Returns (locked, window_name, used_pct, threshold).
    """
    for w in windows:
        name = w.get("name", "")
        pct = w.get("used_pct", 0)
        threshold = LOCK_THRESHOLDS.get(name, {}).get(key_name, 100)
        if pct >= threshold:
            return True, name, pct, threshold
    return False, None, 0, 0


def _refresh_loop():
    while True:
        with lock:
            for name, key in KEYS.items():
                quota_cache[name] = (_fetch_quota_windows(key), time.time())
            STATE_FILE.write_text(json.dumps(
                {n: {"max_pct": _max_pct(v[0]), "windows": v[0],
                     "age_s": int(time.time() - v[1])}
                 for n, v in quota_cache.items()}
                | {"active": _best_unlocked()[0]}, indent=2))
        # Refresh burn predictions (OUTSIDE lock — predict_exhaustion does a
        # safe self-HTTP GET to /quota which itself acquires lock).
        for name in KEYS:
            try:
                _get_predictions(name)
            except Exception:
                pass
        # ── Phase 2.4: Compute pace windows for LiveRouter ───────────────
        # After quota refresh, compute pace_factor input tuples from
        # quota_cache + LiveRouter's ConsumptionKalman burn rates. Stored
        # in _pace_windows for best_key() to pass to select_failover().
        # NEVER blocks quota refresh — wrapped in try/except.
        try:
            global _pace_windows
            if _LIVE_ROUTER is not None:
                with lock:
                    pw = _LIVE_ROUTER.compute_pace_windows(dict(quota_cache))
                if pw:
                    with lock:
                        _pace_windows = pw
        except Exception:
            pass  # pace window computation must never block refresh
        time.sleep(CACHE_TTL)


def _weekly_pct(windows: list[dict]) -> int:
    """Return the ``weekly`` window's used_pct, falling back to max_pct when no
    weekly window is present (e.g. the friend key sometimes lacks one)."""
    for w in windows:
        if w.get("name") == "weekly":
            return w.get("used_pct", 0)
    return _max_pct(windows)


def _best_unlocked():
    """Choose the best key using **per-window** lock thresholds.

    A key is "locked" when *any* of its windows meets/exceeds its threshold in
    :data:`LOCK_THRESHOLDS`.

    Returns ``(chosen, reason, ours_pct, friend_pct, ours_available,
    friend_available)`` — same signature as before so all callers stay
    compatible.

    Selection logic:
      * both locked   → least bad (lowest max_pct); reason ``fallback``
      * exactly one locked → use the other; reason embeds the locked window,
        e.g. ``only_available_friend_locked_weekly_80pct``
      * neither locked → lowest **weekly** percentage (prefer preserving quota);
        reason ``lowest_quota``
      * empty cache   → ``empty_cache`` (defaults to ours)
    """
    if not quota_cache:
        return ("ours", "empty_cache", 0, 0, 0, 0)

    ours_windows   = quota_cache.get("ours",   ([], 0.0))[0]
    friend_windows = quota_cache.get("friend", ([], 0.0))[0]

    op = _max_pct(ours_windows)
    fp = _max_pct(friend_windows)

    o_locked, o_lwin, o_lpct, o_lthr = is_key_locked("ours",   ours_windows)
    f_locked, f_lwin, f_lpct, f_lthr = is_key_locked("friend", friend_windows)

    oa = 0 if o_locked else 1
    fa = 0 if f_locked else 1

    # both locked → least bad (lowest max_pct); tie → ours (preferred)
    if o_locked and f_locked:
        chosen = "ours" if op <= fp else "friend"
        reason = (f"fallback_both_locked_"
                  f"ours_{o_lwin}_{o_lpct}pct_friend_{f_lwin}_{f_lpct}pct")
        return (chosen, reason, op, fp, 0, 0)

    # exactly one locked → use the other; note which window triggered the lock
    if o_locked:
        reason = f"only_available_ours_locked_{o_lwin}_{o_lpct}pct"
        return ("friend", reason, op, fp, 0, 1)
    if f_locked:
        reason = f"only_available_friend_locked_{f_lwin}_{f_lpct}pct"
        return ("ours", reason, op, fp, 1, 0)

    # neither locked → prefer the CHEAPER key (cost-aware tie-break).
    # Per _KEY_COST_MULTIPLIER, ours (1.0) is cheaper than friend (1.21), so
    # we default to ours. We own it; friend's key is a courtesy fallback.
    # If ours has been manually disabled or is mid-backoff, _is_key_healthy
    # catches that in Phase 4 of best_key() and switches to friend there.
    ours_cost  = _KEY_COST_MULTIPLIER.get("ours",   1.0)
    friend_cost = _KEY_COST_MULTIPLIER.get("friend", 1.0)
    if friend_cost < ours_cost:
        chosen, reason = "friend", (f"cost_aware_friend_{friend_cost}_cheaper_"
                                    f"ours_{ours_cost}_o{op}pct_f{fp}pct")
    else:
        chosen, reason = "ours", (f"cost_aware_prefer_ours_both_unlocked_"
                                  f"ours_{ours_cost}_friend_{friend_cost}_"
                                  f"o{op}pct_f{fp}pct")
    return (chosen, reason, op, fp, 1, 1)


def best_key() -> str:
    """Pick a key for this request using PROACTIVE prediction first.

    Proactive (primary): use Kalman burn-rate predictions to select the key
    least likely to exhaust before its window resets.  Predictions are fetched
    OUTSIDE the quota lock (the predictor does a safe self-HTTP GET to /quota).

    Reactive (fallback): when predictions are unavailable (cold start, no data),
    fall back to per-window lock thresholds in _best_unlocked().

    Safety: a predictor failure never breaks key selection — every path is
    wrapped so the proxy always returns a valid key.
    """
    # Phase 1 — PROACTIVE: use Kalman predictions as the primary signal -------
    chosen = None
    reason = ""
    try:
        our_preds = _get_predictions("ours")
        friend_preds = _get_predictions("friend")
        our_exhaust = _will_exhaust(our_preds)
        friend_exhaust = _will_exhaust(friend_preds)

        if our_exhaust is not None and friend_exhaust is None:
            # Our key predicted to exhaust, friend is safe
            chosen = "friend"
            reason = (f"proactive_ours_exhausts_{our_exhaust.get('window','?')}"
                      f"_friend_safe")
        elif friend_exhaust is not None and our_exhaust is None:
            # Friend predicted to exhaust, our key is safe
            chosen = "ours"
            reason = (f"proactive_friend_exhausts_{friend_exhaust.get('window','?')}"
                      f"_ours_safe")
        elif our_exhaust is not None and friend_exhaust is not None:
            # Both exhausting — pick the one that lasts longer
            our_hours = our_exhaust.get("exhausts_in_hours") or 0
            friend_hours = friend_exhaust.get("exhausts_in_hours") or 0
            if friend_hours > our_hours:
                chosen = "friend"
                reason = ("proactive_both_exhausting_prefer_friend_longer_"
                          f"{friend_hours:.1f}h_ours_{our_hours:.1f}h")
            else:
                chosen = "ours"
                reason = ("proactive_both_exhausting_prefer_ours_longer_"
                          f"{our_hours:.1f}h_friend_{friend_hours:.1f}h")
    except Exception:
        pass  # predictor failure → fall through to reactive

    # Also record quota percentages for the log (read outside lock if possible)
    op = fp = 0
    try:
        with lock:
            op = _max_pct(quota_cache.get("ours", ([], 0.0))[0])
            fp = _max_pct(quota_cache.get("friend", ([], 0.0))[0])
    except Exception:
        pass

    # Phase 2 — REACTIVE fallback (when predictions not available) ------------
    if chosen is None:
        with lock:
            chosen, reason, op, fp, oa, fa = _best_unlocked()
    else:
        # Proactive gave us a choice — still determine availability flags
        # from reactive thresholds for the log
        with lock:
            ours_w = quota_cache.get("ours", ([], 0.0))[0]
            friend_w = quota_cache.get("friend", ([], 0.0))[0]
            o_locked, *_ = is_key_locked("ours", ours_w)
            f_locked, *_ = is_key_locked("friend", friend_w)
            oa = 0 if o_locked else 1
            fa = 0 if f_locked else 1

    # Phase 3 — RECOVER: if the non-chosen (previously locked) key has recovered
    # below threshold, prefer it without waiting for next 5-min refresh.  This
    # runs regardless of whether we used proactive or reactive selection.
    try:
        locked_key = "friend" if chosen == "ours" else "ours"
        locked_windows = quota_cache.get(locked_key, ([], 0.0))[0]
        locked_now, *_ = is_key_locked(locked_key, locked_windows)
        if not locked_now:
            # Locked key has recovered — re-evaluate (but only from reactive,
            # to avoid oscillation from stale predictions)
            with lock:
                reactive_choice, reactive_reason, _, _, _, _ = _best_unlocked()
            if reactive_choice != chosen:
                chosen = reactive_choice
                reason = f"proactive_recover_{locked_key}_unlocked"
    except Exception:
        pass  # NEVER break key selection

    # Phase 4 — HEALTH CHECK: skip exhausted keys (empty response / 429)
    if chosen and not _is_key_healthy(chosen):
        other = "friend" if chosen == "ours" else "ours"
        if _is_key_healthy(other):
            chosen = other
            reason = f"health_switch_{other}_other_exhausted"
        else:
            chosen = None
            reason = "both_keys_exhausted"

    # Phase 5 — LIVE ROUTER FAILOVER (Phase 1.2) ─────────────────────────
    # ONLY fires when both z.ai keys are exhausted (chosen is None after
    # Phase 4 health check).  Asks LiveRouter for the cheapest viable
    # external provider via Kalman-converged pricing.  Kill switch:
    # ~/.hermes/bot/.enable_live_routing must exist.  Every call wrapped
    # in try/except — on any failure, falls through to None and the
    # hardcoded ollama → ppq → openrouter chain in _proxy() runs.
    # Phase 5 — LIVE ROUTER FAILOVER (Phase 1.2) ─────────────────────────
    # Fires when best_key()'s INITIAL health check already sees both z.ai
    # keys exhausted (chosen is None). Asks LiveRouter for the cheapest
    # viable external provider via Kalman-converged pricing. Kill switch
    # (.enable_live_routing) + safe fallthrough live inside _consult_live_router.
    #
    # NOTE: this is the LESS common path. In production best_key() usually
    # returns a key whose health cache lags the real 429; that key 429s
    # mid-request and the request-handler retry loop exhausts both keys
    # DURING the loop. That retry-loop terminal fallback now also calls
    # _consult_live_router() (P3.4 Fix 1) — previously it bypassed
    # LiveRouter entirely (841 dual-exhaustion events/2h, 0 live events).
    if chosen is None:
        _pick, _pick_model, _fb, _fb_model = _consult_live_router()
        if _pick:
            chosen = _pick
            reason = f"live_kalman_failover_{_pick}"
            _log_key_decision(chosen_key=chosen, reason=reason,
                              ours_pct=op, friend_pct=fp,
                              ours_available=oa, friend_available=fa)
            return chosen

    _log_key_decision(chosen_key=chosen, reason=reason, ours_pct=op,
                      friend_pct=fp, ours_available=oa, friend_available=fa)
    return chosen


# Constants for retry logic
TRANSIENT_ERRORS = {404, 429, 500, 502, 503, 504}
RETRYABLE_EXCEPTIONS = (
    "Broken pipe",
    "Connection reset",
    "Connection timed out",
    "Remote end closed connection without response",
)

def _is_retryable_error(error):
    """Check if an error should trigger a retry."""
    if isinstance(error, urllib.error.HTTPError):
        return error.code in TRANSIENT_ERRORS
    error_str = str(error)
    return any(err in error_str for err in RETRYABLE_EXCEPTIONS)

def _attempt_retry(e, attempt, name, t0, key_order):
    """Retry with binary exponential backoff.

    Between key switches: short jittered delay (prevents hammering endpoint).
    Full cycle (all keys tried): exponential backoff with Kalman override.
    """
    import random

    if attempt >= len(key_order) - 1:
        # All keys exhausted — full backoff cycle
        _log_rate_limit(key_used=name, attempt=attempt, duration_ms=int((time.time() - t0) * 1000))
        retry_num = attempt - len(key_order) + 1
        if retry_num >= 50:
            return False  # Safety cap exhausted
        elif _rate_limit_predictor is not None:
            _rate_limit_predictor.record_429()
            wait = _rate_limit_predictor.predict_retry_at()
            time.sleep(wait)
            return True
        else:
            # Binary exponential: 2s, 4s, 8s, 16s, 32s, 60s cap
            wait = min(2 ** (retry_num + 1), 60)
            wait *= (0.75 + random.random() * 0.5)
            time.sleep(wait)
            return True
    else:
        # Between key switches — brief delay to let endpoint recover
        time.sleep(1 + random.random())  # 1-2s jitter
        return True

# ── proxy handler ───────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _try_ollama_cloud(self, body: bytes, model: str | None,
                           response_buffer: bytearray, t0: float) -> bool:
        """Forward request to Ollama Cloud API (primary provider, not failover).

        Ollama Cloud is a $20/mo flat-rate subscription with no per-token cost.
        During z.ai peak hours (UTC 6-10), z.ai burns 3x quota — Ollama has no
        peak pricing, making it the preferred provider during peak.

        Returns True on success (response already sent),
        False on failure (caller should try next provider).
        """
        if not OLLAMA_CLOUD_KEY:
            return False
        if not _is_key_healthy("ollama_cloud"):
            return False

        # Map model names: z.ai names work directly on Ollama Cloud API
        # (glm-5.2 → glm-5.2, no :cloud suffix needed for direct API)
        ollama_model = model or "glm-5.2"

        try:
            body_json = json.loads(body) if body else {}
            body_json["model"] = ollama_model
            fwd_body = json.dumps(body_json).encode()

            url = OLLAMA_CLOUD_BASE + "/chat/completions"
            hdrs = {
                "Authorization": f"Bearer {OLLAMA_CLOUD_KEY}",
                "Content-Type": "application/json",
            }

            req = urllib.request.Request(url, data=fwd_body, method="POST", headers=hdrs)
            with urllib.request.urlopen(req, timeout=180) as resp:
                self.send_response(resp.status)
                for h, v in resp.headers.items():
                    if h.lower() not in ("transfer-encoding", "connection"):
                        self.send_header(h, v)
                self.send_header("X-Provider", "ollama_cloud")
                self.end_headers()
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    response_buffer.extend(chunk)
                    self.wfile.write(chunk)
                    self.wfile.flush()

                # Parse usage for spend tracking
                ollama_usage = _parse_usage(bytes(response_buffer))
                ollama_tokens = int(ollama_usage.get("total_tokens") or 0)
                _record_spend("ollama_cloud", ollama_model, ollama_tokens)
                self._spend_recorded = True
                _mark_key_healthy("ollama_cloud")
                # RP-2: extract real cost (estimated from regime rate × tokens)
                _oc_cost, _oc_cost_src = _extract_cost(
                    "ollama_cloud", bytes(response_buffer), ollama_tokens)
                _log_api_call(
                    key_name="ollama_cloud", key_suffix=OLLAMA_CLOUD_KEY[-4:],
                    model=ollama_model,
                    prompt_tokens=int(ollama_usage.get("prompt_tokens") or 0),
                    completion_tokens=int(ollama_usage.get("completion_tokens") or 0),
                    total_tokens=ollama_tokens,
                    tier="ollama_cloud", status_code=resp.status, error=None,
                    duration_ms=int((time.time() - t0) * 1000),
                    cost_usd=_oc_cost, cost_source=_oc_cost_src,
                )
                # Log key decision so dashboard shows the switch to ollama_cloud
                _log_key_decision(
                    chosen_key="ollama_cloud",
                    reason="peak_hour_ollama_primary" if _is_peak_hour() else "zai_both_keys_exhausted_ollama_fallback",
                )
                return True

        except urllib.error.HTTPError as he:
            if he.code == 429:
                _mark_key_exhausted("ollama_cloud")
            return False
        except Exception:
            return False

    def _try_telnyx(self, body: bytes, model: str | None,
                     response_buffer: bytearray, t0: float) -> bool:
        """Forward request to Telnyx as failover for Kimi models.

        Uses the demo endpoint (https://telnyx.com/api/inference) which
        requires no API key — only browser-like Origin/Referer headers.
        Rate-limited to 10 req/min per IP. Returns SSE stream.

        If a production API key (TELNYX_KEY) is available, uses the
        production endpoint instead (no rate limit).

        Returns True on success (response already sent),
        False on failure (caller should send 503).
        """
        # Skip if Telnyx was recently rate-limited (circuit breaker)
        if not _is_key_healthy("telnyx"):
            return False

        # Map model name to Telnyx model ID
        telnyx_model = _PROVIDER_MODEL_NAMES.get("telnyx", {}).get(
            model or "", model or "")
        if not telnyx_model:
            return False

        try:
            body_json = json.loads(body) if body else {}
            body_json["model"] = telnyx_model
            fwd_body = json.dumps(body_json).encode()

            # Use production API if key available, else demo endpoint
            if TELNYX_KEY:
                url = TELNYX_BASE + "/chat/completions"
                hdrs = {
                    "Authorization": f"Bearer {TELNYX_KEY}",
                    "Content-Type": "application/json",
                }
            else:
                url = TELNYX_DEMO_URL
                hdrs = {
                    "Content-Type": "application/json",
                    "Origin": "https://telnyx.com",
                    "Referer": "https://telnyx.com/products/inference",
                    "User-Agent": "Mozilla/5.0",
                }

            req = urllib.request.Request(url, data=fwd_body, method="POST", headers=hdrs)
            with urllib.request.urlopen(req, timeout=180) as resp:
                self.send_response(resp.status)
                for h, v in resp.headers.items():
                    if h.lower() not in ("transfer-encoding", "connection"):
                        self.send_header(h, v)
                self.send_header("X-Provider", "telnyx")
                self.end_headers()
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    response_buffer.extend(chunk)
                    self.wfile.write(chunk)
                    self.wfile.flush()

                # Parse usage for spend tracking
                telnyx_usage = _parse_usage(bytes(response_buffer))
                telnyx_tokens = int(telnyx_usage.get("total_tokens") or 0)
                _record_spend("telnyx", telnyx_model, telnyx_tokens)
                self._spend_recorded = True
                telnyx_cost, telnyx_cost_src = _extract_cost(
                    "telnyx", bytes(response_buffer), telnyx_tokens)
                _mark_key_healthy("telnyx")
                _log_api_call(
                    key_name="telnyx",
                    key_suffix=TELNYX_KEY[-4:] if TELNYX_KEY else "demo",
                    model=telnyx_model,
                    prompt_tokens=int(telnyx_usage.get("prompt_tokens") or 0),
                    completion_tokens=int(telnyx_usage.get("completion_tokens") or 0),
                    total_tokens=telnyx_tokens,
                    tier="telnyx", status_code=resp.status, error=None,
                    duration_ms=int((time.time() - t0) * 1000),
                    cost_usd=telnyx_cost, cost_source=telnyx_cost_src,
                )
                _log_key_decision(
                    chosen_key="telnyx",
                    reason="ollama_cloud_failed_telnyx_fallback",
                )
                return True

        except urllib.error.HTTPError as he:
            if he.code == 429:
                # Rate limited — mark telnyx as temporarily unhealthy
                _mark_key_failure("telnyx", error_type="exhausted")
            return False
        except Exception:
            return False

    def _try_external_failover(self, body: bytes, model: str | None,
                                response_buffer: bytearray, t0: float,
                                preferred: str | None = None) -> bool:
        """Try forwarding to the cheapest funded external provider when z.ai fails.

        Dynamically selects the provider with the lowest cost that still has
        credits remaining. On 402 (out of credits), marks that provider
        unfunded for 1 hour and tries the next cheapest.

        Args:
            preferred: Optional provider name (e.g. LiveRouter's pick) to try
                FIRST, ahead of the cost-sorted order. If it is funded + keyed
                it is attempted before the rest; on failure the remaining
                candidates are tried cheapest-first as normal. Honours the
                LiveRouter pick (P3.4 Fix 1) without weakening the safe
                cost-ordered fallback.

        Returns True on success (response already sent),
        False on failure (caller should send error response).
        """
        # Choose failover model based on requesting profile's quality tier.
        # Manager (glm-5.2): quality floor at deepseek-v4-pro (55.4% SWE-bench).
        # Workers (glm-4.5-flash): cheapest available (output gets vetted).
        if model == "glm-5.2":
            ext_model = MANAGER_FALLBACK_MODEL
        else:
            ext_model = WORKER_FALLBACK_MODEL

        # Collect funded providers with their cost
        candidates = []
        for name, prov in EXTERNAL_PROVIDERS.items():
            if not prov.get("key"):
                continue
            if not _is_provider_funded(name):
                continue
            cost = _get_provider_cost(name, ext_model)
            candidates.append((cost, name, prov))

        # Sort cheapest first; ties broken by _PROVIDER_PRIORITY (lower = tried first)
        candidates.sort(key=lambda c: (c[0], _PROVIDER_PRIORITY.get(c[1], 99)))

        # Honour a LiveRouter-chosen provider (P3.4 Fix 1): if `preferred` is
        # funded + keyed, move it to the front so it is tried FIRST; the rest
        # keep their cost order as the safe fallback. No-op when preferred is
        # absent, unknown, or not a viable candidate.
        if preferred:
            pref = [c for c in candidates if c[1] == preferred]
            if pref:
                candidates = pref + [c for c in candidates if c[1] != preferred]

        if not candidates:
            return False

        for cost, provider_name, prov in candidates:
            try:
                body_json = json.loads(body) if body else {}
                # Per-provider model name translation.
                # PPQ/OpenRouter use "deepseek/deepseek-v4-pro" but DeepInfra expects
                # "deepseek-ai/DeepSeek-V4-Pro" (case-sensitive, dotted form).
                actual_model = _PROVIDER_MODEL_NAMES.get(provider_name, {}).get(ext_model, ext_model)
                body_json["model"] = actual_model
                fwd_body = json.dumps(body_json).encode()

                url = prov["base_url"] + "/chat/completions"
                hdrs = {
                    "Authorization": f"Bearer {prov['key']}",
                    "Content-Type": "application/json",
                }
                if provider_name == "openrouter":
                    hdrs["HTTP-Referer"] = "https://hermes.local"
                    hdrs["X-Title"] = "Hermes Agent"

                req = urllib.request.Request(url, data=fwd_body, method="POST", headers=hdrs)
                try:
                    with urllib.request.urlopen(req, timeout=180) as resp:
                        self.send_response(resp.status)
                        for h, v in resp.headers.items():
                            if h.lower() not in ("transfer-encoding", "connection"):
                                self.send_header(h, v)
                        self.send_header("X-Failover-Provider", provider_name)
                        self.end_headers()
                        while True:
                            chunk = resp.read(4096)
                            if not chunk:
                                break
                            response_buffer.extend(chunk)
                            self.wfile.write(chunk)
                            self.wfile.flush()
                        _mark_funded(provider_name)
                        # Parse usage from the streamed response for spend tracking
                        ext_usage = _parse_usage(bytes(response_buffer))
                        ext_tokens = int(ext_usage.get("total_tokens") or 0)
                        # RP-2: extract real cost from the response body.
                        # Unifies per-provider cost parsing (openrouter usage.cost,
                        # deepinfra usage.estimated_cost, ppq multi-path probe)
                        # into one call. Returns (None, None) when the provider
                        # doesn't return a cost field.
                        ext_cost_usd, ext_cost_source = _extract_cost(
                            provider_name, bytes(response_buffer), ext_tokens)
                        # Use extracted cost for spend tracking (falls back to
                        # _estimate_cost_usd inside _record_spend when None).
                        _record_spend(provider_name, ext_model, ext_tokens,
                                      actual_cost=ext_cost_usd)
                        # Deduct from DeepInfra local credit balance
                        if provider_name == "deepinfra" and ext_cost_usd is not None and ext_cost_usd > 0:
                            remaining = _deduct_deepinfra_balance(ext_cost_usd)
                        self._spend_recorded = True
                        _log_api_call(
                            key_name=provider_name, key_suffix=prov["key"][-4:],
                            model=ext_model,
                            prompt_tokens=int(ext_usage.get("prompt_tokens") or 0),
                            completion_tokens=int(ext_usage.get("completion_tokens") or 0),
                            total_tokens=ext_tokens,
                            tier=provider_name, status_code=resp.status, error=None,
                            duration_ms=int((time.time() - t0) * 1000),
                            cost_usd=ext_cost_usd, cost_source=ext_cost_source,
                        )
                        # Log key decision so dashboard shows the failover switch
                        _log_key_decision(
                            chosen_key=provider_name,
                            reason=f"zai_exhausted_{provider_name}_failover",
                        )
                        return True
                except urllib.error.HTTPError as he:
                    if he.code == 402:
                        _mark_unfunded(provider_name)
                        continue
                    raise
            except Exception:
                continue

        return False

    def _proxy(self):
        # We strip Transfer-Encoding from upstream responses (below) yet pass no
        # Content-Length for streamed bodies, so connection-close is the body
        # delimiter. Force it — otherwise HTTP/1.1 keep-alive leaves the socket
        # open and clients hang waiting for body-end (the /quota + BrokenPipe
        # symptoms). Sending the "Connection: close" header alone is NOT enough;
        # BaseHTTPRequestHandler keys off self.close_connection.
        self.close_connection = True
        t0 = time.time()
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        self._spend_recorded = False  # set True by _try_external_failover on success

        # ── Quota-aware model tier routing (auto-downgrade) ────────────────
        # Step 1: Extract original model + client tier hint
        original_model = _extract_model(body)
        tier_hint = self.headers.get("X-Model-Tier", "")

        # Step 1b: Global spend cap — circuit breaker for runaway loops
        allowed, current_spend, cap = _check_spend_cap("unknown")  # Pre-key selection check
        if not allowed:
            tier = _spend_tier(original_model)
            err = json.dumps({
                "error": f"daily spend cap exceeded for {tier}",
                "spend_usd": round(current_spend, 4),
                "cap_usd": cap,
                "reset_at": "midnight local"
            }).encode()
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)
            return

        # Step 1c: Ollama-only models — route directly to Ollama Cloud
        # These models don't exist on z.ai, so skip z.ai entirely
        _OLLAMA_ONLY_MODELS = {"kimi-k2.7-code", "kimi-k3:cloud", "gpt-oss:120b", "gemma4:31b", "qwen3.5:397b"}
        if original_model in _OLLAMA_ONLY_MODELS and OLLAMA_CLOUD_KEY:
            response_buffer = bytearray()
            if self._try_ollama_cloud(body, original_model, response_buffer, t0):
                return
            # Try Telnyx fallback for Kimi models before returning 503
            if original_model in _TELNYX_FALLBACK_MODELS:
                telnyx_buffer = bytearray()
                if self._try_telnyx(body, original_model, telnyx_buffer, t0):
                    return
            # If both Ollama Cloud and Telnyx fail, return 503
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(f'{{"error":"both ollama cloud and telnyx failed for ollama-only model {original_model}"}}'.encode())
            return

        # Step 1d + Step 2 — choose a routing key.
        #
        # ADVISOR MODE (Phase 2.2, hot-swappable): when the feature flag is ON
        # (touch ~/.hermes/bot/.optimizer_advisor_mode  OR
        #  ROUTING_ADVISOR_ENABLED=1) the price-first RoutingOptimizer is
        # consulted FIRST; best_key() is the fallback on any failure. Peak-hour
        # pricing is now the optimizer's job — during z.ai peak (UTC 6-10) it
        # charges z.ai 3x, making ollama_cloud cheaper, so it routes there
        # automatically. best_key() is NEVER removed — it is the fallback on
        # ANY optimizer exception or "no viable provider" result.
        #
        # Flag OFF → behaviour is UNCHANGED: the original peak-hour Ollama
        # pre-check + best_key() cascade runs exactly as before.
        # `peak` is computed once here so downstream (failover chain, logging)
        # always sees a defined value regardless of which branch ran.
        peak = _is_peak_hour()
        if _routing_advisor is not None and _routing_advisor.enabled():
            chosen = None
            try:
                _adv = _routing_advisor.decide(
                    difficulty="medium", estimated_tokens=0)
                # Optimizer may route directly to ollama_cloud (self-hosted,
                # bypasses z.ai). If it does and ollama fails, fall through to
                # best_key() (chosen stays None → failover chain below).
                if _adv.routed_directly_to_ollama and OLLAMA_CLOUD_KEY:
                    response_buffer = bytearray()
                    if self._try_ollama_cloud(
                            body, original_model, response_buffer, t0):
                        return
                chosen = _adv.key
                _log_key_decision(
                    chosen_key=chosen or "",
                    reason=("optimizer_advisor:"
                            + (_adv.reason or "price_optimal"))[:120])
            except Exception:
                pass  # advisor must never break routing
            if chosen is None:
                chosen = best_key()
        else:
            # ORIGINAL CASCADE — flag off or advisor module unavailable.
            # Step 1d: Peak-hour routing — consult LiveRouter first (P3.4-fix),
            # then fall through to peak-hour Ollama pre-check.
            if peak:
                _pick, _pick_model, _fb, _fb_model = _consult_live_router()
                if _pick:
                    _log_key_decision(
                        chosen_key=_pick,
                        reason=f"live_kalman_failover_{_pick}")
                    response_buffer = bytearray()
                    if _pick == "ollama_cloud" and OLLAMA_CLOUD_KEY:
                        if self._try_ollama_cloud(
                                body, original_model, response_buffer, t0):
                            return
                    elif _pick in EXTERNAL_PROVIDERS or _pick == "deepinfra":
                        if self._try_external_failover(body, original_model,
                                                       response_buffer, t0,
                                                       preferred=_pick):
                            return
                    # LiveRouter pick failed — fall through
            # Peak-hour Ollama pre-check (fallback if LiveRouter disabled/no pick)
            if peak and OLLAMA_CLOUD_KEY:
                response_buffer = bytearray()
                if self._try_ollama_cloud(
                        body, original_model, response_buffer, t0):
                    return
            # Step 2: Choose key.
            chosen = best_key()

        # Shadow mode (Phase 2.1, ADR-014): record what the price-first
        # optimizer WOULD have chosen, alongside the live pick. READ-ONLY —
        # never changes `chosen`. In advisor mode the live pick already came
        # from the optimizer, so this logs the agreement (useful monitoring);
        # any failure is swallowed so production is unaffected.
        #
        # T7 / C1 fix (docs/shadow-7d-report.md §3/§6): ALSO compute the
        # LiveRouter's pressure-routing pick so the P6 divergence and 429
        # exit-gate columns get genuine data.  Before this fix the live path
        # called log_decision() (legacy API) which left pressure_provider,
        # actual_cost, divergence, is_429 all NULL — making the exit gate
        # degenerate (passed trivially on empty inputs).  The pressure pick
        # bypasses the kill switch intentionally — that's the point of SHADOW
        # mode (log, don't route).
        if _shadow_logger and _shadow_optimizer and chosen:
            try:
                _sd = _shadow_optimizer.route(difficulty="medium", estimated_tokens=0)
                if _sd:
                    # ── C1: compute LiveRouter pressure pick (best-effort) ──
                    _pr_prov = _pr_mod = _pr_cost = _act_cost = None
                    if _LIVE_ROUTER is not None:
                        try:
                            _pw_s = None
                            with lock:
                                _pw_s = dict(_pace_windows) if _pace_windows else None
                            (_pr_prov, _pr_mod), _ = _LIVE_ROUTER.select_failover(
                                quota_state=_snapshot_quota(),
                                health_state=_snapshot_health(),
                                peak=_is_peak_hour(),
                                pace_windows=_pw_s,
                            )
                            _rates = _converged_rates or {}
                            if _pr_prov:
                                _pr_cost = (_rates.get(_pr_prov)
                                            or _rates.get(str(_pr_prov).replace("zai_", "")))
                            _ll = _shadow_live_label(chosen)
                            _act_cost = (_rates.get(_ll)
                                         or _rates.get(chosen)
                                         or _rates.get(_ll.replace("zai_", "")))
                        except Exception:
                            pass  # pressure pick is best-effort only
                    # ── Log with pressure dimension if available ──
                    if hasattr(_shadow_logger, 'log_decision_with_pressure'):
                        _shadow_logger.log_decision_with_pressure(
                            ts=time.time(),
                            live_provider=_shadow_live_label(chosen),
                            live_model=original_model,
                            shadow_provider=_sd.get("chosen_provider"),
                            shadow_model=_sd.get("chosen_model"),
                            shadow_cost=_sd.get("effective_cost_per_1m"),
                            tokens=0,
                            reason=(_sd.get("reason") or "")[:200],
                            live_cost=_act_cost,
                            quota_regime=_get_ollama_quota_status().get("regime"),
                            pressure_provider=_pr_prov,
                            pressure_model=_pr_mod,
                            pressure_cost=_pr_cost,
                            actual_cost=_act_cost,
                        )
                    else:
                        _shadow_logger.log_decision(
                            ts=time.time(),
                            live_provider=_shadow_live_label(chosen),
                            live_model=original_model,
                            shadow_provider=_sd.get("chosen_provider"),
                            shadow_model=_sd.get("chosen_model"),
                            shadow_cost=_sd.get("effective_cost_per_1m"),
                            tokens=0,
                            reason=(_sd.get("reason") or "")[:200],
                            live_cost=None,
                            quota_regime=_get_ollama_quota_status().get("regime"),
                        )
            except Exception:
                pass  # Shadow mode never blocks production

        # If both z.ai keys exhausted, consult LiveRouter first (P3.4-fix),
        # then fall through to Ollama Cloud / PPQ hardcoded chain.
        if chosen is None:
            # LiveRouter consultation (kill switch checked inside)
            _pick, _pick_model, _fb, _fb_model = _consult_live_router()
            if _pick:
                _log_key_decision(
                    chosen_key=_pick,
                    reason=f"live_kalman_failover_{_pick}")
                response_buffer = bytearray()
                if _pick == "ollama_cloud" and OLLAMA_CLOUD_KEY:
                    if self._try_ollama_cloud(body, original_model, response_buffer, t0):
                        return
                elif _pick in EXTERNAL_PROVIDERS or _pick == "deepinfra":
                    if self._try_external_failover(body, original_model,
                                                   response_buffer, t0,
                                                   preferred=_pick):
                        return
                # LiveRouter pick failed — fall through to hardcoded chain
            response_buffer = bytearray()
            if OLLAMA_CLOUD_KEY and self._try_ollama_cloud(body, original_model, response_buffer, t0):
                return
            if self._try_external_failover(body, original_model, response_buffer, t0):
                return
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"all providers exhausted, retry later"}')
            return

        # Phase 1.2: LiveRouter returned an external provider (not a z.ai
        # key).  Route to the appropriate external handler.  This ONLY
        # happens when both z.ai keys are exhausted AND the kill switch
        # (.enable_live_routing) is active.  If the external provider fails,
        # fall through to the hardcoded failover chain below.
        if chosen not in KEYS:
            response_buffer = bytearray()
            if chosen == "ollama_cloud" and OLLAMA_CLOUD_KEY:
                if self._try_ollama_cloud(body, original_model, response_buffer, t0):
                    return
            elif chosen in EXTERNAL_PROVIDERS or chosen == "deepinfra":
                # Try the LiveRouter-chosen provider first, then the rest
                if self._try_external_failover(body, original_model,
                                               response_buffer, t0):
                    return
            # LiveRouter provider failed — fall through to hardcoded chain
            response_buffer = bytearray()
            if OLLAMA_CLOUD_KEY and self._try_ollama_cloud(body, original_model, response_buffer, t0):
                return
            if self._try_external_failover(body, original_model, response_buffer, t0):
                return
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"all providers exhausted, retry later"}')
            return

        # Step 3: Compute tier for chosen key from Kalman + peak hours + client hint
        model_tier_info = None
        if _select_model_tier is not None and body:
            try:
                model_tier_info = _select_model_tier(chosen, tier_hint if tier_hint else None)
                new_model = model_tier_info.get("model")
                if original_model and new_model and new_model != original_model:
                    body_json = json.loads(body)
                    body_json["model"] = new_model
                    body = json.dumps(body_json).encode()
                    self.headers["Content-Length"] = str(len(body))
            except Exception:
                pass

        # Step 4: Extract final model (may have been rewritten)
        model = _extract_model(body)

        # Step 5: Log the model decision
        if model_tier_info:
            _log_model_decision(
                key_name=chosen,
                model=model,
                original_model=original_model,
                tier=model_tier_info.get("tier"),
                base_tier=model_tier_info.get("base_tier"),
                hint=tier_hint if tier_hint else None,
                reason=model_tier_info.get("reason"),
                peak=1 if model_tier_info.get("peak") else 0,
                hours_left=model_tier_info.get("hours_left"),
                active_key=chosen,
            )
        elif original_model != model:
            _log_model_decision(
                key_name=chosen,
                model=model,
                original_model=original_model,
                tier="client",
                base_tier="client",
                hint=tier_hint if tier_hint else None,
                reason=f"client X-Model-Tier={tier_hint}",
                peak=0,
                active_key=chosen,
            )

        order = [chosen] + [n for n in KEYS if n != chosen]
        # Never try manually-disabled keys (operator touched
        # ~/.hermes/bot/.key_disabled_<name>). best_key() Phase 4 already steers
        # the *initial* choice away from them via _is_key_healthy; this filter
        # also drops them from the retry fallback list so the loop skips them
        # entirely. If every key is disabled, `order` empties and the request
        # falls through to Ollama Cloud / external failover (correct behaviour).
        order = [n for n in order if not _is_manually_disabled(n)]

        response_buffer = bytearray()
        key_used: str | None = None
        status_code = None
        error_text = None
        try:
            for attempt, name in enumerate(order):
                key_used = name
                key = KEYS[name]
                try:
                    path = self.path
                    # Strip /v1 prefix (OpenAI SDK sends /v1/chat/completions but
                    # the z.ai v4 base URL already contains the API version).
                    if path.startswith("/v1/"):
                        path = path[3:]
                    # Only proxy /chat/completions to z.ai.  Non-chat paths
                    # (model listings, Ollama API probes, version checks) get
                    # a fast local 404 — sending them to z.ai wastes quota
                    # and triggers Hermes fallback retries that burn PPQ.
                    if not path.endswith("/chat/completions"):
                        self.send_response(404)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(b'{"error":"only /chat/completions is proxied"}')
                        return
                    url = UPSTREAM + path
                    hdrs = {k: v for k, v in self.headers.items()
                            if k.lower() not in ("host", "authorization", "connection", "content-length")}
                    hdrs["Authorization"] = f"Bearer {key}"
                    hdrs["Content-Type"] = "application/json"
                    req = urllib.request.Request(url, data=body, method=self.command, headers=hdrs)
                    with urllib.request.urlopen(req, timeout=180) as resp:
                        status_code = resp.status
                        # Buffer full response before sending — allows
                        # empty-response detection for key health tracking.
                        full_body = resp.read()

                        # Check for empty or error response
                        resp_text = full_body.decode('utf-8', errors='ignore').strip()
                        is_empty = (
                            not resp_text
                            or resp_text == "data: [DONE]"
                        )

                        # Parse JSON to check content field
                        is_error_response = False
                        is_truncated = False  # finish_reason=length (ran out of tokens)
                        if not is_empty:
                            try:
                                resp_json = json.loads(resp_text)
                                # Check for error response (quota exhausted, etc.)
                                if "error" in resp_json and "choices" not in resp_json:
                                    is_error_response = True
                                else:
                                    choices = resp_json.get("choices", [])
                                    if choices:
                                        msg_obj = choices[0].get("message", {})
                                        content = msg_obj.get("content", "")
                                        finish_reason = choices[0].get("finish_reason", "")
                                        if finish_reason == "length":
                                            is_truncated = True
                                        if not content or not content.strip():
                                            # Content is empty — check if reasoning
                                            # has value we can use instead
                                            reasoning = msg_obj.get("reasoning_content", "")
                                            if reasoning and reasoning.strip():
                                                # Inject reasoning as content so
                                                # the tokens aren't wasted
                                                msg_obj["content"] = reasoning
                                                full_body = json.dumps(resp_json).encode()
                                                is_empty = False
                                            else:
                                                is_empty = True
                            except Exception:
                                pass

                        if is_error_response:
                            # Error responses are transient (model overload,
                            # internal errors) — NOT quota issues. Only 429
                            # should block a key. Failover this request only.
                            continue

                        if is_empty:
                            # Content AND reasoning both empty — key produced nothing.
                            # Try external failover for THIS request only.
                            # Do NOT mark key as exhausted (it might work next time).
                            if self._try_external_failover(body, model, response_buffer, t0):
                                return
                            continue  # try next key

                        # Non-empty response — send to client
                        _mark_key_healthy(name)
                        self.send_response(resp.status)
                        for h, v in resp.headers.items():
                            if h.lower() not in ("transfer-encoding", "connection"):
                                self.send_header(h, v)
                        if is_truncated:
                            self.send_header("X-Response-Truncated", "true")
                        self.end_headers()
                        response_buffer.extend(full_body)
                        self.wfile.write(full_body)
                        self.wfile.flush()
                        # Success — reset the Kalman consecutive-429 streak.
                        if _rate_limit_predictor is not None:
                            _rate_limit_predictor.record_success()
                        return
                except urllib.error.HTTPError as e:
                    # Classify the failure by HTTP status to arm the correct
                    # circuit-breaker backoff (req 2 — dead-key detection):
                    #   429              → exhausted (exponential 2→60s)
                    #   401/403          → dead key  (flat 1h)
                    #   500/502/503/504  → server err (flat 30s)
                    if e.code == 429:
                        _mark_key_exhausted(name)
                    elif e.code in (401, 403):
                        _mark_key_dead(name)
                    elif e.code in (500, 502, 503, 504):
                        _mark_key_server_error(name)
                    if _is_retryable_error(e):
                        if _attempt_retry(e, attempt, name, t0, order):
                            continue
                    # z.ai failure — try external failover before giving up
                    # Include 429 (rate limit) since z.ai returns 429 when exhausted
                    if e.code in (401, 403, 429) and self._try_external_failover(body, model, response_buffer, t0):
                        return
                    # Non-retryable error
                    status_code = e.code
                    error_text = f"HTTPError {e.code}"
                    body_err = e.read()
                    response_buffer.extend(body_err)
                    self.send_response(e.code)
                    self.end_headers()
                    self.wfile.write(body_err)
                    return
                except Exception as e:
                    if _is_retryable_error(e):
                        if _attempt_retry(e, attempt, name, t0, order):
                            continue
                    # Non-retryable error
                    status_code = 502
                    error_text = f"proxy error: {e}"
                    msg = f"proxy error: {e}".encode()
                    response_buffer.extend(msg)
                    self.send_response(status_code)
                    self.end_headers()
                    self.wfile.write(msg)
                    return

            # Phase 1.2 LIVE ROUTER — retry-loop terminal fallback (P3.4 Fix 1).
            # When BOTH z.ai keys 429-exhaust DURING the retry loop, best_key()'s
            # initial pick already returned a key (its health cache lagged the
            # real 429), so the Phase 5 gate inside best_key() never fired. This
            # is the production path that previously bypassed LiveRouter (841
            # dual-exhaustion events/2h, 0 live events). Consult LiveRouter HERE
            # and route its pick before the hardcoded ollama->external chain.
            # Kill switch + safe fallthrough live inside _consult_live_router;
            # on any failure / no pick we fall through to the chain below.
            _pick, _pick_model, _fb, _fb_model = _consult_live_router()
            if _pick:
                _log_key_decision(
                    chosen_key=_pick,
                    reason=f"live_kalman_failover_{_pick}")
                response_buffer = bytearray()
                if _pick == "ollama_cloud" and OLLAMA_CLOUD_KEY:
                    if self._try_ollama_cloud(body, model, response_buffer, t0):
                        return
                elif _pick in EXTERNAL_PROVIDERS:
                    if self._try_external_failover(body, model,
                                                   response_buffer, t0,
                                                   preferred=_pick):
                        return
                # LiveRouter pick failed (or was a z.ai key) — fall through to
                # the hardcoded chain below (safe fallback, criterion 4).

            # All z.ai keys exhausted — try Ollama Cloud (primary, not failover)
            if not peak and OLLAMA_CLOUD_KEY:
                if self._try_ollama_cloud(body, model, response_buffer, t0):
                    return

            # All primary providers exhausted — try paid failover (PPQ/OpenRouter)
            if self._try_external_failover(body, model, response_buffer, t0):
                return

            # All providers failed
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"all providers exhausted, retry later"}')
            return
        finally:
            usage = _parse_usage(bytes(response_buffer))
            suffix = None
            if key_used and KEYS.get(key_used):
                suffix = KEYS[key_used][-4:]
            # RP-2: extract real cost (flat-rate $0 for ours/friend z.ai keys)
            _zai_tokens = int(usage.get("total_tokens") or 0)
            _zai_cost, _zai_cost_src = _extract_cost(
                key_used, bytes(response_buffer), _zai_tokens)
            _log_api_call(
                key_name=key_used, key_suffix=suffix, model=model,
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                total_tokens=_zai_tokens,
                tier="zai", status_code=status_code, error=error_text,
                duration_ms=int((time.time() - t0) * 1000),
                cost_usd=_zai_cost, cost_source=_zai_cost_src,
            )
            if not getattr(self, '_spend_recorded', False):
                _record_spend(key_used, model, _zai_tokens)

            # ── Provider telemetry (Phase 2.5.1) ─────────────────────────────
            # One row per request: success/fail, latency, token-mismatch.
            # NEVER raises — telemetry failure is silent and must never break
            # request handling.  Wrapped in its own try/except (the function
            # itself also swallows errors, so this is belt-and-suspenders).
            try:
                _latency_ms = int((time.time() - t0) * 1000)
                # Use completion_tokens — NOT total_tokens — for the audit.
                # `actual_tokens` is estimated from len(response_buffer)//4,
                # and the response buffer contains ONLY the completion content
                # (the prompt is never echoed back).  Comparing total_tokens
                # (prompt+completion) against a completion-only estimate always
                # looks like a >20% over-billing gap whenever the prompt is
                # non-trivial, producing false-positive billing-mismatch alerts.
                # (Phase 2.5.4 false-positive fix.)
                _billed = int(usage.get("completion_tokens") or 0)
                _resp_buf = bytes(response_buffer)
                # Classify the upstream response for telemetry. Extracted to
                # _classify_response() (testable; mirrors _parse_usage's SSE
                # handling) so genuine HTTP/network errors are reported with
                # their real error_text instead of a generic 'parse_error'.
                _resp_received, _resp_valid, _err_type = _classify_response(
                    _resp_buf, error_text)
                # Estimate actual tokens + detect billing mismatch (Phase 2.5.4).
                # Uses the unit-tested audit_token_count from src/token_audit.py
                # (with a never-raising fallback stub if the import failed).
                # Token audit NEVER blocks request handling — _audit_token_count
                # swallows all errors internally.
                _actual, _mismatch, _mm_rate = _audit_token_count(_billed, _resp_buf)
                if _mismatch:
                    # Quality signal: feed mismatch_rate into CPVO via the
                    # token_mismatch telemetry column. Warn loudly — a large
                    # billed-vs-actual gap on the COMPLETION content is a
                    # billing-fraud / silent-downgrade signal worth
                    # investigating.  (Note: total_tokens is intentionally NOT
                    # used here — see the comment at the _billed assignment
                    # above — because the response buffer holds completion text
                    # only, so completion_tokens is the correct comparison basis.)
                    print(
                        f"[telemetry] token billing mismatch (completion): "
                        f"provider={key_used or 'unknown'} "
                        f"billed={_billed} actual~={_actual} "
                        f"gap={_mm_rate:.0%}",
                        flush=True,
                    )
                _log_provider_telemetry(
                    conn=_usage_db(),
                    provider=key_used or "unknown",
                    response_received=_resp_received,
                    response_valid=_resp_valid,
                    latency_ms=_latency_ms,
                    error_type=_err_type,
                    billed_tokens=_billed,
                    actual_tokens=_actual,
                    token_mismatch=_mismatch,
                    model=model,
                )
            except Exception:
                pass

            # ── Shadow mode: log optimizer decision alongside live pick ────
            # Read-only comparison. NEVER affects routing. Wrapped so any
            # shadow failure cannot break the proxied request.
            if _shadow_hook is not None:
                try:
                    _shadow_hook.compare(
                        live_provider=key_used,
                        live_model=model,
                        tokens=int(usage.get("total_tokens") or 0),
                        quota_state=_snapshot_quota(),
                        health_state=_snapshot_health(),
                        peak=peak if 'peak' in dir() else False,
                    )
                except Exception:
                    pass

            # ── Phase 2.3: Live consumption tracking ──────────────────────
            # Feed completed request token count to LiveRouter's Kalman
            # filters. Wrapped in try/except — NEVER breaks request handling.
            # record_request updates the ConsumptionKalman for the provider
            # that served this request, keeping burn-rate predictions fresh.
            if _LIVE_ROUTER is not None:
                try:
                    total_tokens = int(usage.get("total_tokens") or 0)
                    _LIVE_ROUTER.record_request(
                        provider=key_used if key_used else "unknown",
                        tokens=total_tokens,
                    )
                except Exception:
                    pass  # recording must never break production

    def do_POST(self): self._proxy()
    def do_PUT(self):  self._proxy()
    def do_GET(self):
        if self.path == "/quota":
            with lock:
                data = {}
                for n, v in quota_cache.items():
                    wins = v[0]
                    lckd, lwin, lpct, lthr = is_key_locked(n, wins)
                    data[n] = {
                        "windows": wins,
                        "locked": lckd,
                        "locked_window": lwin,
                        "locked_pct": lpct,
                        "locked_threshold": lthr,
                        "max_pct": _max_pct(wins),
                        "age_s": int(time.time() - v[1]),
                    }
                data["active"] = _best_unlocked()[0]
                data["proactive_cooldown"] = {
                    "switched_to": _proactive_switch_state["key"],
                    "active": time.time() < _proactive_switch_state["until"],
                    "expires_in_s": max(0, int(_proactive_switch_state["until"] - time.time())),
                }
            # Predictions: cache-ONLY (never triggers a fetch → no self-HTTP
            # recursion deadlock).  The background _refresh_loop keeps these warm.
            for n in KEYS:
                if n in data:
                    data[n]["predictions"] = _get_cached_predictions(n)
            # Ollama Cloud quota from tracker (EUv2-5)
            data["ollama_cloud"] = _snapshot_quota().get("ollama_cloud", {})
            payload = json.dumps(data, indent=2).encode()
            self.close_connection = True   # honor the Connection: close header below
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        elif self.path == "/health":
            self.close_connection = True   # honor the Connection: close header below
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"ok")
        elif self.path == "/tier":
            # Current recommended model tier (for dispatch gate queries)
            # Supports ?urgency=urgent|standard|background query parameter
            self.close_connection = True
            try:
                from urllib.parse import urlparse, parse_qs
                qs = parse_qs(urlparse(self.path).query)
                urgency = qs.get("urgency", ["standard"])[0]
                chosen = best_key()
                if _select_model_tier is not None:
                    info = _select_model_tier(chosen, None, urgency)
                else:
                    info = {"tier": "unknown", "model": "glm-5.2",
                            "reason": "model_tier_router unavailable"}
                info["active_key"] = chosen
                info["quota_pct"] = {n: _max_pct(v[0]) for n, v in quota_cache.items()}
            except Exception as e:
                info = {"tier": "error", "reason": str(e)}
            payload = json.dumps(info, indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        elif self.path.startswith("/route"):
            # Full routing decision endpoint (Kalman + costs + difficulty)
            self.close_connection = True
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            tokens = int(qs.get("tokens", ["0"])[0])
            difficulty = qs.get("difficulty", ["medium"])[0]
            try:
                sys.path.insert(0, os.path.dirname(__file__))
                from burn_predictor import route_request
                decision = route_request(estimated_tokens=tokens, difficulty=difficulty)
            except Exception as e:
                decision = {"error": str(e)}
            payload = json.dumps(decision, indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        elif self.path.startswith("/v1/dispatch_gate"):
            # Dispatch gate — should this job run now?
            # Three-dimension Kalman-gated decision (no SQLite reads):
            #   D1 hardware availability (binary) → D2 quota sufficiency
            #   (hardware-scaled safety margin + flash downgrade) → D3 price
            #   (scarcity override when hardware present).  See
            #   IMPL-SPEC-kalman-dispatch-gate.md (v2) + src/dispatch_gate.py.
            self.close_connection = True
            from urllib.parse import urlparse, parse_qs
            from datetime import datetime, timezone
            try:
                qs = parse_qs(urlparse(self.path).query)
                estimated_tokens = int(qs.get("estimated_tokens", ["0"])[0])
                task_type = qs.get("task_type", ["coding"])[0]
                urgency = qs.get("urgency", ["standard"])[0]
                hardware_req = qs.get("hardware_req", ["none"])[0]
                task_subtype = qs.get("task_subtype", [None])[0]

                peak = _is_peak_hour()
                peak_mult = 3.0 if peak else 1.0
                quota_snap = _snapshot_quota()
                health_snap = _snapshot_health()

                # 1. Gather primary candidates (ours/friend) for BOTH the gate
                #    and the legacy recommended_provider / downgrade_chain fields.
                candidates = []
                gate_quota = {}
                for key in ("ours", "friend"):
                    if not health_snap.get(key):
                        gate_quota[key] = {"used_pct": 100.0, "remaining": 0.0, "healthy": False}
                        continue
                    q = quota_snap.get(key, {})
                    remaining = q.get("remaining", 0)
                    gate_quota[key] = {
                        "used_pct": q.get("used_pct", 0.0),
                        "remaining": remaining,
                        "healthy": True,
                    }
                    if remaining >= estimated_tokens:
                        base = (_converged_rates or {}).get(key)
                        if base is None:
                            base = _rpt_rate(key)
                        eff = base * _KEY_COST_MULTIPLIER.get(key, 1.0) * peak_mult
                        candidates.append({
                            "provider": key,
                            "price_per_m": eff,
                            "remaining": remaining,
                            "used_pct": q.get("used_pct", 0.0),
                        })
                # Ollama Cloud — flat rate, BYPASSES the quota margin gate (no
                # exhaustion risk).  Tracked separately so it can act as a
                # fallback when the primary-key gate holds.
                flat_candidates = []
                if health_snap.get("ollama_cloud") and OLLAMA_CLOUD_KEY:
                    base = (_converged_rates or {}).get("ollama_cloud")
                    if base is None:
                        base = _rpt_rate("ollama_cloud")
                    eff = base * _KEY_COST_MULTIPLIER.get("ollama_cloud", 1.0) * peak_mult
                    flat_candidates.append({
                        "provider": "ollama_cloud",
                        "price_per_m": eff,
                        "remaining": quota_snap.get("ollama_cloud", {}).get("remaining", 999999999),
                        "used_pct": 0.0,
                    })
                candidates.sort(key=lambda c: c["price_per_m"])
                flat_candidates.sort(key=lambda c: c["price_per_m"])
                all_candidates = candidates + flat_candidates

                # 2. Cached burn-rate predictions (cache-only → never fetches).
                burn_rate = {}
                hours_until = {}
                for key in ("ours", "friend"):
                    preds = _get_cached_predictions(key)
                    exhaust = _will_exhaust(preds)
                    burn_rate[key] = (exhaust or {}).get("burn_rate_pct_per_hour", 0.0) or 0.0
                    hours_until[key] = (exhaust or {}).get("exhausts_in_hours", 999)

                # 3. Hardware probe (Dimension 1) — only when hardware_req != none.
                hw_state = _probe_hardware(hardware_req)

                # 4. Run the three-dimension gate (src/dispatch_gate.py).
                if _evaluate_dispatch is not None:
                    gate = _evaluate_dispatch(
                        estimated_tokens=estimated_tokens,
                        task_type=task_type,
                        hardware_req=hardware_req,
                        task_subtype=task_subtype,
                        quota=gate_quota,
                        burn_rate_pct_per_hour=burn_rate,
                        converged_rates=(_converged_rates or {"ours": 0.001, "friend": 0.001}),
                        is_peak=peak,
                        peak_mult=peak_mult,
                        hardware_state=hw_state,
                    )
                else:
                    # Module unavailable — coarse decision, but the HARDWARE
                    # GATE (D1) must stay FAIL-CLOSED.  Without the real
                    # module we cannot safely confirm a board/DQ05, so default
                    # to *unavailable* unless the probed hw_state actually
                    # confirms presence+free.  This mirrors
                    # src/dispatch_gate._hardware_available so a board-required
                    # task can never dispatch on ollama_cloud (flat-rate path
                    # below also checks gate["hardware"]["available"]).
                    _hws = hw_state or {}
                    _lock_free = _hws.get("lock_status") == "free"
                    _hw_avail = (
                        hardware_req == "none"
                        or (hardware_req == "board"
                            and _hws.get("board_present") and _lock_free)
                        or (hardware_req == "dual_board"
                            and _hws.get("board_count", 0) >= 2 and _lock_free)
                        or (hardware_req == "dq05"
                            and _hws.get("dq05_reachable"))
                    )
                    gate = {
                        "can_dispatch": bool(candidates) and _hw_avail,
                        "reason": "dispatch_gate module unavailable; coarse check",
                        "recommended_model": (candidates[0] and "glm-5.2") if candidates else None,
                        "effective_price_per_m": round(candidates[0]["price_per_m"], 6) if candidates else None,
                        "predicted_cost": None,
                        "hours_until_exhaustion": {k: hours_until[k] for k in ("ours", "friend")},
                        "quota_used_pct": {k: round(gate_quota[k]["used_pct"], 1) for k in ("ours", "friend")},
                        "burn_rate_pct_per_hour": {k: round(burn_rate[k], 1) for k in ("ours", "friend")},
                        "is_peak_hour": peak, "peak_multiplier": peak_mult,
                        "scarcity_factor": 1.0, "downgraded": False,
                        "scarcity_override": False,
                        "hardware": {"required": hardware_req, "available": _hw_avail},
                        "task_budget": estimated_tokens, "safety_margin": 2.0,
                    }

                # 5. Flat-rate fallback: gate held on QUOTA (primary keys tight)
                #    but a flat-rate provider is available → dispatch anyway (no
                #    quota risk).  Does NOT apply to hardware holds — a task that
                #    needs a board/DQ05 cannot run on a flat-rate LLM provider.
                recommended_provider = all_candidates[0]["provider"] if all_candidates else None
                hw_avail = gate.get("hardware", {}).get("available", True)
                if not gate["can_dispatch"] and flat_candidates and hw_avail:
                    fc = flat_candidates[0]
                    gate["can_dispatch"] = True
                    gate["downgraded"] = True
                    gate["recommended_model"] = "llama3.3-70b"
                    gate["reason"] = ("primary keys tight (gate hold); dispatching "
                                      "on flat-rate " + fc["provider"])
                    gate["effective_price_per_m"] = round(fc["price_per_m"], 6)
                    recommended_provider = fc["provider"]

                # 6. Legacy fields (kept for backward compatibility — ADDITIVE).
                # Urgency-tier model selection incl. the new spec task types.
                TASK_MODELS = {
                    "coding":     {"high": "glm-5.2",        "standard": "glm-4.5-air",   "low": "glm-4.5-flash"},
                    "reasoning":  {"high": "glm-4.5",        "standard": "glm-4.5-air",   "low": "glm-4.5-flash"},
                    "chat":       {"high": "glm-4.5-air",    "standard": "glm-4.5-air",   "low": "glm-4.5-flash"},
                    "simple":     {"high": "glm-4.5-flash",  "standard": "glm-4.5-flash", "low": "glm-4.5-flash"},
                    "mechanical": {"high": "glm-4.5-flash",  "standard": "glm-4.5-flash", "low": "glm-4.5-flash"},
                    "research":   {"high": "glm-5.2",        "standard": "glm-5.2",        "low": "glm-4.5-flash"},
                    "review":     {"high": "glm-5.2",        "standard": "glm-4.5-air",   "low": "glm-4.5-flash"},
                    "docs":       {"high": "glm-4.5-flash",  "standard": "glm-4.5-flash", "low": "glm-4.5-flash"},
                }
                tt = task_type if task_type in TASK_MODELS else "coding"
                chain = []
                for tier in ("high", "standard", "low"):
                    m = TASK_MODELS[tt][tier]
                    viable = bool(all_candidates)
                    provider = all_candidates[0]["provider"] if all_candidates else "none"
                    chain.append({"model": m, "tier": tier, "provider": provider, "viable": viable})

                # Defer suggestion (suppressed when scarcity override is active).
                defer = None
                if peak and urgency == "background" and not gate.get("scarcity_override"):
                    defer = {"reason": "peak_hours_3x_cost", "wait_until_utc_hour": 11, "savings_factor": 3.0}

                # Quota state snapshot (thread-safe).
                quota_state = {}
                with lock:
                    for key in ("ours", "friend"):
                        wins = quota_cache.get(key, ([], 0.0))[0]
                        pct = _max_pct(wins)
                        lckd, _lwin, _lpct, _lthr = is_key_locked(key, wins)
                        quota_state[key] = {
                            "used_pct": pct,
                            "remaining_tokens": int(max(0.0, 2_000_000 * (1.0 - pct / 100.0))),
                            "locked": lckd,
                        }

                # Peak timing.
                now_utc = datetime.now(timezone.utc)
                peak_ends_in = max(0, 11 - now_utc.hour) if peak else None

                # 7. Build response — gate fields (authoritative) + legacy fields.
                est_cost = None
                if gate.get("effective_price_per_m") is not None:
                    est_cost = round(gate["effective_price_per_m"] * estimated_tokens / 1e6, 6)
                info = dict(gate)
                info.update({
                    "recommended_provider": recommended_provider,
                    "estimated_cost_usd": est_cost,
                    "hours_until_exhaust": hours_until,
                    "peak_active": peak,
                    "peak_ends_in_hours": peak_ends_in,
                    "defer_suggestion": defer,
                    "downgrade_chain": chain,
                    "quota_state": quota_state,
                    "timestamp": int(time.time()),
                })
            except Exception as e:
                info = {"can_dispatch": False, "reason": "error: " + str(e)}
            payload = json.dumps(info, indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        elif self.path == "/v1/models" or self.path == "/models":
            # Model listing — return stub so Hermes doesn't 404 → fall back to PPQ
            self.close_connection = True
            now = int(time.time())
            models_data = {
                "object": "list",
                "data": [
                    {"id": "glm-5.2", "object": "model", "created": now, "owned_by": "zai"},
                    {"id": "glm-4.5-flash", "object": "model", "created": now, "owned_by": "zai"},
                    {"id": "glm-4.5-air", "object": "model", "created": now, "owned_by": "zai"},
                    {"id": "kimi-k2.7-code", "object": "model", "created": now, "owned_by": "ollama"},
                    {"id": "kimi-k3:cloud", "object": "model", "created": now, "owned_by": "ollama"},
                ]
            }
            payload = json.dumps(models_data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        elif self.path == "/spend":
            # Daily spend tracker — shows current spend vs caps
            self.close_connection = True
            try:
                today = _date.today().isoformat()
                rows = _usage_db().execute(
                    "SELECT tier, spend_usd, call_count, token_count "
                    "FROM daily_spend WHERE date=?", (today,)).fetchall()
                data = {
                    "date": today,
                    "caps": {"manager": _SPEND_CAP_MANAGER, "worker": _SPEND_CAP_WORKER},
                    "tiers": {},
                }
                for tier, spend, calls, tokens in rows:
                    cap = _SPEND_CAP_MANAGER if tier == "manager" else _SPEND_CAP_WORKER
                    data["tiers"][tier] = {
                        "spend_usd": round(spend, 4),
                        "cap_usd": cap,
                        "pct_of_cap": round(spend / cap * 100, 1) if cap > 0 else 0,
                        "call_count": calls,
                        "token_count": tokens,
                    }
            except Exception as e:
                data = {"error": str(e)}
            payload = json.dumps(data, indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        else:
            self._proxy()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    t = threading.Thread(target=_refresh_loop, daemon=True)
    t.start()
    time.sleep(3)  # let first quota fetch complete
    print(f"zai_proxy on :{PORT}  quotas={ {n: _max_pct(v[0]) for n, v in quota_cache.items()} }")
    # Allow socket reuse to prevent "Address already in use" on restart
    from socketserver import TCPServer
    TCPServer.allow_reuse_address = True
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
