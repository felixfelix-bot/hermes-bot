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
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

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
UPSTREAM   = "https://api.z.ai/api/coding/paas/v4"
QUOTA_URL  = "https://api.z.ai/api/monitor/usage/quota/limit"
CACHE_TTL  = 300                                # 5 min
PORT       = 9099
STATE_FILE = Path.home() / ".hermes" / "bot" / "zai_proxy_state.json"

# ── usage logging DB (separate from response_cache.db) ──────────────────────
USAGE_DB = Path.home() / ".hermes" / "bot" / "zai_usage.db"
_usage_db_conn: sqlite3.Connection | None = None
_usage_db_lock = threading.Lock()

quota_cache: dict[str, tuple[list[dict], float]] = {}   # name → (windows, ts)
lock = threading.Lock()

# ── proactive burn-rate prediction (Phase 3) ─────────────────────────────────
# Import the burn predictor.  Wrapped so a broken burn_predictor.py never crashes
# the proxy — if the import fails, proactive switching is silently disabled and
# the proxy falls back to reactive (lock-based) key selection.
_predict_exhaustion = None
try:
    sys.path.insert(0, os.path.dirname(__file__))
    from burn_predictor import predict_exhaustion as _predict_exhaustion
except Exception:
    pass

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
            duration_ms INTEGER
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
                  status_code=None, error=None, duration_ms=None):
    """Log one API call event. Swallows all errors — logging must never break a request."""
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

    # neither locked → always prefer our key. We own it; friend's key is a
    # courtesy fallback used only when our key is locked (weekly >= 80%).
    return ("ours", f"prefer_ours_both_unlocked_ours_{op}_friend_{fp}", op, fp, 1, 1)


def best_key() -> str:
    """Pick a key for this request and log the decision.  Decides under the quota
    lock; logs the decision outside the lock (DB write never blocks quota reads).

    Phase 3 — proactive burn-rate prediction: after the normal lock-based
    selection, check whether the chosen key is *predicted* to exhaust before its
    window resets.  If so, and the other key is unlocked with headroom, switch
    proactively — before a 429 ever happens.  A 30-minute cooldown prevents
    oscillation.  The entire proactive block runs OUTSIDE the quota lock (the
    predictor does a self-HTTP GET to /quota which needs ``lock``) and is wrapped
    so a predictor failure can never break a proxied request.
    """
    with lock:
        chosen, reason, op, fp, oa, fa = _best_unlocked()

    # PROACTIVE burn prediction (outside the quota lock — never deadlocks) -------
    if _can_proactive_switch():
        try:
            exhaust_pred = _will_exhaust(_get_predictions(chosen))
            if exhaust_pred is not None:
                other_key = "friend" if chosen == "ours" else "ours"
                if other_key in KEYS:                     # only one key? skip
                    # Don't proactively switch to a key the lock logic already
                    # rejected as locked — stale read of quota_cache is fine here.
                    other_windows = quota_cache.get(other_key, ([], 0.0))[0]
                    other_locked, *_ = is_key_locked(other_key, other_windows)
                    if not other_locked:
                        # Only switch if the other key is NOT also predicted to
                        # exhaust (insufficient-data entries are treated as safe).
                        if _will_exhaust(_get_predictions(other_key)) is None:
                            chosen = other_key
                            reason = (f"proactive_switch_predicted_exhaustion_"
                                      f"{exhaust_pred.get('window', '?')}")
                            _proactive_switch_state["key"] = chosen
                            _proactive_switch_state["until"] = (time.time()
                                + _PROACTIVE_COOLDOWN_SECONDS)
        except Exception:
            pass      # predictor must NEVER break key selection

    # PROACTIVE RECOVER check — if the locked key has recovered below threshold
    # (window reset), prefer it without waiting for next 5-min refresh.
    # This runs outside the proactive cooldown so a recovered key is picked up
    # immediately when its window rolls over.
    try:
        # Determine which key was locked (not chosen)
        locked_key = "friend" if chosen == "ours" else "ours"
        locked_windows = quota_cache.get(locked_key, ([], 0.0))[0]
        locked_now, *_ = is_key_locked(locked_key, locked_windows)
        if not locked_now:
            # Key that was locked has recovered — re-evaluate
            with lock:
                chosen2, reason2, op2, fp2, oa2, fa2 = _best_unlocked()
            if chosen2 != chosen:
                chosen = chosen2
                reason = f"proactive_recover_{locked_key}_unlocked"
                # Reset cooldown so the next proactive prediction can fire
                _proactive_switch_state["key"] = None
                _proactive_switch_state["until"] = 0.0
    except Exception:
        pass      # NEVER break key selection

    _log_key_decision(chosen_key=chosen, reason=reason, ours_pct=op,
                      friend_pct=fp, ours_available=oa, friend_available=fa)
    return chosen


