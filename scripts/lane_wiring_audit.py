#!/usr/bin/env python3
"""lane_wiring_audit.py — Detect routing lanes that are unusable despite value.

Runs hourly (no_agent cron). Finds the "endpoint could add value but can't serve"
class of defect — the one that took ollama offline for ~24h on 2026-09-08/09:

  * `_OLLAMA_CLOUD_KEYS` emptied by the P0-1 edit (flags rm'd but list never
    restored) → flat router listed ollama first in every chain, but the
    dispatcher iterated an empty list → 54 dispatch_fails / 30s backoff while
    96% free quota sat idle and ~$5/h flowed to PAYGO.

Design principles:
  * Read-only against live state: /quota, key_health, api_calls, flag files.
  * Live probes (rate-limited 1/lane/6h) DISAMBIGUATE "provider down" from
    "our wiring broken": a key that probes 200 but the router keeps
    dispatch-failing is a wiring/config gap, not a provider outage.
  * Findings are SILENT to the user (anomaly rows written alerted=1, so
    anomaly-notify skips them) and instead AUTO-SCHEDULE a SOON-priority fix
    task on the llm-routing board (worker-routing, QGATES body). Transition
    dedup: a new task only on ok→broken; recurrence comments on the open task.
  * Legitimate exhaustion (oc3 monthly 100%, oc2 session 100%, opencode_go
    until reset) is EXPECTED state — never a finding.

Findings:
  LANE_WIRING_GAP        — probe succeeds but router dispatch-fails / PAYGO wins
                           while a quota lane is healthy+headroom.
  QUOTA_MODEL_DRIFT      — proxy /quota reports headroom but probe says exhausted
                           (or vice-versa).
  SUSTAINED_DISPATCH_FAIL— dispatch_fail streak + headroom + no operator flag
                           + probe succeeds (signature of config gap).
  COST_LEAK              — 1h PAYGO spend while a wired-healthy quota lane with
                           headroom carried ~zero traffic.

Exit codes: 0 = clean (silent), 1 = findings (still silent; tasks created).
"""

from __future__ import annotations
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BOT_DIR = Path.home() / ".hermes" / "bot"
DB_PATH = BOT_DIR / "zai_usage.db"
STATE_PATH = BOT_DIR / "lane_audit_state.json"
PROXY_URL = "http://localhost:9099"

_DRY_RUN = False

# Backoff: when consecutive runs find everything stable, double the interval
# between REAL audit passes until it caps at 24h. Reset to 1h on any novelty
# (a finding, an unresolved open finding, or a state-signature change). The
# cron still fires hourly; the script self-throttles via next_run_at in state.
BACKOFF_BASE_S = 3600                 # 1h base interval
BACKOFF_CAP_S = 24 * 3600             # 24h max
BACKOFF_STEPS = int(BACKOFF_CAP_S / BACKOFF_BASE_S).bit_length()  # ~5 steps

PROBE_INTERVAL_S = 6 * 3600          # 1 probe per lane per 6h
OLLAMA_BASE = "https://ollama.com/v1"
OPENCODE_BASE = "https://opencode.ai/zen/go/v1"

# Lanes whose marginal cost is ~$0 (subscription/quota/included) — the ones whose
# idle capacity represents avoidable PAYGO spend.
QUOTA_LANES = {
    "ours", "friend",
    "ollama_cloud", "ollama_cloud_2", "ollama_cloud_3", "ollama_cloud_4",
    "opencode_go",
}
# Per-token / balance lanes — the "PAYGO" reference cost.
PAYGO_LANES = {
    "deepseek", "chutes", "openrouter", "ppq", "neuralwatt", "deepinfra",
    "telnyx", "routstr", "routstrd",
}

# .env variable names → probe base URL (None = z.ai quota GET, no chat probe).
PROBE_KEYS = {
    "ours":          ("ZAI_OUR_KEY", None),
    "friend":        ("ZAI_API_KEY", None),
    "ollama_cloud":  ("OLLAMA_CLOUD_API_KEY", OLLAMA_BASE),
    "ollama_cloud_2": ("OLLAMA_CLOUD_API_KEY_2", OLLAMA_BASE),
    "ollama_cloud_3": ("OLLAMA_CLOUD_API_KEY_3_STOIC_HERSCHEL_499", OLLAMA_BASE),
    "ollama_cloud_4": ("OLLAMA_CLOUD_API_KEY_4_SLEEPY_EASLEY_477", OLLAMA_BASE),
    "opencode_go":   ("OPENCODE_GO_API_KEY", OPENCODE_BASE),
}

