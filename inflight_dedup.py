"""inflight_dedup.py — short-window coalescing for duplicate in-flight requests.

WHY (measured 2026-09-12 on this host)
--------------------------------------
The `routstrd` caller key fanned the *same* request out many times in parallel:
35 identical calls were logged inside 0.7 s (same 67,627-token prompt, same
67,584 cached tokens, every one returning HTTP 200 after 90-180 s). Over one
hour, 561 of 1655 logged calls (34%) were duplicates of another call in the same
second — 104 duplicate groups. Each duplicate burns a full upstream call and a
worker thread; on a 4-core / 7 GB box that is what drove swap past 9 GB and
tripped the token-bleed guard's ESTOP.

Every duplicate is an *inbound* request: the proxy logs one row per inbound
request and all of them succeeded, so this is caller-side fan-out, not
proxy-side retry-after-failure.

WHAT THIS DOES
--------------
While an identical request (client key + model + normalized body) is in flight,
later identical requests inside the hold window are reported as duplicates so
the caller can be answered with HTTP 429 + Retry-After instead of a second
upstream call. One upstream call per distinct request; the duplicates are shed
at the edge.

Default is OFF (``window_s=0.0``): import, wire, and enable deliberately via
``PROXY_INFLIGHT_DEDUP_WINDOW_S``.

Thread-safe: the proxy serves requests from a thread pool, so every mutation is
guarded by a lock.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Tuple

__all__ = ["normalize_body", "fingerprint", "InflightDedup", "Decision"]

# Body fields that legitimately differ between two otherwise identical requests.
# Only fields here are stripped before hashing.
DEFAULT_IGNORE_KEYS: Tuple[str, ...] = (
    "user",
    "metadata",
    "request_id",
    "idempotency_key",
    "stream",
    "stream_options",
    "logprobs",
    "top_logprobs",
)

LEADER = "leader"
DUPLICATE = "duplicate"


def normalize_body(raw: Any, ignore_keys: Optional[Iterable[str]] = None) -> str:
    """Canonical JSON string for hashing: key order and whitespace independent.

    ``raw`` may be ``bytes``/``str`` (parsed as JSON) or an already-decoded
    object. Unparseable input degrades to the raw text with whitespace collapsed,
    so dedup still works for non-JSON bodies instead of raising.
    """
    ignore = set(DEFAULT_IGNORE_KEYS if ignore_keys is None else ignore_keys)
    obj: Any = raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            return " ".join(raw.split())
    if isinstance(obj, dict):
        obj = {k: v for k, v in obj.items() if k not in ignore}
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def fingerprint(client_key: str, model: str, body: Any,
                ignore_keys: Optional[Iterable[str]] = None) -> str:
    """Stable sha256 over (client key, model, normalized body)."""
    payload = "\x00".join((str(client_key or ""), str(model or ""),
                           normalize_body(body, ignore_keys)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Decision:
    """Outcome of :meth:`InflightDedup.begin`."""

    state: str                 # LEADER or DUPLICATE
    fingerprint: str
    inflight_for_s: float      # how long the identical request has been running
    retry_after_s: float       # suggested Retry-After for duplicates

    @property
    def is_duplicate(self) -> bool:
        return self.state == DUPLICATE


class InflightDedup:
    """Tracks in-flight request fingerprints.

    ``window_s``  how long after the leader starts new identical requests are
                  treated as duplicates (0 disables dedup entirely).
    ``hard_ttl_s`` absolute cap on how long an entry may suppress duplicates —
                  protects against a leaked entry (crashed handler) silencing a
                  request forever.
    ``max_entries`` bound on tracked entries; oldest are evicted first.
    """

    def __init__(self, window_s: float = 0.0, hard_ttl_s: float = 300.0,
                 max_entries: int = 1024, clock=time.monotonic):
        self.window_s = max(0.0, float(window_s))
        self.hard_ttl_s = max(self.window_s, float(hard_ttl_s))
        self.max_entries = max(1, int(max_entries))
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: Dict[str, float] = {}          # fp -> started_at
        self._leaders = 0
        self._duplicates = 0
        self._expired = 0

    # -- core ---------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self.window_s > 0.0

    def begin(self, fp: str) -> Decision:
        """Register ``fp``; returns LEADER for the first, DUPLICATE for twins."""
        now = self._clock()
        with self._lock:
            self._prune(now)
            started = self._entries.get(fp)
            if started is None or not self.enabled:
                self._entries[fp] = now
                self._prune(now)          # enforce the cap after inserting
                self._leaders += 1
                return Decision(LEADER, fp, 0.0, 0.0)
            age = now - started
            self._duplicates += 1
            return Decision(DUPLICATE, fp, age, max(0.0, self.window_s - age))

    def end(self, fp: str) -> None:
        """Release an entry when its request finishes (no-op if unknown)."""
        with self._lock:
            self._entries.pop(fp, None)

    # -- introspection ------------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.enabled,
                "window_s": self.window_s,
                "hard_ttl_s": self.hard_ttl_s,
                "inflight": len(self._entries),
                "leaders": self._leaders,
                "duplicates": self._duplicates,
                "expired": self._expired,
                "dup_ratio": (self._duplicates / (self._leaders + self._duplicates)
                              if (self._leaders + self._duplicates) else 0.0),
            }

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()
            self._leaders = self._duplicates = self._expired = 0

    # -- internals ----------------------------------------------------------
    def _prune(self, now: float) -> None:
        stale = [fp for fp, t in self._entries.items()
                 if now - t > self.hard_ttl_s]
        for fp in stale:
            self._entries.pop(fp, None)
            self._expired += 1
        if len(self._entries) > self.max_entries:
            for fp, _ in sorted(self._entries.items(), key=lambda kv: kv[1])[
                    : len(self._entries) - self.max_entries]:
                self._entries.pop(fp, None)
                self._expired += 1


def from_env(env: Dict[str, str]) -> InflightDedup:
    """Build from environment variables (0 window == disabled)."""
    def _f(name: str, default: float) -> float:
        try:
            return float(env.get(name, default))
        except (TypeError, ValueError):
            return default

    return InflightDedup(
        window_s=_f("PROXY_INFLIGHT_DEDUP_WINDOW_S", 0.0),
        hard_ttl_s=_f("PROXY_INFLIGHT_DEDUP_TTL_S", 300.0),
        max_entries=int(_f("PROXY_INFLIGHT_DEDUP_MAX_ENTRIES", 1024)),
    )
