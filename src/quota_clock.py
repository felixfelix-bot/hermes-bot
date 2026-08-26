"""quota_clock.py — Per-provider quota window anchor registry.

Provides ``window_start(provider, kind, now)`` that returns the epoch-second
start of the *current* quota window for *provider* of *kind*
("session" | "weekly" | "monthly").

Anchor precedence (highest first):
  1. API-fetched — for providers whose quota endpoint exposes ``nextResetTime``
     (z.ai). Refreshed lazily; cached in the registry state file.
  2. Learned — for providers with no API but whose dashboard publishes a reset
     time (ollama_cloud). Seeded by the operator or learned from observations.
  3. Rolling fallback — ``now - window_duration`` (the historical behaviour).

Window alignment is *weekly-only* per ADR-014. Sessions stay rolling on every
provider (z.ai doesn't publish session resets; ollama's session semantics are
opaque; user decision 2026-08-26).

Kill-switch: ``QUOTA_CLOCK_ALIGN_ENABLED=false`` reverts every consumer to the
rolling fallback. The registry state file remains on disk but is unused.

State file: ``~/.hermes/bot/quota_clock_state.json``
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

_HOME = Path.home()
_STATE_PATH = _HOME / ".hermes" / "bot" / "quota_clock_state.json"

_WINDOW_DURATIONS_S = {
    "session":  5 * 3600,     # 5 hours
    "weekly":   7 * 86400,    # 7 days
    "monthly": 30 * 86400,    # ~30 days
}

_ENABLED = os.environ.get("QUOTA_CLOCK_ALIGN_ENABLED", "true").lower() in ("1", "true", "yes", "on")


def _load_state() -> dict:
    """Load the registry state, or return an empty dict if missing/corrupt."""
    try:
        return json.loads(_STATE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    """Persist the registry state (best-effort, never raises)."""
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))
    except OSError:
        pass


def _rolling_start(kind: str, now: float) -> float:
    """Rolling-window fallback: ``now - window_duration``."""
    return now - _WINDOW_DURATIONS_S.get(kind, _WINDOW_DURATIONS_S["weekly"])


def _anchored_start(anchor_ts: float, duration_s: int, now: float) -> float:
    """Compute the current window start given an anchor and duration.

    The anchor is the *next* reset time. The current window's start is
    ``anchor - duration`` (the window that contains ``now``). If the anchor
    has already passed, fall back to rolling.
    """
    if anchor_ts <= now:
        return _rolling_start_from_duration(duration_s, now)
    start = anchor_ts - duration_s
    if start > now:
        start = anchor_ts - 2 * duration_s
    return max(start, now - duration_s)


def _rolling_start_from_duration(duration_s: int, now: float) -> float:
    return now - duration_s


def window_start(provider: str, kind: str, now: Optional[float] = None) -> float:
    """Return the epoch-second start of the current quota window.

    Falls back to rolling when alignment is disabled or no anchor is known.
    """
    if now is None:
        now = time.time()
    if not _ENABLED:
        return _rolling_start(kind, now)

    state = _load_state()
    p_entry = state.get(provider, {})
    anchor = p_entry.get(f"{kind}_anchor_ts")
    if not anchor:
        return _rolling_start(kind, now)

    duration = _WINDOW_DURATIONS_S.get(kind, _WINDOW_DURATIONS_S["weekly"])
    return _anchored_start(float(anchor), duration, now)


def next_reset(provider: str, kind: str = "weekly") -> Optional[float]:
    """Return the next known reset timestamp (epoch seconds), or None."""
    if not _ENABLED:
        return None
    state = _load_state()
    return state.get(provider, {}).get(f"{kind}_anchor_ts")


def register_anchor(provider: str, kind: str, anchor_ts: float) -> None:
    """Record (or overwrite) an anchor in the registry state file."""
    state = _load_state()
    if provider not in state:
        state[provider] = {}
    state[provider][f"{kind}_anchor_ts"] = float(anchor_ts)
    state[provider][f"{kind}_anchor_updated"] = time.time()
    _save_state(state)


def fetch_zai_anchors(api_key: str) -> dict:
    """Fetch nextResetTime for all z.ai windows from the quota API.

    Returns ``{kind: anchor_ts}`` where ``kind`` is one of
    ``"session"``, ``"weekly"``, ``"monthly"`` (best-effort mapping).
    Empty dict on failure.
    """
    import urllib.request
    result = {}
    try:
        req = urllib.request.Request(
            "https://api.z.ai/api/monitor/usage/quota/limit",
            headers={"Authorization": f"Bearer {api_key}",
                     "User-Agent": "Mozilla/5.0",
                     "Accept-Language": "en-US,en"},
        )
        with urllib.request.urlopen(req, timeout=12) as r:
            payload = json.loads(r.read())
        if payload.get("code") != 200:
            return result
        for entry in payload.get("data", {}).get("limits", []):
            reset_ms = entry.get("nextResetTime", 0)
            if not reset_ms:
                continue
            unit   = entry.get("unit", 0)
            number = entry.get("number", 0)
            if   unit == 3:  kind = "session"
            elif unit == 6:  kind = "weekly"
            elif unit == 5:  kind = "monthly"
            else:            continue
            result[kind] = reset_ms / 1000.0
    except Exception:
        pass
    return result


def refresh_zai_anchors(api_key: str, provider_name: str) -> int:
    """Fetch z.ai anchors and write them to the registry under *provider_name*.

    Returns the number of anchors refreshed.
    """
    anchors = fetch_zai_anchors(api_key)
    if not anchors:
        return 0
    for kind, ts in anchors.items():
        register_anchor(provider_name, kind, ts)
    return len(anchors)