# Finding thresholds
DISPATCH_FAIL_STREAK = 5        # consecutive-ish failures before we care
HEADROOM_MIN_PCT = 50.0         # below this, the lane is too close to full to blame
COST_LEAK_MIN_USD = 0.50        # 1h PAYGO spend below this = not worth a task
COST_LEAK_MIN_TOK = 1_000_000   # 1h PAYGO tokens below this = not worth a task




def _load_env() -> dict[str, str]:
    """Load dotenv values from the manager/bot env files (name → value)."""
    vals: dict[str, str] = {}
    for ep in (Path.home() / ".hermes/profiles/manager/.env",
               Path.home() / ".hermes/.env",
               BOT_DIR / ".env"):
        if not ep.is_file():
            continue
        for line in ep.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            line = line.removeprefix("export ").strip()
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip()
            v = v.split(" #", 1)[0].strip()
            if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
                v = v[1:-1]
            if k and k not in vals and v:
                vals[k] = v
    return vals


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(errors="ignore"))
    except Exception:
        return {}


def _save_state(st: dict) -> None:
    if _DRY_RUN:
        return  # dry-run is read-only: never persist probes or findings
    try:
        STATE_PATH.write_text(json.dumps(st, indent=2))
    except Exception:
        pass


def _db_conn() -> sqlite3.Connection:
    return sqlite3.connect(str(DB_PATH), timeout=5)


def fetch_quota() -> dict:
    try:
        with urllib.request.urlopen(PROXY_URL + "/quota", timeout=8) as r:
            return json.loads(r.read().decode(errors="ignore"))
    except Exception:
        return {}


def read_key_health() -> dict[str, dict]:
    out: dict[str, dict] = {}
    try:
        c = _db_conn()
        for row in c.execute(
                "SELECT key_name, healthy, failure_count, last_error_type, "
                "backoff_seconds, disabled_manually, backoff_until FROM key_health"):
            out[row[0]] = {
                "healthy": row[1], "failure_count": row[2],
                "last_error_type": row[3], "backoff_seconds": row[4],
                "disabled_manually": row[5],
                # epoch seconds the lane stays benched until (0/None = not benched).
                # The routing gate (zai_proxy._is_key_healthy) honours retry_after
                # = backoff_until; an EXPIRED backoff is a historical mirror note,
                # NOT an active bench — see the 2026-09-09 oc2 false-positive.
                "backoff_until": row[6],
            }
        c.close()
    except Exception:
        pass
    return out


def read_1h_spend() -> dict[str, dict]:
    out: dict[str, dict] = {}
    try:
        c = _db_conn()
        for row in c.execute(
                "SELECT key_name, SUM(total_tokens), SUM(cost_usd), COUNT(*) "
                "FROM api_calls WHERE ts > ? GROUP BY key_name",
                (time.time() - 3600,)):
            out[row[0] or ""] = {"tokens": row[1] or 0, "cost": row[2] or 0.0,
                                 "calls": row[3] or 0}
        c.close()
    except Exception:
        pass
    return out


def disabled_flags() -> set[str]:
    out: set[str] = set()
    for name in QUOTA_LANES | PAYGO_LANES:
        if (BOT_DIR / f".key_disabled_{name}").exists():
            out.add(name)
    return out


