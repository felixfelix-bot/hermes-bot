#!/usr/bin/env python3
"""compression_model_router — dynamic compression summarizer model selection.

Selects the cheapest capable model for context compression summarization,
based on:
  - Available endpoints (health tracking via proxy state)
  - Kalman pressure (quota burn forecast → effective cost multiplier)
  - Base cost (per-model $/M token pricing)
  - Benchmarks (general/reasoning quality floor for summarization)
  - Context constraint (summarizer context >= session context)
  - Repeat-compression heuristic (after N>=2 compressions in a session,
    prefer the model with the lowest cached input rate)

Mirrors the model_tier_router.py hook pattern — imported by zai_proxy.py
as a parallel hook alongside _select_model_tier.

State: ~/.hermes/bot/compression_model_router_state.json
Input: ~/.hermes/bot/compression_threshold_override.json (governor output)
"""
from __future__ import annotations

import json
import time
from pathlib import Path

BOT_DIR = Path.home() / ".hermes" / "bot"
STATE_FILE = BOT_DIR / "compression_model_router_state.json"
GOVERNOR_FILE = BOT_DIR / "compression_threshold_override.json"

COMPRESSION_SENTINEL = "__compress__"
SESSION_COMPRESSION_THRESHOLD = 2

CANDIDATES = [
    {
        "model": "deepseek-v4-flash",
        "input_rate": 0.14,
        "cached_rate": 0.03,
        "output_rate": 0.28,
        "context_length": 1_048_576,
        "bench_score": 80,
        "role": "default",
    },
    {
        "model": "gemma-4-31b",
        "input_rate": 0.14,
        "cached_rate": 0.01,
        "output_rate": 0.42,
        "context_length": 262_144,
        "bench_score": 72,
        "role": "repeat_cached",
    },
]

MIN_BENCH_SCORE = 60
DEFAULT_BUDGET = 0.20


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"session_compression_counts": {}, "last_ts": 0}


def _save_state(state: dict):
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception:
        pass


def _load_governor_budget() -> float:
    if GOVERNOR_FILE.exists():
        try:
            data = json.loads(GOVERNOR_FILE.read_text())
            return float(data.get("compression_budget", DEFAULT_BUDGET))
        except Exception:
            pass
    return DEFAULT_BUDGET


def _get_session_compression_count(session_id: str | None, state: dict) -> int:
    if not session_id:
        return 0
    entry = state.get("session_compression_counts", {}).get(session_id)
    if isinstance(entry, dict):
        return entry.get("count", 0)
    return 0


def _increment_session_compression_count(session_id: str | None, state: dict):
    if not session_id:
        return
    counts = state.setdefault("session_compression_counts", {})
    now = time.time()
    entry = counts.get(session_id)
    current = entry.get("count", 0) if isinstance(entry, dict) else 0
    counts[session_id] = {"count": current + 1, "last_seen": now}
    cutoff = now - 3600
    stale = [k for k, v in list(counts.items())
             if isinstance(v, dict) and v.get("last_seen", 0) < cutoff]
    for k in stale:
        del counts[k]


def _get_kalman_pressure(session_id: str | None) -> float:
    proxy_state_path = BOT_DIR / "zai_proxy_state.json"
    if session_id:
        try:
            state = json.loads(proxy_state_path.read_text())
            for key_data in state.values():
                if isinstance(key_data, dict):
                    predictions = key_data.get("predictions", [])
                    for pred in predictions:
                        pct = pred.get("projected_pct")
                        if pct is not None and 0 < pct < 100:
                            return 1.0 + (pct / (100 - pct + 0.01))
        except Exception:
            pass
    return 1.0


def _score_candidate(candidate: dict, pressure: float, is_repeat: bool) -> float:
    bench = candidate["bench_score"]
    if bench < MIN_BENCH_SCORE:
        return -1.0

    if is_repeat:
        cost = candidate["cached_rate"]
    else:
        cost = candidate["input_rate"]

    effective_cost = cost * pressure
    if effective_cost <= 0:
        return float("inf")

    return bench / effective_cost


def select_compression_model(
    session_id: str | None = None,
    session_context_length: int = 131_072,
) -> dict:
    """Select the best compression summarizer model.

    Returns a dict with:
        model:          str — selected model name
        reason:         str — human-readable explanation
        effective_cost:float — base_cost × pressure
        budget:        float — governor-provided budget ceiling ($/M)
        is_repeat:     bool — this is a repeat compression in the session
        candidates:    list — all candidates considered (for logging)
    """
    state = _load_state()
    compression_count = _get_session_compression_count(session_id, state)
    is_repeat = compression_count >= SESSION_COMPRESSION_THRESHOLD

    budget = _load_governor_budget()
    pressure = _get_kalman_pressure(session_id)

    eligible = [
        c for c in CANDIDATES
        if c["context_length"] >= session_context_length
    ]

    if not eligible:
        return {
            "model": CANDIDATES[0]["model"],
            "reason": "no candidates pass context filter; using default",
            "effective_cost": CANDIDATES[0]["input_rate"] * pressure,
            "budget": budget,
            "is_repeat": is_repeat,
            "candidates": [],
        }

    scored = []
    for c in eligible:
        score = _score_candidate(c, pressure, is_repeat)
        if score > 0:
            effective_cost = (c["cached_rate"] if is_repeat else c["input_rate"]) * pressure
            scored.append({
                "model": c["model"],
                "score": round(score, 2),
                "effective_cost": round(effective_cost, 4),
                "context_length": c["context_length"],
                "role": c["role"],
            })

    scored.sort(key=lambda x: x["score"], reverse=True)

    if not scored:
        return {
            "model": CANDIDATES[0]["model"],
            "reason": "no candidates passed bench floor; using default",
            "effective_cost": CANDIDATES[0]["input_rate"] * pressure,
            "budget": budget,
            "is_repeat": is_repeat,
            "candidates": [],
        }

    best = scored[0]
    _increment_session_compression_count(session_id, state)
    state["last_ts"] = time.time()
    state["last_selected_model"] = best["model"]
    state["last_budget"] = budget
    state["last_pressure"] = round(pressure, 4)
    _save_state(state)

    repeat_tag = " (repeat: cached rate)" if is_repeat else ""
    tag = "OK" if best["effective_cost"] <= budget else "OVER_BUDGET"
    reason = (
        f"{best['model']}{repeat_tag} score={best['score']:.1f} "
        f"cost=${best['effective_cost']:.4f}/M budget=${budget:.2f}/M "
        f"pressure={pressure:.2f} [{tag}]"
    )

    return {
        "model": best["model"],
        "reason": reason,
        "effective_cost": best["effective_cost"],
        "budget": budget,
        "is_repeat": is_repeat,
        "candidates": scored,
    }


def is_compression_request(task_type: str | None, model: str | None) -> bool:
    """Check if a request is a compression call."""
    if task_type and task_type.strip().lower() == "compression":
        return True
    if model and model.strip() == COMPRESSION_SENTINEL:
        return True
    return False


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Compression Model Router")
    parser.add_argument("--session-id", type=str, default=None)
    parser.add_argument("--context-length", type=int, default=131072)
    args = parser.parse_args()

    result = select_compression_model(args.session_id, args.context_length)
    print(json.dumps(result, indent=2))
