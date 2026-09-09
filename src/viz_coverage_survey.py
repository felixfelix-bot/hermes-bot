#!/usr/bin/env python3
"""viz_coverage_survey.py — ensure every live LLM endpoint is represented in the viz.

Run on every 5th catalog-drift cron iteration (see catalog_drift_check.py). It:

  1. Builds the live endpoint universe from the single sources of truth:
       - flat_router.PROVIDER_MODELS / PROVIDER_TIER / _SEED_RATES (routed lanes)
       - the proxy /quota payload (subscription lanes, incl. un-wired ones)
       - real_price_tracker provider-name registries (known endpoints)
  2. Computes which of those are missing from the price_viz render tables.
  3. Derives tier/seed-rate/lane metadata for the missing endpoints and writes
     them into ~/.hermes/bot/viz_provider_overlay.json (read by price_viz.py at
     import — display color/linestyle are derived deterministically there).
  4. Re-renders the plots (fresh price_viz subprocess) and, on first addition,
     sends a signal alert summarizing what changed.

The overlay is additive and idempotent: running the survey again adds nothing
when the plots already cover the universe.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

HOME = Path.home()
BOT = HOME / ".hermes" / "bot"
SRC = BOT / "src"
OVERLAY_PATH = BOT / "viz_provider_overlay.json"
SURVEY_STATE_PATH = BOT / "viz_survey_state.json"
COUNTER_PATH = BOT / "viz_survey_counter.json"
QUOTA_ENDPOINT = "http://localhost:9099/quota"

# Run the survey on every Nth catalog-drift cron iteration (6-hourly), i.e.
# roughly every 30 hours.
EVERY_N_RUNS = 5

for _p in (str(BOT), str(SRC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Endpoints that are real registry names but not chat-facing lanes we want in
# the landscape (above-quota pricing concept, test keys, etc.).
IGNORE_PREFIXES = ("ollama_cloud_extra",)
IGNORE_SUFFIXES = ("_test",)

# Top-level /quota keys that are control metadata, not lanes.
_QUOTA_META_KEYS = {"active", "proactive_cooldown"}

# Tier fallback seed rates when nothing else is derivable (cosmetic: for
# quota/flat/included the ASCII price is the $0.001 floor regardless).
_TIER_DEFAULT_SEED = {"included": 0.40, "flat": 0.40, "quota": 0.068,
                      "balance": 1.0, "per_token": 1.0}

# Defaults for token lanes whose capacity we can't otherwise learn.
_DEFAULT_TOKEN_CAPACITY = 3_500_000_000
_DEFAULT_SESSION_CAPACITY = 500_000_000


def _fetch_quota() -> dict:
    try:
        with urllib.request.urlopen(QUOTA_ENDPOINT, timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return {}


def _ignored(name: str) -> bool:
    if name.startswith(IGNORE_PREFIXES):
        return True
    if name.endswith(IGNORE_SUFFIXES):
        return True
    return False


def _flat_router_tables() -> tuple[dict, dict]:
    """(PROVIDER_TIER, _SEED_RATES) from flat_router, or empty dicts."""
    try:
        import flat_router as fr
        return dict(getattr(fr, "PROVIDER_TIER", {})), \
            dict(getattr(fr, "_SEED_RATES", {}))
    except Exception:
        return {}, {}


def _real_price_rates() -> dict:
    """Provider-name -> last-resort $/M from real_price_tracker."""
    try:
        import real_price_tracker as rpt
        rates = {}
        rates.update(getattr(rpt, "LAST_RESORT_RATES", {}) or {})
        rates.update(getattr(rpt, "SEED_RATES", {}) or {})
        return rates
    except Exception:
        return {}


def live_universe() -> set[str]:
    """Union of every endpoint the router/proxy/price-tracker knows about."""
    universe: set[str] = set()
    tier, seed = _flat_router_tables()
    universe.update(tier.keys())
    universe.update(seed.keys())
    try:
        import flat_router as fr
        universe.update(getattr(fr, "PROVIDER_MODELS", {}).keys())
    except Exception:
        pass
    quota = _fetch_quota()
    for key, val in quota.items():
        if key in _QUOTA_META_KEYS:
            continue
        if isinstance(val, dict):
            universe.add(key)
    universe.update(_real_price_rates().keys())
    return {n for n in universe if n and not _ignored(n)}


def represented_endpoints() -> set[str]:
    """Endpoints already in the price_viz provider table OR the overlay.

    The overlay providers/lanes are merged at render time (not import), so the
    static table alone would under-report; union in the overlay so a previously
    added endpoint doesn't re-trigger the survey on every run.
    """
    reps: set[str] = set()
    try:
        import price_viz as pv
        reps.update(pv.PROVIDER_TIER.keys())
    except Exception:
        pass
    overlay = _load_overlay()
    reps.update((overlay.get("providers", {}) or {}).keys())
    reps.update((overlay.get("lanes", {}) or {}).keys())
    return reps


def _infer_tier(name: str, quota: dict) -> str | None:
    info = quota.get(name)
    if isinstance(info, dict):
        regime = info.get("regime")
        if regime in ("included", "exhausted") and isinstance(info.get("total"), (int, float)):
            return "included"
        if "is_exhausted" in info or isinstance(info.get("total"), float):
            return "balance"
    return None


def _twin_seed(name: str, tier: str) -> float | None:
    """Reuse the display seed of a same-tier sibling sharing the longest prefix."""
    try:
        import price_viz as pv
    except Exception:
        return None
    best, best_len = None, -1
    for other, rate in pv.SEED_RATES.items():
        if pv.PROVIDER_TIER.get(other) != tier:
            continue
        if name.startswith(other) and len(other) > best_len:
            best, best_len = rate, len(other)
    return best


def _derive_seed(name: str, tier: str, seed_table: dict) -> float:
    if name in seed_table:
        return float(seed_table[name])
    # Included/flat/quota tiers render at the $0.001 floor regardless of seed;
    # the seed only affects sort order, so reuse a same-tier sibling's display
    # seed to keep twins adjacent.
    if tier in ("included", "flat", "quota"):
        twin = _twin_seed(name, tier)
        if twin is not None:
            return float(twin)
        return float(_TIER_DEFAULT_SEED.get(tier, 1.0))
    rate = _real_price_rates().get(name)
    if rate is not None:
        return float(rate)
    return float(_TIER_DEFAULT_SEED.get(tier, 1.0))


def derive_metadata(name: str, tier_table: dict, seed_table: dict,
                    quota: dict) -> dict | None:
    """Return overlay metadata {tier, seed_rate, lane?} for a missing endpoint."""
    tier = tier_table.get(name) or _infer_tier(name, quota)
    if tier is None:
        return None
    seed_rate = _derive_seed(name, tier, seed_table)
    meta: dict = {"tier": tier, "seed_rate": seed_rate}

    lane = None
    info = quota.get(name)
    if isinstance(info, dict) and isinstance(info.get("total"), (int, float)):
        cap = info["total"]
        # opencode_go reports total=Infinity — don't turn that into a token cap.
        if cap != float("inf"):
            lane = {
                "kind": "token",
                "capacity": int(cap),
                "session_capacity": int(_DEFAULT_SESSION_CAPACITY),
            }
    elif tier == "balance":
        lane = {"kind": "usd", "capacity": None, "session_capacity": None}
    if lane is not None:
        meta["lane"] = lane
    return meta


def _load_overlay() -> dict:
    try:
        data = json.loads(OVERLAY_PATH.read_text())
    except Exception:
        return {"providers": {}, "lanes": {}}
    if not isinstance(data, dict):
        return {"providers": {}, "lanes": {}}
    data.setdefault("providers", {})
    data.setdefault("lanes", {})
    return data


def _persist_overlay(overlay: dict) -> None:
    OVERLAY_PATH.write_text(json.dumps(overlay, indent=2, sort_keys=True))


def _load_state() -> dict:
    try:
        return json.loads(SURVEY_STATE_PATH.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        SURVEY_STATE_PATH.write_text(json.dumps(state, indent=2))
    except Exception:
        pass


def _load_counter() -> dict:
    try:
        return json.loads(COUNTER_PATH.read_text())
    except Exception:
        return {}


def _save_counter(counter: dict) -> None:
    try:
        COUNTER_PATH.write_text(json.dumps(counter, indent=2))
    except Exception:
        pass


def run_if_due(force: bool = False, alert: bool = True) -> dict | None:
    """Increment the run counter; run the survey only on the Nth iteration.

    Returns the survey summary when it fired, else None. Never raises (so the
    catalog-drift job is unaffected by any survey failure).
    """
    try:
        counter = _load_counter()
        runs = int(counter.get("runs", 0)) + 1
        counter["runs"] = runs
        _save_counter(counter)
        if not force and runs % EVERY_N_RUNS != 0:
            return None
        return run(alert=alert)
    except Exception as e:
        print(f"[viz-survey] error: {e}", file=sys.stderr)
        return None


def render_now() -> None:
    """Re-render the plots so the merged overlay is reflected immediately."""
    python = sys.executable
    subprocess.run([python, str(BOT / "price_viz.py")],
                   capture_output=True, timeout=300)


def _send_alert(summary: dict) -> None:
    added = summary.get("new", [])
    if not added:
        return
    lines = ["🖼️ VIZ COVERAGE — endpoints added to plots"]
    for name in sorted(added):
        lines.append(f"  ➕ {name}")
    unres = summary.get("unrepresentable", [])
    for name, reason in unres:
        lines.append(f"  ⚠️ {name} (not addable: {reason})")
    try:
        subprocess.run(
            ["bash", str(BOT / "scripts" / "send-viz-signal.sh"),
             "--message", "\n".join(lines)],
            capture_output=True, timeout=90)
    except Exception:
        pass


def run(alert: bool = True, dry_run: bool = False) -> dict:
    tier_table, seed_table = _flat_router_tables()
    quota = _fetch_quota()
    universe = live_universe()
    represented = represented_endpoints()
    missing = sorted(u for u in universe - represented if not _ignored(u))

    overlay = _load_overlay()
    added: list[str] = []
    unrepresentable: list[tuple[str, str]] = []
    for name in missing:
        if name in overlay.get("providers", {}):
            added.append(name)  # already merged in overlay but not in table
            continue
        meta = derive_metadata(name, tier_table, seed_table, quota)
        if meta is None:
            unrepresentable.append((name, "no tier/seed derivable"))
            continue
        lane = meta.pop("lane", None)
        overlay["providers"][name] = meta
        if lane:
            overlay["lanes"][name] = lane
        added.append(name)

    state = _load_state()
    new_adds = [a for a in added if a not in state.get("added", [])]
    state["added"] = sorted(set(added))
    state["missing"] = sorted(missing)

    if added and not dry_run:
        _persist_overlay(overlay)
        render_now()
        _save_state(state)

    summary = {
        "universe": sorted(universe),
        "represented": sorted(represented),
        "missing": sorted(missing),
        "added": sorted(added),
        "new": sorted(new_adds),
        "unrepresentable": unrepresentable,
    }
    if alert and new_adds and not dry_run:
        _send_alert(summary)
    return summary


if __name__ == "__main__":
    result = run(alert=False, dry_run=("--dry-run" in sys.argv))
    print(json.dumps(result, indent=2))