def _probe_chat(base: str, key: str, model: str = "glm-5.2") -> tuple[int, str]:
    """Tiny 1-token chat probe. Returns (http_code, body_prefix)."""
    url = base.rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read(200).decode(errors="ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read(200).decode(errors="ignore")
    except Exception:
        return 0, ""


def _probe_zai(key: str) -> tuple[int, str]:
    """GET z.ai quota endpoint (no token cost)."""
    url = "https://api.z.ai/api/monitor/usage/quota/limit"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read(200).decode(errors="ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read(200).decode(errors="ignore")
    except Exception:
        return 0, ""


def probe_lane(lane: str, env: dict[str, str], state: dict) -> int | None:
    """Rate-limited probe of one lane. Returns http code or None if skipped."""
    if lane not in PROBE_KEYS:
        return None
    probes = state.setdefault("probes", {})
    last = probes.get(lane, {})
    if time.time() - last.get("ts", 0) < PROBE_INTERVAL_S and "code" in last:
        return last["code"]
    env_var, base = PROBE_KEYS[lane]
    key = env.get(env_var, "")
    if not key:
        return None
    if base is None:  # z.ai quota GET
        code, body = _probe_zai(key)
    else:
        code, body = _probe_chat(base, key)
    last = {"ts": time.time(), "code": code, "body": body[:200]}
    probes[lane] = last
    state["probes"] = probes
    _save_state(state)
    return code


def probe_end_to_end(model: str = "glm-5.2", state: dict | None = None) -> str | None:
    """1-token request through the proxy; return X-Provider header value."""
    if state is not None:
        e2e = state.setdefault("e2e", {})
        if time.time() - e2e.get("ts", 0) < PROBE_INTERVAL_S and "prov" in e2e:
            return e2e["prov"]
    url = PROXY_URL + "/v1/chat/completions"
    body = json.dumps({"model": model,
                       "messages": [{"role": "user", "content": "hi"}],
                       "max_tokens": 1}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    prov = None
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            prov = r.headers.get("X-Provider")
    except urllib.error.HTTPError as e:
        # headers can be None on synthesized/headerless HTTPErrors — never
        # let the error handler itself raise.
        prov = (e.headers or {}).get("X-Provider")
    except Exception:
        pass
    if state is not None:
        state["e2e"] = {"ts": time.time(), "prov": prov}
        _save_state(state)
    return prov


def _quota_has_headroom(quota: dict, lane: str) -> bool:
    """True if /quota says the lane has meaningful remaining headroom.

    Returns False when the lane is absent from /quota (unknown state) — we must
    not blame a lane for leaking traffic nor flag drift on data we don't have.
    """
    if lane not in quota:
        return False
    q = quota.get(lane, {})
    if not isinstance(q, dict):  # e.g. /quota "active": "friend" — non-lane entries
        return False
    if lane in ("ours", "friend"):
        # z.ai: windows carry used_pct; headroom if max used < 60%.
        mx = max((w.get("used_pct", 0) for w in q.get("windows", [])), default=100)
        return mx < 60.0
    if lane == "opencode_go":
        return q.get("regime") == "included" and q.get("probe_exhausted") is not True
    # ollama lanes: server truth via probe_exhausted/server_used_pct
    if q.get("regime") == "exhausted":
        return False
    if q.get("probe_exhausted"):
        return False
    sp = q.get("server_used_pct")
    if sp is not None and sp >= 90.0:
        return False
    return True


def _emit_finding(category: str, lane: str, title: str, detail: str,
                  state: dict, dry_run: bool = False) -> None:
    """Silent anomaly row + auto-schedule a SOON fix task (transition dedup)."""
    findings = state.setdefault("findings", {})
    key = f"{category}:{lane}"
    prev = findings.get(key, {})
    now = time.time()
    if prev.get("status") == "open":
        # Recurrence — comment on the open task (best-effort), don't duplicate.
        tid = prev.get("task_id")
        if tid and not dry_run:
            try:
                subprocess.run(
                    ["hermes", "kanban", "--board", "llm-routing", "comment",
                     tid, f"[lane-wiring-audit] still broken: {detail}"],
                    env=_hermes_env(), timeout=60, capture_output=True)
            except Exception:
                pass
        return
    # Write silent anomaly (alerted=1 so anomaly-notify skips it)
    if not dry_run:
        try:
            c = _db_conn()
            c.execute(
                "INSERT INTO anomaly_events (ts, severity, category, title, "
                "detail, alerted, resolved) VALUES (?,?,?,?,?,1,0)",
                (now, "WARN", category, title, json.dumps({"key_name": lane,
                                                           "detail": detail})))
            c.commit()
            c.close()
        except Exception:
            pass
        tid = _create_fix_task(category, lane, title, detail)
    else:
        tid = None
    findings[key] = {"status": "open", "task_id": tid, "first_seen": now}
    state["findings"] = findings
    if not dry_run:
        _save_state(state)
    print(f"[lane-wiring-audit] {category} {lane}: {detail}")


def _resolve_finding(category: str, lane: str, state: dict,
                     dry_run: bool = False) -> None:
    """Mark a finding resolved (broken→ok) and tell the open fix task.

    Comments on the open kanban task so the worker/manager sees the condition
    cleared (verify + close) — closing the loop end-to-end: broken → task →
    fixed → resolved. Silent (no user surface).

    Dry-run is fully inert: NO kanban comment, NO state persist (t_efc68b73
    incident 2026-09-09 — the unguarded subprocess posted ~40 duplicate
    "condition cleared" comments from inside pytest runs, because bare
    audit(dry_run=True) reloads the real state file where the finding was
    still open while _save_state no-ops).
    """
    findings = state.setdefault("findings", {})
    key = f"{category}:{lane}"
    rec = findings.get(key, {})
    if rec.get("status") != "open":
        return
    findings[key]["status"] = "resolved"
    findings[key]["resolved_at"] = time.time()
    state["findings"] = findings
    if not dry_run:
        _save_state(state)
    tid = rec.get("task_id")
    if tid and not dry_run:
        try:
            subprocess.run(
                ["hermes", "kanban", "--board", "llm-routing", "comment",
                 tid, f"[lane-wiring-audit] condition cleared {time.strftime('%Y-%m-%d %H:%M')} "
                      f"— verify + close (auto-resolution)"],
                env=_hermes_env(), timeout=60, capture_output=True)
        except Exception:
            pass
    print(f"[lane-wiring-audit] {category} {lane}: resolved")


def _state_signature(quota: dict, health: dict) -> str:
    """Cheap, stable hash of the routing-relevant state.

    Novelty-reset: any change here (failure count, error type, health flag,
    headroom, e2e provider) resets the backoff to 1h — incidents of the
    "lane broken" class always move at least one of these before any threshold
    trips. A 24h backoff therefore can never sit across a developing outage.
    """
    sig: dict = {}
    for lane in sorted(QUOTA_LANES | PAYGO_LANES):
        h = health.get(lane, {})
        headroom = _quota_has_headroom(quota, lane)
        sig[lane] = [
            h.get("healthy", 0),
            h.get("failure_count", 0),
            h.get("last_error_type"),
            h.get("backoff_seconds", 0),
            int(bool(headroom)),
        ]
    return json.dumps(sig, sort_keys=True)


def _backoff_interval(streak: int) -> int:
    """Interval in seconds for the given clean-streak (binary exp, 24h cap)."""
    return min(BACKOFF_BASE_S * (2 ** max(0, streak)), BACKOFF_CAP_S)


def _should_run_now(state: dict, force: bool = False) -> bool:
    """True if a real audit pass is due (or forced)."""
    if force:
        return True
    next_run = state.get("backoff", {}).get("next_run_at", 0)
    return time.time() >= next_run


def _update_backoff(state: dict, signature: str, found: int) -> None:
    """Advance or reset the backoff after a real pass.

    - any finding this run → reset to 1h
    - any open (unresolved) finding → hold at 1h (watch known-broken lanes)
    - signature changed vs last run → reset to 1h (novelty)
    - otherwise → clean streak+1, interval doubles toward the 24h cap
    """
    b = state.setdefault("backoff", {})
    now = time.time()
    findings = state.get("findings", {})
    open_any = any(f.get("status") == "open" for f in findings.values())
    changed = b.get("last_signature") not in (None, signature)

    if found or open_any or changed:
        b["clean_streak"] = 0
        b["next_run_at"] = now + BACKOFF_BASE_S
    else:
        streak = b.get("clean_streak", 0) + 1
        b["clean_streak"] = streak
        b["next_run_at"] = now + _backoff_interval(streak)
    b["last_signature"] = signature
    b["last_run_at"] = now
    state["backoff"] = b
    _save_state(state)


def _hermes_env() -> dict:
    e = dict(os.environ)
    e["HERMES_URGENCY_EXEMPT"] = "1"
    return e


def _create_fix_task(category: str, lane: str, title: str,
                     detail: str) -> str | None:
    """Create a SOON-priority fix task on llm-routing. Returns task id or None."""
    week = time.strftime("%G-W%V")
    ikey = f"lane-audit-{category}-{lane}-{week}"
    qgates = ("QUALITY GATES (MANDATORY — paste evidence in completion summary):\n"
              "- Gate 1 (TDD): failing test first. RED->GREEN.\n"
              "- Gate 2: full suite green, coverage >=80%.\n"
              "- Gate 2.5 (Cold review): cross-family (kimi-consultant) cold review. Paste JSON verdict.\n"
              "- Gate 3 (Docs): .md changed in same commit.\n"
              "- Gate 4: atomic conventional commits.\n"
              "- Gate 5: git push verified.\n"
              "- Gate 8: consolidate to main / PR with task-id.\n"
              "- Gate 6: status=review, NOT done (manager approves).\n"
              "- Gate 9: DECLARED EXEMPT (backend-only).")
    body = (f"Urgency: SOON (auto-scheduled by lane-wiring-audit).\n\n"
            f"{qgates}\n\n"
            f"TASK: {title}\n\n"
            f"EVIDENCE: {detail}\n\n"
            f"Category {category}, lane {lane}, detected {time.strftime('%Y-%m-%d %H:%M')}. "
            f"This lane is usable (or believed usable) but cannot serve traffic — fix the "
            f"wiring/config/health-state so the market can route through it.")
    try:
        r = subprocess.run(
            ["hermes", "kanban", "--board", "llm-routing", "create", title,
             "--assignee", "worker-routing", "--priority", "1",
             "--model", "glm-5.3", "--provider", "zai",
             "--initial-status", "blocked", "--created-by", "manager",
             "--idempotency-key", ikey, "--json", "--body", body],
            env=_hermes_env(), timeout=90, capture_output=True, text=True)
        if r.returncode == 0:
            try:
                j = json.loads(r.stdout or "{}")
                tid = j.get("id")
                if tid:
                    subprocess.run(["hermes", "kanban", "--board", "llm-routing",
                                    "unblock", tid,
                                    "lane-wiring-audit: fix scheduled, priority SOON"],
                                   env=_hermes_env(), timeout=60, capture_output=True)
                return tid
            except Exception:
                pass
    except Exception:
        pass
    return None


def audit(dry_run: bool = False, state: dict | None = None) -> int:
    global _DRY_RUN
    _DRY_RUN = dry_run
    env = _load_env()
    quota = fetch_quota()
    health = read_key_health()
    spend = read_1h_spend()
    flags = disabled_flags()
    state = state if state is not None else _load_state()

    found = 0
    findings = state.setdefault("findings", {})
    signature = _state_signature(quota, health)

    # ── End-to-end wiring gap ──────────────────────────────────────────────
    xprov = probe_end_to_end(state=state)
    if xprov and xprov not in ("none",):
        if xprov in PAYGO_LANES:
            # PAYGO won for glm-5.2 — check if a quota lane should have won.
            culprit = None
            for lane in ("ollama_cloud", "ollama_cloud_4", "ollama_cloud_2",
                         "opencode_go", "ours"):
                if lane in flags:
                    continue
                if _quota_has_headroom(quota, lane) and health.get(lane, {}).get("healthy"):
                    culprit = lane
                    break
            if culprit:
                _emit_finding("LANE_WIRING_GAP", culprit,
                              f"glm-5.2 routed to {xprov} while {culprit} is healthy+headroom",
                              f"end-to-end probe X-Provider={xprov}; "
                              f"{culprit} quota headroom but not winning routing.",
                              state, dry_run)
                found += 1

    # ── Per-lane probe: dispatch-fail paradox + quota-model drift ──────────
    fc_prev = state.setdefault("failure_counts", {})
    for lane in sorted(QUOTA_LANES):
        if lane in flags:
            continue
        h = health.get(lane, {})
        has_headroom = _quota_has_headroom(quota, lane)
        probe_code = probe_lane(lane, env, state)

        fc_now = h.get("failure_count", 0) or 0
        fc_before = fc_prev.get(lane, fc_now)
        fc_prev[lane] = fc_now

        # SUSTAINED_DISPATCH_FAIL: failures ACTIVELY climbing while probe works
        # and quota headroom exists → live wiring/config gap (not stale).
        if (h.get("last_error_type") == "dispatch_fail"
                and fc_now > fc_before
                and fc_now >= DISPATCH_FAIL_STREAK
                and has_headroom and probe_code == 200):
            _emit_finding("SUSTAINED_DISPATCH_FAIL", lane,
                          f"{lane} dispatch-failing ({fc_now}x, +{fc_now - fc_before} this run) but probe=200",
                          f"key_health dispatch_fail x{fc_now} (climbing); "
                          f"live probe HTTP 200; quota headroom present. Wiring/config gap.",
                          state, dry_run)
            found += 1
        elif h.get("last_error_type") == "dispatch_fail" and has_headroom and probe_code == 200:
            _resolve_finding("SUSTAINED_DISPATCH_FAIL", lane, state, dry_run)

        # QUOTA_MODEL_DRIFT: /quota believes headroom but probe says limited,
        # OR proxy marks exhausted/dead but probe succeeds (stale backoff).
        # "Stale backoff" requires the bench to be STILL ACTIVE: the mirror's
        # last_error_type lingers after recovery (sticky until the next state
        # transition), so it alone is not a bench. Only fire when
        # backoff_until is in the future — the 2026-09-09 oc2 false-positive
        # fired 94s after a 2s backoff had already expired (one transient 429,
        # self-healed by the proxy's server-truth recovery 3min later).
        _benched = (h.get("last_error_type") in ("exhausted", "dead")
                    and float(h.get("backoff_until") or 0) > time.time())
        if probe_code in (429, 403) and has_headroom:
            _emit_finding("QUOTA_MODEL_DRIFT", lane,
                          f"{lane} /quota reports headroom but probe returns {probe_code}",
                          f"/quota regime/headroom says available, live probe HTTP {probe_code}.",
                          state, dry_run)
            found += 1
        elif probe_code == 200 and _benched and has_headroom:
            _emit_finding("QUOTA_MODEL_DRIFT", lane,
                          f"{lane} marked {h.get('last_error_type')} but probe=200 (stale backoff)",
                          f"key_health={h.get('last_error_type')} backoff {h.get('backoff_seconds')}s "
                          f"(active until {h.get('backoff_until'):.0f}) "
                          f"but live probe HTTP 200.",
                          state, dry_run)
            found += 1
        elif probe_code == 200 and has_headroom \
                and f"QUOTA_MODEL_DRIFT:{lane}" in findings \
                and findings[f"QUOTA_MODEL_DRIFT:{lane}"].get("status") == "open":
            # Mirror recovered (error cleared or backoff expired) and the lane
            # serves live traffic — resolve the open finding so recurrence
            # detection re-arms instead of latching open forever.
            _resolve_finding("QUOTA_MODEL_DRIFT", lane, state, dry_run)

    # ── COST_LEAK: PAYGO spend while a quota lane idled ─────────────────────
    paygo_tokens = sum(v["tokens"] for k, v in spend.items() if k in PAYGO_LANES)
    paygo_cost = sum(v["cost"] for k, v in spend.items() if k in PAYGO_LANES)
    if paygo_cost >= COST_LEAK_MIN_USD and paygo_tokens >= COST_LEAK_MIN_TOK:
        idle = []
        for lane in sorted(QUOTA_LANES):
            if lane in flags:
                continue
            if not _quota_has_headroom(quota, lane):
                continue
            if not health.get(lane, {}).get("healthy"):
                continue
            if spend.get(lane, {}).get("tokens", 0) < paygo_tokens * 0.05:
                idle.append(lane)
        if idle:
            _emit_finding("COST_LEAK", idle[0],
                          f"PAYGO carried {paygo_tokens/1e6:.1f}M tokens (${paygo_cost:.2f}/h) "
                          f"while {','.join(idle)} sat healthy+idle",
                          f"last-1h PAYGO {paygo_tokens} tokens ${paygo_cost:.2f}; "
                          f"healthy idle quota lanes: {','.join(idle)}.",
                          state, dry_run)
            found += 1
    else:
        # Leak cleared — PAYGO spend below threshold this window. Resolve any
        # open COST_LEAK findings so recurrence re-arms instead of latching open
        # forever (the 2026-09-09 t_efc68b73 latch: the outage ended at the pool
        # restore, but the finding stayed "open" and held the backoff at 1h).
        for key in list(findings):
            if key.startswith("COST_LEAK:") and findings[key].get("status") == "open":
                _resolve_finding("COST_LEAK", key.split(":", 1)[1], state, dry_run)

    # ── Backoff bookkeeping (real runs only; dry-run never mutates) ────────
    if not dry_run:
        _update_backoff(state, signature, found)
    else:
        _save_state(state)
    return 1 if found else 0


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    force = "--run-now" in sys.argv
    if not dry:
        state = _load_state()
        if not _should_run_now(state, force=force):
            # Not due yet (backoff active) — silent no-op.
            sys.exit(0)
    sys.exit(audit(dry_run=dry))