# ── proxy handler ───────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

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
        model = _extract_model(body)

        chosen = best_key()                        # logs the key decision
        order = [chosen] + [n for n in KEYS if n != chosen]

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
                    url = UPSTREAM + path
                    hdrs = {k: v for k, v in self.headers.items()
                            if k.lower() not in ("host", "authorization", "connection", "content-length")}
                    hdrs["Authorization"] = f"Bearer {key}"
                    hdrs["Content-Type"] = "application/json"
                    req = urllib.request.Request(url, data=body, method=self.command, headers=hdrs)
                    with urllib.request.urlopen(req, timeout=180) as resp:
                        status_code = resp.status
                        self.send_response(resp.status)
                        for h, v in resp.headers.items():
                            if h.lower() not in ("transfer-encoding", "connection"):
                                self.send_header(h, v)
                        self.end_headers()
                        # streaming passthrough (SSE-safe) + buffer for usage parsing
                        while True:
                            chunk = resp.read(4096)
                            if not chunk:
                                break
                            response_buffer.extend(chunk)
                            self.wfile.write(chunk)
                            self.wfile.flush()
                        # Success — reset the Kalman consecutive-429 streak.
                        if _rate_limit_predictor is not None:
                            _rate_limit_predictor.record_success()
                        return
                except urllib.error.HTTPError as e:
                    if e.code == 429:
                        # Tried all keys; enter unlimited retry gated by the
                        # Kalman-predicted recovery time.  Falls back to capped
                        # exponential backoff when the filter has too few samples.
                        if attempt == len(order) - 1:
                            _log_rate_limit(key_used=name, attempt=attempt, duration_ms=int((time.time() - t0) * 1000))
                            retry_num = attempt - len(order) + 1  # 0-indexed per full cycle
                            if retry_num >= 50:
                                pass          # safety cap exhausted — fall through to return 429
                            elif _rate_limit_predictor is not None:
                                _rate_limit_predictor.record_429()
                                wait = _rate_limit_predictor.predict_retry_at()
                                time.sleep(wait)
                                continue      # loop back through all keys
                            else:
                                # Predictor unavailable — old exponential backoff.
                                import random
                                wait = min(2 ** (retry_num + 1), 30) * (0.75 + random.random() * 0.5)
                                time.sleep(wait)
                                continue
                        else:
                            continue   # try the other key
                    status_code = e.code
                    error_text = f"HTTPError {e.code}"
                    body_err = e.read()
                    response_buffer.extend(body_err)
                    self.send_response(e.code)
                    self.end_headers()
                    self.wfile.write(body_err)
                    return
                except Exception as e:
                    status_code = 502
                    error_text = f"proxy error: {e}"
                    msg = f"proxy error: {e}".encode()
                    response_buffer.extend(msg)
                    self.send_response(502)
                    self.end_headers()
                    self.wfile.write(msg)
                    return
        finally:
            usage = _parse_usage(bytes(response_buffer))
            suffix = None
            if key_used and KEYS.get(key_used):
                suffix = KEYS[key_used][-4:]
            _log_api_call(
                key_name=key_used, key_suffix=suffix, model=model,
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                total_tokens=int(usage.get("total_tokens") or 0),
                tier="zai", status_code=status_code, error=error_text,
                duration_ms=int((time.time() - t0) * 1000),
            )

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
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self._proxy()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    t = threading.Thread(target=_refresh_loop, daemon=True)
    t.start()
    time.sleep(3)  # let first quota fetch complete
    print(f"zai_proxy on :{PORT}  quotas={ {n: _max_pct(v[0]) for n, v in quota_cache.items()} }")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
