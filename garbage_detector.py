#!/usr/bin/env python3
"""garbage_detector — content-quality garbage detection + market-based backoff.

Design (docs/PLAN-garbage-output-detection.md, 2026-09-07):
  * The proxy sees ALL model traffic (manager<->worker included), so content
    garbage is detected HERE, centrally, on every successful (HTTP 200)
    completion. The response is still delivered to the caller — pass-through
    by operator choice; NO in-flight failover.
  * Backoff is MARKET-BASED: garbage strikes raise the (provider, model)
    pair's effective price via flat_router's select_provider(), so cheaper
    healthy lanes win naturally. Strikes decay out of the window — the
    penalty is temporary. Fails open to 1.0 on any error or cold start.

Env knobs (read once at import; fail-open everywhere):
  GARBAGE_PRICE_ENABLED          = "1" (default) — "0"/"false"/"no" disables
  GARBAGE_LEDGER_ENABLED         = "1" (default) — ledger writes on/off
  GARBAGE_PRICE_BASE_MULT        = "3.0"  — multiplier per strike (BASE^n)
  GARBAGE_PRICE_WINDOW           = "3600" — seconds; strikes decay out of it
  GARBAGE_PRICE_MAX_STRIKES      = "4"    — strikes at cap => price mult = inf
  GARBAGE_MAX_COMPLETION_TOKENS  = "32000" — oversized-completion detector
  GARBAGE_LEDGER_PATH            = <bot>/garbage_ledger.jsonl by default
  GARBAGE_LEDGER_MAX_BYTES       = "5242880" — simple .1 rotation
  GARBAGE_REPETITION_RATIO       = "0.12"  — zlib compress-ratio ceiling
  GARBAGE_REPETITION_LINE_DOM    = "0.70"  — dominant-line fraction ceiling
  GARBAGE_MOJIBAKE_MAX_COUNT     = "8"     — U+FFFD replacements allowed
  GARBAGE_GIBBERISH_MAX_HITRATE  = "0.05"  — common-word hit-rate floor
  GARBAGE_GIBBERISH_MIN_WORDS    = "120"   — words needed to judge language
  GARBAGE_NONLATIN_MIN_RATIO     = "0.70"  — CJK fraction for nonlatin_spam

Kill-switch (no restart; 30s cache):  touch ~/.hermes/bot/.disable_garbage_pricing
Re-enable:                             rm  ~/.hermes/bot/.disable_garbage_pricing

Phase A (2026-09-08): Garbage hardening on 3 flat-rate lanes + keying-consistency fix.
  * _garbage_check now fires from _try_ollama_cloud, _try_telnyx, AND
    _try_opencode_go — closing the coverage hole (G2) where flat-rate lanes
    were never garbage-scored.
  * Strikes are recorded via _resolve_model_for_provider(provider, model) so
    the strike key and the price-lookup key are always the same (G3).
  * All call sites go through the one _garbage_check helper which resolves
    the key before recording.
  * Tested via TDD: source-inspection for the call sites + PROVIDER_MODELS-
    spanning keying-consistency assertion.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import zlib
from collections import Counter
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent


def _env_flag(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() not in ("0", "false", "no", "off")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


PRICE_ENABLED: bool = _env_flag("GARBAGE_PRICE_ENABLED", True)
LEDGER_ENABLED: bool = _env_flag("GARBAGE_LEDGER_ENABLED", True)
PRICE_BASE_MULT: float = max(1.0, _env_float("GARBAGE_PRICE_BASE_MULT", 3.0))
PRICE_WINDOW: float = max(1.0, _env_float("GARBAGE_PRICE_WINDOW", 3600.0))
PRICE_MAX_STRIKES: int = max(1, _env_int("GARBAGE_PRICE_MAX_STRIKES", 4))
MAX_COMPLETION_TOKENS: int = max(0, _env_int("GARBAGE_MAX_COMPLETION_TOKENS", 32000))

REPETITION_MIN_CHARS: int = 300
REPETITION_MAX_RATIO: float = _env_float("GARBAGE_REPETITION_RATIO", 0.12)
REPETITION_MIN_LINES: int = 10
REPETITION_LINE_DOMINANCE: float = _env_float("GARBAGE_REPETITION_LINE_DOM", 0.70)
MOJIBAKE_MAX_COUNT: int = _env_int("GARBAGE_MOJIBAKE_MAX_COUNT", 8)
CONTROL_MAX_RATIO: float = _env_float("GARBAGE_CONTROL_MAX_RATIO", 0.005)
GIBBERISH_MIN_WORDS: int = _env_int("GARBAGE_GIBBERISH_MIN_WORDS", 120)
GIBBERISH_MAX_HITRATE: float = _env_float("GARBAGE_GIBBERISH_MAX_HITRATE", 0.05)
NONLATIN_ENABLED: bool = _env_flag("GARBAGE_NONLATIN_ENABLED", False)
NONLATIN_MIN_RATIO: float = _env_float("GARBAGE_NONLATIN_MIN_RATIO", 0.70)

LEDGER_PATH: Path = Path(os.environ.get(
    "GARBAGE_LEDGER_PATH", str(BOT_DIR / "garbage_ledger.jsonl")))
LEDGER_MAX_BYTES: int = _env_int("GARBAGE_LEDGER_MAX_BYTES", 5 * 1024 * 1024)

_SNIPPET_MAX = 500

_LOCK = threading.Lock()
# (provider, model_lower) -> [strike timestamps within window]
_STRIKES: dict[tuple[str, str], list[float]] = {}
# pair -> ts of the last first-in-window alert (alert-once semantics)
_ALERTED_WINDOW: dict[tuple[str, str], float] = {}
_KILLSWITCH_TS: dict[str, object] = {"checked": 0.0, "present": False}

# ~300 most common English words — enough coverage that natural English
# hits 30-60% while keyboard mash / phonetic gibberish hits ~0%.
_COMMON_WORDS = frozenset("""a about after all also am an and any are as at be
been being but by can could did do does doing down even every for from get
give go had has have he her here him his how i if in into is it its just know
like made make many may me more most much must my no nor not now of off on
only or other our out over own said say see she should so some such than that
the their them then there these they this those through to too under until up
use very want was way we well were what when where which while who why will
with would you your about above across against among around because before
behind below between both during each few first found great group him however
important large last later least left less long looked major many next often
part people place point possible right same small still thing think three
time together took true try turn two used work world year yes number without
within able across along already always among another anyone around become
becomes been before behind believe best better between big both bring build
came case cause certain change child come company consider could course day
different does done early end enough even ever fact feel felt find first found
four free full further gave getting given goes going gone got great group
happened hard help high hold home hour idea kind knew know known late learn
least leave let level light likely little live lo long look looking lost
love main means might mind moment month months moved much name near need
never new news next nothing now number off often oh okay old once ones open
order others outside own past perhaps person place plan play point policy
problem process public put quality question real really recent research
result right room run said same says school second see seem seen sense
service set several shall she short show side since small society some
something sometimes soon sound space speak specific still story study
stuff sure system take talk talk talking tell term test thank that thats
their them themselves then theory there therefore thus together told
took town true try trying turn turning two type understand understand
upon us use used using usually very view want war way week well went were
what when whether which while whole whose why wife will within without word
words work working world would write year years""".split())

_WORD_RE = re.compile(r"[A-Za-z]{2,}")
_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


# ── kill-switch ──────────────────────────────────────────────────────────────

def _killswitch_active() -> bool:
    """True if ~/.hermes/bot/.disable_garbage_pricing exists (30s cache,
    fail-closed to the LAST known state on errors)."""
    now = time.time()
    checked = _KILLSWITCH_TS["checked"]
    if isinstance(checked, (int, float)) and (now - checked) < 30:
        return bool(_KILLSWITCH_TS["present"])
    present = False
    try:
        present = os.path.exists(str(BOT_DIR / ".disable_garbage_pricing"))
    except Exception:
        present = bool(_KILLSWITCH_TS["present"])
    _KILLSWITCH_TS["checked"] = now
    _KILLSWITCH_TS["present"] = present
    return present


# ── pure detectors (no state, no IO) ────────────────────────────────────────

def _snippet(text: str) -> str:
    """Capped, scrubbed evidence snippet for the ledger/alerts."""
    if not isinstance(text, str):
        text = str(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch if (ch.isprintable() or ch in "\n\t") else " "
                   for ch in text)
    return text[:_SNIPPET_MAX]


def _repetition_reason(text: str) -> str | None:
    """Degenerate repetition: zlib-compresses absurdly well, or one line
    dominates the output. Code and valid JSON compress at 0.2-0.4; a
    repetition loop lands well under 0.10."""
    if len(text) > REPETITION_MIN_CHARS:
        raw = text.encode("utf-8", "ignore")
        try:
            ratio = len(zlib.compress(raw, 6)) / max(1, len(raw))
        except Exception:
            ratio = 1.0
        if ratio < REPETITION_MAX_RATIO:
            return "repetition_compress"
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) >= REPETITION_MIN_LINES:
        freq = Counter(lines).most_common(1)[0][1]
        if freq / len(lines) >= REPETITION_LINE_DOMINANCE:
            return "repetition_lines"
    return None


def _response_wants_json(request_json) -> bool:
    """True when the CLIENT asked for a JSON response (OpenAI contract)."""
    if not isinstance(request_json, dict):
        return False
    rf = request_json.get("response_format")
    if isinstance(rf, dict) and rf.get("type") in ("json_object", "json_schema"):
        return True
    if isinstance(rf, str) and rf == "json_object":
        return True
    return False


def _bad_json_reason(text: str) -> str | None:
    """Content fails json.loads (fenced ```json block accepted) when the
    request demanded JSON."""
    m = _FENCE_RE.search(text)
    candidate = m.group(1).strip() if m else text.strip()
    try:
        json.loads(candidate)
        return None
    except Exception:
        return "bad_json"


def _nonascii_reason(text: str) -> str | None:
    """Mojibake / control-char spam."""
    n_repl = text.count("\ufffd")
    if n_repl >= MOJIBAKE_MAX_COUNT:
        return "mojibake"
    if len(text) > 200 and n_repl / max(1, len(text)) > 0.01:
        return "mojibake"
    if len(text) > 100:
        ctrl = sum(1 for ch in text if ord(ch) < 32 and ch not in "\t\n\r")
        if ctrl / max(1, len(text)) > CONTROL_MAX_RATIO:
            return "control_chars"
    return None


def _gibberish_reason(text: str) -> str | None:
    """Keyboard-mash / phonetic spam detection for natural-language output.

    Conservative: structured content (fences, JSON, symbol-heavy), short
    texts and non-Latin scripts are skipped. Only fires on a large English-
    looking word sample whose common-word hit-rate is ~zero. English prose
    hits 30-60% vs the ~300-word list; mash hits ~0%."""
    stripped = text.strip()
    if stripped.startswith(("```", "{", "[")):
        return None
    # Opt-in non-latin spam: CJK-dominated with zero English words. OFF by
    # default (GARBAGE_NONLATIN_ENABLED) — legit non-English chat must never
    # be penalized; the fleet of agents is English-language ops.
    if NONLATIN_ENABLED:
        n = len(stripped)
        if n > 200:
            cjk = sum(1 for ch in stripped if "\u4e00" <= ch <= "\u9fff")
            if cjk / max(1, n) > NONLATIN_MIN_RATIO:
                probe = _WORD_RE.findall(stripped.lower())
                if not any(w in _COMMON_WORDS for w in probe):
                    return "nonlatin_spam"
    letters = sum(1 for ch in stripped if ch.isalpha())
    if letters < 100:
        return None
    if letters / max(1, len(stripped)) < 0.55:
        return None  # symbol-heavy (code-ish) — not our signal
    words = _WORD_RE.findall(stripped.lower())[:250]
    if len(words) < GIBBERISH_MIN_WORDS:
        return None
    hits = sum(1 for w in words if w in _COMMON_WORDS)
    if hits / len(words) < GIBBERISH_MAX_HITRATE:
        return "gibberish_english"
    return None


def looks_like_garbage(content, request_json=None) -> dict | None:
    """Content-level garbage checks. Returns {'reason', 'snippet'} or None.

    Pure: no state, no IO. Safe to unit-test and to call speculatively."""
    try:
        text = content if isinstance(content, str) else ("" if content is None else str(content))
        if not text.strip():
            return {"reason": "empty", "snippet": ""}

        for fn in (_nonascii_reason, _repetition_reason):
            reason = fn(text)
            if reason:
                return {"reason": reason, "snippet": _snippet(text)}

        if _response_wants_json(request_json):
            reason = _bad_json_reason(text)
            if reason:
                return {"reason": reason, "snippet": _snippet(text)}

        reason = _gibberish_reason(text)
        if reason:
            return {"reason": reason, "snippet": _snippet(text)}
        return None
    except Exception:
        return None


# ── response parsing (JSON + SSE) ───────────────────────────────────────────

def _extract_sse(text: str) -> tuple[str, dict]:
    """Accumulate delta.content (+ reasoning fallback) + last usage from an
    SSE buffer. Mirrors zai_proxy FIX-1 (2026-09-02) for streams."""
    parts: list[str] = []
    reasoning_parts: list[str] = []
    usage: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        if isinstance(obj.get("usage"), dict):
            usage = obj["usage"]
        for ch in (obj.get("choices") or []):
            if not isinstance(ch, dict):
                continue
            delta = ch.get("delta") or {}
            c = delta.get("content")
            r = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(c, str) and c:
                parts.append(c)
            elif isinstance(r, str) and r:
                reasoning_parts.append(r)
    joined = "".join(parts)
    if not joined.strip():
        joined = "".join(reasoning_parts)
    return joined, usage


def inspect_response(resp_bytes, request_body=None, completion_tokens: int = 0) -> dict | None:
    """Parse a successful upstream completion (plain JSON or SSE) and run
    the detectors + oversize check. Returns garbage-info dict or None.

    Pure with respect to strike state (does not record anything)."""
    try:
        if not isinstance(resp_bytes, (bytes, bytearray, str)):
            return None
        text = (resp_bytes if isinstance(resp_bytes, str)
                else bytes(resp_bytes).decode("utf-8", "replace")).strip()
        if not text:
            return {"reason": "empty_body", "snippet": ""}

        resp_json = None
        usage: dict = {}
        content: str | None = None
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                resp_json = obj
                usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
                choices = obj.get("choices") or []
                if choices and isinstance(choices[0], dict):
                    msg = choices[0].get("message") or {}
                    c = msg.get("content")
                    content = c if isinstance(c, str) else None
                    reasoning = (msg.get("reasoning_content")
                                 or msg.get("reasoning") or "")
                    if (not content or not content.strip()) and isinstance(reasoning, str) and reasoning.strip():
                        content = reasoning  # mirrors zai_proxy FIX-1 (2026-09-02)
                else:
                    # JSON body without choices (upstream error, usage-only
                    # ack, non-completion payload) — not a judgeable completion.
                    return None
        except Exception:
            pass

        if resp_json is None:
            # SSE stream — accumulate deltas (content, reasoning fallback)
            sse_content, usage = _extract_sse(text)
            content = sse_content or None
            if not usage and not (sse_content or "").strip():
                return None  # not a completion stream we can judge

        request_json = None
        if request_body:
            try:
                request_json = json.loads(
                    bytes(request_body).decode("utf-8", "ignore"))
            except Exception:
                request_json = None

        g = looks_like_garbage(content, request_json)

        comp_tok = int(completion_tokens or 0)
        if comp_tok <= 0 and usage:
            try:
                comp_tok = int(usage.get("completion_tokens") or 0)
            except (TypeError, ValueError):
                comp_tok = 0
        if not g and comp_tok > 0 and MAX_COMPLETION_TOKENS > 0 and comp_tok > MAX_COMPLETION_TOKENS:
            g = {"reason": "oversized", "snippet": _snippet(content or "")}

        if g:
            g = dict(g)
            g.setdefault("completion_tokens", comp_tok)
            try:
                g.setdefault("prompt_tokens", int(usage.get("prompt_tokens") or 0))
            except (TypeError, ValueError):
                g.setdefault("prompt_tokens", 0)
        return g
    except Exception:
        return None


# ── strikes + market price multiplier ────────────────────────────────────────

def _pair(name, model) -> tuple[str, str]:
    return (str(name or "?"), str(model or "?").lower())


def garbage_price_mult(name, model) -> float:
    """Effective-price multiplier for (provider, model): 1.0 clean, BASE^n
    escalating, inf at MAX_STRIKES, decaying to 1.0 once strikes age out of
    the window. Fail-open: 1.0 on any error, cold start, or kill-switch."""
    try:
        if not PRICE_ENABLED or _killswitch_active():
            return 1.0
        key = _pair(name, model)
        now = time.time()
        with _LOCK:
            ts_list = [ts for ts in _STRIKES.get(key, [])
                      if (now - ts) <= PRICE_WINDOW]
        n = len(ts_list)
        if n <= 0:
            return 1.0
        if n >= PRICE_MAX_STRIKES:
            return float("inf")
        return float(PRICE_BASE_MULT) ** n
    except Exception:
        return 1.0


def strikes_in_window(name, model) -> int:
    try:
        key = _pair(name, model)
        now = time.time()
        with _LOCK:
            return len([ts for ts in _STRIKES.get(key, [])
                        if (now - ts) <= PRICE_WINDOW])
    except Exception:
        return 0


# ── ledger ──────────────────────────────────────────────────────────────────

def _append_ledger(evt: dict) -> None:
    """Append one event to the JSONL ledger; rotate at LEDGER_MAX_BYTES.
    Fire-and-forget: never raises."""
    try:
        p = Path(LEDGER_PATH)
        try:
            if p.exists() and p.stat().st_size > LEDGER_MAX_BYTES:
                rot = p.with_name(p.name + ".1")
                try:
                    os.replace(str(p), str(rot))
                except Exception:
                    pass
        except Exception:
            pass
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(evt, ensure_ascii=True, default=str) + "\n")
    except Exception:
        pass


# ── top-level entry (called from zai_proxy hooks) ───────────────────────────

def report_success_response(provider, model, resp_bytes,
                             request_body=None, prompt_tokens: int = 0,
                             completion_tokens: int = 0,
                             duration_ms=None, session_id=None,
                             task_type=None) -> dict | None:
    """Run detection on a DELIVERED successful response; record strike +
    ledger entry. Returns garbage-info dict (with first_in_window +
    strikes_in_window + price_mult) or None when clean. Never raises.

    Pass-through semantics: the caller still delivers the response — this
    only feeds routing (strikes -> price) and observability (ledger)."""
    try:
        if not (PRICE_ENABLED or LEDGER_ENABLED):
            return None
        g = inspect_response(resp_bytes, request_body, completion_tokens)
        if not g:
            return None

        key = _pair(provider, model)
        now = time.time()
        first_in_window = False
        with _LOCK:
            ts_list = [ts for ts in _STRIKES.get(key, [])
                       if (now - ts) <= PRICE_WINDOW]
            ts_list.append(now)
            _STRIKES[key] = ts_list
            last_alert = _ALERTED_WINDOW.get(key, 0.0)
            first_in_window = (now - last_alert) > PRICE_WINDOW
            if first_in_window:
                _ALERTED_WINDOW[key] = now

        info = dict(g)
        info.update({
            "provider": key[0],
            "model": key[1],
            "strikes_in_window": len(ts_list),
            "first_in_window": first_in_window,
            "price_mult": garbage_price_mult(*key),
            "prompt_tokens": int(prompt_tokens or 0) or info.get("prompt_tokens", 0),
            "completion_tokens": (int(completion_tokens or 0)
                                   or info.get("completion_tokens", 0)),
            "duration_ms": duration_ms,
            "session_id": session_id,
            "task_type": task_type,
        })
        if LEDGER_ENABLED:
            _append_ledger({"ts": now, **info})
        return info
    except Exception:
        return None


# ── test/ops helpers ─────────────────────────────────────────────────────────

def reset_state() -> None:
    """Clear in-memory strike + alert state (tests / ops reset)."""
    with _LOCK:
        _STRIKES.clear()
        _ALERTED_WINDOW.clear()


def ledger_events(limit: int = 50) -> list[dict]:
    """Read the newest `limit` ledger events (ops inspection)."""
    try:
        with open(LEDGER_PATH, encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        out = []
        for ln in lines[-limit:]:
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
        return out
    except Exception:
        return []


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="garbage_detector ops")
    ap.add_argument("provider", nargs="?")
    ap.add_argument("model", nargs="?")
    ap.add_argument("--mult", action="store_true",
                    help="print current price multiplier")
    ap.add_argument("--ledger", type=int, default=0,
                    help="print last N ledger events")
    args = ap.parse_args()
    if args.ledger:
        for evt in ledger_events(args.ledger):
            print(json.dumps(evt, ensure_ascii=True))
        return
    if args.provider and args.model and args.mult:
        m = garbage_price_mult(args.provider, args.model)
        n = strikes_in_window(args.provider, args.model)
        print(f"{args.provider}/{args.model}: strikes={n} mult="
              f"{'inf' if m == float('inf') else f'{m:.2f}'}")
        return
    print(__doc__)


if __name__ == "__main__":
    main()
