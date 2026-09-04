#!/usr/bin/env python3
"""price_viz.py — Price/Quota visualization for the flat router.

Renders multiple visualization variants as PNG files and an ASCII summary.
Designed to be called by cron (hourly) or on demand via `python3 price_viz.py`.

Outputs to ~/.hermes/viz/ by default.
Served by zai_proxy at /viz/<name>.png.

Visualizations:
  V1: 2D price-vs-quota envelope curves (LOG/LINEAR toggle)
  V2: Price heatmap (time × provider, LogNorm)
  V3: Quota heatmap (time × provider, linear 0-100%)
  V7: ASCII block for Signal/terminal embedding
  V8b: Dynamic 2-panel headroom (token lanes + USD balance lanes)
  V9: Model-mix 7d stacked area (tokens by model)
  V10: Model x lane stacked bars (with $/M column)
  V11: ASCII insights strip (triggered-only suggestion engine)
  (V4 3D pressure surface retired — theoretical single-provider model, no
   measured data; message already covered by V1 y-axis.)

Usage:
  python3 price_viz.py [--linear] [--outdir DIR]
"""
from __future__ import annotations

import os
import sys
import json
import time
import sqlite3
import math
from pathlib import Path
from typing import Optional

import matplotlib
matplotlib.use("Agg")  # non-interactive backend
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import numpy as np

# ── Configuration ────────────────────────────────────────────────────────────

BOT_DIR = Path.home() / ".hermes" / "bot"
ZAI_USAGE_DB = BOT_DIR / "zai_usage.db"
API_BURN_DB = Path.home() / ".hermes" / "bot" / "api_burn.db"
DEFAULT_OUTDIR = Path.home() / ".hermes" / "viz"

# Provider tiers (mirrors flat_router.PROVIDER_TIER)
PROVIDER_TIER = {
    "ours": "quota",
    "friend": "quota",
    "neuralwatt": "balance",
    "opencode_go": "flat",
    "ollama_cloud": "included",
    "ollama_cloud_2": "included",
    "deepinfra": "per_token",
    "ppq": "per_token",
    "telnyx": "per_token",
    "openrouter": "per_token",
    "routstr": "per_token",
    "routstrd": "per_token",
}

# Quota pressure curve parameters (from pricing_engine.py)
QUOTA_PRESSURE_ONSET = float(os.environ.get("OLLAMA_QUOTA_PRESSURE_ONSET", "0.70"))
QUOTA_PRESSURE_ASYMPTOTE = float(os.environ.get("OLLAMA_QUOTA_PRESSURE_ASYMPTOTE", "1.5"))
MIN_EFFECTIVE_PRICE = 0.001  # $/M floor
_WEEKLY_S_FALLBACK = 7 * 86400  # rolling weekly duration when no anchor

# Seed rates for display (from flat_router._SEED_RATES)
SEED_RATES = {
    "ours": 0.068,
    "friend": 0.082,
    "ollama_cloud": 0.40,
    "ollama_cloud_2": 0.40,
    "opencode_go": 0.40,
    "neuralwatt": 2.21,
    "deepinfra": 1.30,
    "ppq": 0.80,
    "openrouter": 1.50,
    "telnyx": 5.40,
    "routstr": 1.00,
    "routstrd": 1.00,
}

# Color palette per provider
PROVIDER_COLORS = {
    "ours": "#1f77b4",
    "friend": "#17becf",
    "ollama_cloud": "#2ca02c",
    "ollama_cloud_2": "#90c3c4",
    "opencode_go": "#ff7f0e",
    "neuralwatt": "#d62728",
    "deepinfra": "#9467bd",
    "ppq": "#8c564b",
    "telnyx": "#e377c2",
    "openrouter": "#7f7f7f",
    "routstr": "#bcbd22",
    "routstrd": "#ff9896",
}

LOG_Y = True  # default: logarithmic y-axis


# ── Data layer ───────────────────────────────────────────────────────────────

def _connect_usage_db():
    return sqlite3.connect(f"file:{ZAI_USAGE_DB}?mode=ro", uri=True, timeout=5)


def _connect_api_burn_db():
    return sqlite3.connect(f"file:{API_BURN_DB}?mode=ro", uri=True, timeout=5)


def load_realized_prices(hours_back: int = 168) -> dict[str, list[tuple[float, float]]]:
    """Load realized effective prices from routing_profit.

    Returns {provider: [(ts, effective_price), ...]}.
    """
    db = _connect_usage_db()
    db.row_factory = sqlite3.Row
    now = time.time()
    cutoff = now - hours_back * 3600
    rows = db.execute(
        "SELECT ts, provider_used, effective_price FROM routing_profit "
        "WHERE ts > ? AND effective_price > 0 ORDER BY ts",
        (cutoff,)
    ).fetchall()
    db.close()
    result: dict[str, list[tuple[float, float]]] = {}
    for r in rows:
        p = r["provider_used"]
        if p not in result:
            result[p] = []
        result[p].append((r["ts"], r["effective_price"]))
    return result


def load_quota_series(hours_back: int = 48) -> dict[str, list[tuple[float, float]]]:
    """Load quota usage_fraction over time.

    Primary source: ``provider_balances`` table (balance-style providers like
    neuralwatt/openrouter/ppq/telnyx/routstr). Falls back to synthesizing a
    time-series from ``api_calls`` token sums for quota-tier providers
    (ours, friend, ollama_cloud, ollama_cloud_2, opencode_go) that don't write
    rows to provider_balances.

    Returns {provider: [(ts, usage_fraction), ...]}.
    """
    result: dict[str, list[tuple[float, float]]] = {}

    db = _connect_api_burn_db()
    db.row_factory = sqlite3.Row
    now = time.time()
    cutoff = now - hours_back * 3600

    # --- Primary: provider_balances ---
    try:
        rows = db.execute(
            "SELECT collected_at, provider, usage_fraction FROM provider_balances "
            "WHERE collected_at > ? ORDER BY collected_at",
            (cutoff,)
        ).fetchall()
        for r in rows:
            p = r["provider"]
            if p not in result:
                result[p] = []
            result[p].append((r["collected_at"], r["usage_fraction"]))
    except Exception:
        pass

    # --- Fallback: synthesize from api_calls for quota-tier providers ---
    # Limits come from LANE_REGISTRY_STATIC (single source of truth) via
    # _lane_limits — the hardcoded QUOTA_LIMITS dict is retired.
    registry = dict(LANE_REGISTRY_STATIC)
    for prov in ("ours", "friend", "ollama_cloud", "ollama_cloud_2", "opencode_go"):
        sess_limit, weekly_limit = _lane_limits(prov, registry)
        synth: list[tuple[float, float]] = []
        try:
            udb = _connect_usage_db()
            udb.row_factory = sqlite3.Row
            sess_w = 5 * 3600
            week_s = 7 * 86400
            n_buckets = min(hours_back, 48)
            for i in range(n_buckets):
                ts = cutoff + i * 3600
                sess_tok = udb.execute(
                    "SELECT COALESCE(SUM(total_tokens),0) AS t FROM api_calls "
                    "WHERE key_name=? AND ts > ? AND ts <= ?",
                    (prov, ts - sess_w, ts)
                ).fetchone()["t"]
                wk_tok = udb.execute(
                    "SELECT COALESCE(SUM(total_tokens),0) AS t FROM api_calls "
                    "WHERE key_name=? AND ts > ? AND ts <= ?",
                    (prov, ts - week_s, ts)
                ).fetchone()["t"]
                sess_pct = min(1.0, sess_tok / sess_limit) if sess_limit > 0 else 0.0
                wk_pct = min(1.0, wk_tok / weekly_limit) if weekly_limit > 0 else 0.0
                synth.append((ts, max(sess_pct, wk_pct)))
            udb.close()
        except Exception:
            pass
        if prov not in result:
            result[prov] = synth
        else:
            # Merge with balance-derived series (densify)
            result[prov].extend(synth)
            result[prov].sort(key=lambda x: x[0])
    db.close()
    return result


def load_current_quota_state() -> dict[str, float]:
    """Get current quota usage_pct per provider from api_calls token sums.

    For z.ai (ours/friend) anchors: weekly window starts at the provider's
    actual reset clock (from quota_clock registry), not rolling-7d.
    """
    # Lazy import quota_clock — optional, falls back to rolling.
    try:
        import sys as _sys
        _scrp = str(Path(__file__).resolve().parent / "src")
        if _scrp not in _sys.path:
            _sys.path.insert(0, _scrp)
        from quota_clock import window_start as _window_start
    except Exception:
        _window_start = lambda p, k, n: n - _WEEKLY_S_FALLBACK

    db = _connect_usage_db()
    db.row_factory = sqlite3.Row
    now = time.time()
    result = {}
    registry = dict(LANE_REGISTRY_STATIC)
    for provider in PROVIDER_TIER:
        if PROVIDER_TIER[provider] in ("quota", "flat", "included"):
            sess_start = now - 5 * 3600     # session stays rolling (no provider publishes a session anchor)
            weekly_start = _window_start(provider, "weekly", now)
            sess_tokens = db.execute(
                "SELECT COALESCE(SUM(total_tokens), 0) as t FROM api_calls "
                "WHERE key_name=? AND ts > ?", (provider, sess_start)
            ).fetchone()["t"]
            weekly_tokens = db.execute(
                "SELECT COALESCE(SUM(total_tokens), 0) as t FROM api_calls "
                "WHERE key_name=? AND ts > ?", (provider, weekly_start)
            ).fetchone()["t"]
            sess_limit, weekly_limit = _lane_limits(provider, registry)
            sess_pct = min(100.0, sess_tokens / sess_limit * 100) if sess_limit > 0 else 0
            weekly_pct = min(100.0, weekly_tokens / weekly_limit * 100) if weekly_limit > 0 else 0
            result[provider] = max(sess_pct, weekly_pct) / 100.0
        else:
            result[provider] = 0.0
    db.close()
    return result


def compute_theoretical_curve(provider: str, n_points: int = 200) -> tuple[np.ndarray, np.ndarray]:
    """Compute the theoretical price-vs-quota curve for a provider.

    Returns (usage_fractions, effective_prices).
    """
    tier = PROVIDER_TIER.get(provider, "per_token")
    base_rate = SEED_RATES.get(provider, 1.0)

    usage = np.linspace(0.0, 1.2, n_points)
    prices = np.zeros(n_points)

    if tier in ("quota",):
        # T1: MIN_EFFECTIVE_PRICE × max(0.0001, time_decay) × peak × health
        # Simplified: floor + quadratic ramp above onset
        prices = np.full(n_points, MIN_EFFECTIVE_PRICE)
        for i, u in enumerate(usage):
            if u > QUOTA_PRESSURE_ONSET:
                t = (u - QUOTA_PRESSURE_ONSET) / (1.0 - QUOTA_PRESSURE_ONSET)
                k = QUOTA_PRESSURE_ASYMPTOTE - 1.0
                factor = 1.0 + k * t / max(1e-6, 1.0 - t)
                if u >= 1.0:
                    factor = float("inf")
                prices[i] = MIN_EFFECTIVE_PRICE * factor
    elif tier in ("flat", "included"):
        # T3/T4: MIN_EFFECTIVE_PRICE + per-model pressure
        prices = np.full(n_points, MIN_EFFECTIVE_PRICE)
        for i, u in enumerate(usage):
            if u > QUOTA_PRESSURE_ONSET:
                t = (u - QUOTA_PRESSURE_ONSET) / (1.0 - QUOTA_PRESSURE_ONSET)
                k = QUOTA_PRESSURE_ASYMPTOTE - 1.0
                factor = 1.0 + k * t / max(1e-6, 1.0 - t)
                if u >= 1.0:
                    factor = QUOTA_PRESSURE_ASYMPTOTE  # cap for Ollama
                prices[i] = MIN_EFFECTIVE_PRICE * factor
    else:
        # T2/T5: base_rate (flat, no quota pressure)
        prices = np.full(n_points, base_rate)

    return usage, prices


# ── Renderers ────────────────────────────────────────────────────────────────

def render_envelope(outdir: Path, log_y: bool = True) -> Path:
    """V1: 2D price-vs-quota envelope curves for all providers."""
    fig, ax = plt.subplots(figsize=(12, 7))
    current_state = load_current_quota_state()

    for provider in sorted(PROVIDER_TIER, key=lambda p: SEED_RATES.get(p, 99)):
        usage, prices = compute_theoretical_curve(provider)
        color = PROVIDER_COLORS.get(provider, "#cccccc")
        label = provider
        ax.plot(usage * 100, prices, label=label, color=color, linewidth=1.5, alpha=0.8)

        # Mark current operating point
        cur_pct = current_state.get(provider, 0) * 100
        if cur_pct > 0:
            idx = min(int(cur_pct / 100 * len(usage)), len(usage) - 1)
            tier = PROVIDER_TIER.get(provider, "per_token")
            if tier in ("quota", "flat", "included"):
                # For quota-tier, the curve caps at asymptote
                # (MIN_EFFECTIVE_PRICE * QUOTA_PRESSURE_ASYMPTOTE),
                # not at SEED_RATES (which is the cold-start estimate).
                cur_price = min(prices[idx], MIN_EFFECTIVE_PRICE * QUOTA_PRESSURE_ASYMPTOTE) if not math.isinf(prices[idx]) else MIN_EFFECTIVE_PRICE * QUOTA_PRESSURE_ASYMPTOTE
            else:
                cur_price = prices[idx] if not math.isinf(prices[idx]) else SEED_RATES.get(provider, 1.0)
            ax.plot(cur_pct, cur_price, "o", color=color, markersize=6, zorder=5)

    ax.axvline(x=QUOTA_PRESSURE_ONSET * 100, color="gray", linestyle="--", alpha=0.5, label=f"onset ({QUOTA_PRESSURE_ONSET:.0%})")
    ax.axvline(x=100, color="red", linestyle="--", alpha=0.3, label="quota limit")

    ax.set_xlabel("Quota Usage (%)")
    ax.set_ylabel("Effective $/M tokens")
    ax.set_title("Price vs Quota — All Providers" + (" (log)" if log_y else " (linear)"))
    if log_y:
        ax.set_yscale("log")
        ax.set_ylim(bottom=MIN_EFFECTIVE_PRICE * 0.5, top=max(SEED_RATES.values()) * 3)
    ax.legend(loc="upper left", fontsize=7, ncol=2)
    ax.grid(True, alpha=0.3)
    ax.set_xlim(0, 120)

    outpath = outdir / "price-envelope.png"
    fig.savefig(outpath, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return outpath


def render_price_heatmap(outdir: Path) -> Path:
    """V2: Heatmap of realized effective prices over time (LogNorm color)."""
    prices_data = load_realized_prices(hours_back=168)
    if not prices_data:
        return None

    # Build matrix: providers × hourly buckets
    now = time.time()
    n_hours = 168
    providers = sorted(prices_data.keys())
    hour_bins = [now - (n_hours - i) * 3600 for i in range(n_hours)]

    matrix = np.full((len(providers), n_hours), np.nan)
    for i, prov in enumerate(providers):
        for ts, price in prices_data[prov]:
            hour_idx = int((ts - (now - n_hours * 3600)) / 3600)
            if 0 <= hour_idx < n_hours:
                if not np.isnan(matrix[i, hour_idx]):
                    matrix[i, hour_idx] = (matrix[i, hour_idx] + price) / 2
                else:
                    matrix[i, hour_idx] = price

    fig, ax = plt.subplots(figsize=(14, max(4, len(providers) * 0.4)))
    # Replace nan with a very small value for LogNorm
    display = np.copy(matrix)
    display[np.isnan(display)] = MIN_EFFECTIVE_PRICE * 0.5

    norm = mcolors.LogNorm(vmin=MIN_EFFECTIVE_PRICE, vmax=np.nanmax(matrix) if not np.all(np.isnan(matrix)) else 10)
    im = ax.imshow(display, aspect="auto", cmap="YlOrRd", norm=norm, interpolation="nearest")

    ax.set_yticks(range(len(providers)))
    ax.set_yticklabels(providers, fontsize=8)
    time_labels = [time.strftime("%m-%d", time.gmtime(hour_bins[i])) if i % 24 == 0 else "" for i in range(n_hours)]
    ax.set_xticks(range(0, n_hours, 24))
    ax.set_xticklabels([time.strftime("%m-%d", time.gmtime(hour_bins[i])) for i in range(0, n_hours, 24)], fontsize=7, rotation=45)
    ax.set_xlabel("Time (UTC)")
    ax.set_title("Realized Effective Price ($/M, log color) — Last 7 Days")
    plt.colorbar(im, ax=ax, label="$/M tokens (log)")

    outpath = outdir / "price-heatmap.png"
    fig.savefig(outpath, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return outpath


def render_quota_heatmap(outdir: Path) -> Path:
    """V3: Heatmap of quota usage_fraction over time (linear 0-100%)."""
    quota_data = load_quota_series(hours_back=48)
    if not quota_data:
        return None

    now = time.time()
    n_mins = 576  # 48h in 5-min buckets
    providers = sorted(quota_data.keys())
    bucket_bins = [now - (n_mins - i) * 300 for i in range(n_mins)]

    matrix = np.full((len(providers), n_mins), np.nan)
    for i, prov in enumerate(providers):
        for ts, frac in quota_data[prov]:
            min_idx = int((ts - (now - n_mins * 300)) / 300)
            if 0 <= min_idx < n_mins:
                matrix[i, min_idx] = frac * 100

    fig, ax = plt.subplots(figsize=(14, max(4, len(providers) * 0.4)))
    display = np.copy(matrix)
    display[np.isnan(display)] = 0

    im = ax.imshow(display, aspect="auto", cmap="RdYlGn_r", vmin=0, vmax=100, interpolation="nearest")

    ax.set_yticks(range(len(providers)))
    ax.set_yticklabels(providers, fontsize=8)
    ax.set_xticks(range(0, n_mins, 72))
    ax.set_xticklabels([time.strftime("%H:%M", time.gmtime(bucket_bins[i])) for i in range(0, n_mins, 72)], fontsize=7, rotation=45)
    ax.set_xlabel("Time (UTC, last 48h)")
    ax.set_title("Quota Usage (%) — Last 48h")
    plt.colorbar(im, ax=ax, label="Usage %")

    outpath = outdir / "quota-heatmap.png"
    fig.savefig(outpath, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return outpath


def render_ascii() -> str:
    """V7: ASCII block for Signal/terminal embedding.

    Includes a ``resets`` column showing hours-until-reset when an anchor is
    known (only z.ai publishes nextResetTime via quota_clock registry).
    """
    # Lazy import quota_clock — optional.
    try:
        import sys as _sys
        _scrp = str(Path(__file__).resolve().parent / "src")
        if _scrp not in _sys.path:
            _sys.path.insert(0, _scrp)
        from quota_clock import next_reset as _next_reset
    except Exception:
        _next_reset = lambda p, k="weekly": None

    current_state = load_current_quota_state()
    now = time.time()
    lines = ["📊 PRICE LANDSCAPE (current)", ""]
    lines.append(f"{'Provider':<16s} {'Tier':<10s} {'Quota%':>7s} {'$/M':>10s} {'Resets':>8s}")
    lines.append("-" * 58)

    for provider in sorted(PROVIDER_TIER, key=lambda p: SEED_RATES.get(p, 99)):
        tier = PROVIDER_TIER[provider]
        quota = current_state.get(provider, 0) * 100
        rate = SEED_RATES.get(provider, 1.0)

        if tier in ("quota", "flat", "included"):
            if quota > QUOTA_PRESSURE_ONSET * 100:
                effective = MIN_EFFECTIVE_PRICE * 2.5  # simplified
            else:
                effective = MIN_EFFECTIVE_PRICE
            tier_label = tier.upper()
        else:
            effective = rate
            tier_label = tier.upper()

        # Reset column — only for providers with known anchors
        reset_str = ""
        anchor_ts = _next_reset(provider, "weekly")
        if anchor_ts and anchor_ts > now:
            hrs = (anchor_ts - now) / 3600
            if hrs >= 24:
                reset_str = f"{hrs/24:.1f}d"
            else:
                reset_str = f"{hrs:.0f}h"

        lines.append(f"{provider:<16s} {tier_label:<10s} {quota:>6.1f}% ${effective:>8.4f}/M {reset_str:>8s}")

        # Bar
        bar_len = int(quota / 5)
        bar = "█" * bar_len + "░" * (20 - bar_len)
        lines.append(f"  {bar}")

    lines.append("")
    lines.append(f"onset={QUOTA_PRESSURE_ONSET:.0%}  asymptote={QUOTA_PRESSURE_ASYMPTOTE}x  floor=${MIN_EFFECTIVE_PRICE}/M")
    return "\n".join(lines)


# ── Main ─────────────────────────────────────────────────────────────────────

# ── V8b lane registry ────────────────────────────────────────────────────────
# Single source of truth mapping every provider lane -> (kind, capacity).
#   token: token caps (weekly token limit) — stacked in Panel A.
#   usd:   balance lanes (routstrd/telnyx/ppq/neuralwatt) — absolute USD in
#          Panel B, NEVER token-stacked.
#   flat:  flat/included lanes (opencode_go) — hatched band, never stacked.
# Capacity comes from the limits table (static weekly token caps); the live
# /quota payload overlays used_pct/remaining and absorbs any extra lanes it
# carries that aren't in the static registry.

# ``capacity`` is the weekly token limit for token kind lanes (single source
# of truth — replaces the old hardcoded QUOTA_LIMITS dict in the loaders).
# ``session_capacity`` is the rolling 5h session cap used by load_current_
# quota_state for the max(session, weekly) usage fraction. Flat lanes carry
# numeric caps too (they ARE token-capped at the same 3.5B/500M as the other
# z.ai general lanes) but are rendered as a hatched band, never stacked.
LANE_REGISTRY_STATIC = {
    "ours":           {"kind": "token", "capacity": 14_000_000,     "session_capacity": 2_000_000},
    "friend":         {"kind": "token", "capacity": 14_000_000,     "session_capacity": 2_000_000},
    "ollama_cloud":   {"kind": "token", "capacity": 3_500_000_000,  "session_capacity": 500_000_000},
    "ollama_cloud_2": {"kind": "token", "capacity": 3_500_000_000,  "session_capacity": 500_000_000},
    "opencode_go":    {"kind": "flat",  "capacity": 3_500_000_000,  "session_capacity": 500_000_000},
    "neuralwatt":     {"kind": "usd",   "capacity": None,           "session_capacity": None},
    "routstrd":       {"kind": "usd",   "capacity": None,           "session_capacity": None},
    "telnyx":         {"kind": "usd",   "capacity": None,           "session_capacity": None},
    "ppq":            {"kind": "usd",   "capacity": None,           "session_capacity": None},
}

# Fallbacks for a lane absent from the registry (shouldn't happen in practice).
_DEFAULT_WEEKLY_LIMIT = 3_500_000_000
_DEFAULT_SESSION_LIMIT = 500_000_000


def _lane_limits(lane: str, registry: dict) -> tuple[int, int]:
    """Return (session_limit, weekly_limit) for a lane from the registry.

    Single source of truth; falls back to the generic z.ai general-lane caps
    only when the lane is absent entirely (never for a registry present lane).
    """
    entry = registry.get(lane) or {}
    sess = entry.get("session_capacity") or _DEFAULT_SESSION_LIMIT
    wkly = entry.get("capacity") or _DEFAULT_WEEKLY_LIMIT
    return int(sess), int(wkly)

QUOTA_ENDPOINT = "http://localhost:9099/quota"


def _fetch_quota_payload() -> Optional[dict]:
    """Fetch the live /quota payload from the local proxy. None on failure."""
    try:
        import urllib.request
        with urllib.request.urlopen(QUOTA_ENDPOINT, timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def build_lane_registry(payload: Optional[dict]) -> dict:
    """Map every provider lane -> {kind, capacity}.

    Merges the static registry with any lanes the /quota payload carries, so
    extra lanes are absorbed automatically. Token capacities stay from the
    limits table (the payload's ``total`` is the session cap, not weekly).
    """
    reg = {lane: dict(entry) for lane, entry in LANE_REGISTRY_STATIC.items()}
    if payload:
        for lane, info in payload.items():
            if not isinstance(info, dict):
                continue
            if lane not in reg:
                # Unknown lane from payload — classify by shape.
                if "remaining_usd" in info or "total_credits_usd" in info:
                    reg[lane] = {"kind": "usd", "capacity": None, "session_capacity": None}
                elif info.get("regime") == "included":
                    reg[lane] = {"kind": "flat", "capacity": None, "session_capacity": None}
                else:
                    reg[lane] = {"kind": "token", "capacity": None, "session_capacity": None}
    return reg


def _rolling_window_sums(ts_arr, tok_arr, boundaries, window_s):
    """In-memory rolling window sums (numpy) — replaces per-hour SQL scans.

    ts_arr/tok_arr: parallel arrays of event timestamps and token counts.
    boundaries: list of window-end timestamps.
    Returns list of total tokens used in [b - window_s, b] for each b.
    """
    if len(ts_arr) == 0:
        return [0.0] * len(boundaries)
    order = np.argsort(ts_arr)
    ts_s = ts_arr[order]
    tok_s = tok_arr[order]
    prefix = np.concatenate(([0.0], np.cumsum(tok_s)))
    out = []
    for b in boundaries:
        i_end = int(np.searchsorted(ts_s, b, side="right"))
        i_start = int(np.searchsorted(ts_s, b - window_s, side="right"))
        out.append(float(prefix[i_end] - prefix[i_start]))
    return out


def _parse_usd_remaining(limit_remaining, raw_json):
    """Resolve a USD lane's remaining balance, reader-side.

    The api_burn collector writes ``limit_remaining=0.0`` for neuralwatt on
    every row (its kwh-included bucket is exhausted) while the real dollar
    balance lives in ``raw_json.remaining_usd``. When the mirror column is 0
    or None, fall back to parsing the raw_json payload (covers historical
    rows too — we do NOT patch the collector).

    Returns the best float balance, or None when nothing usable is present.
    """
    if limit_remaining is not None:
        try:
            rem = float(limit_remaining)
        except (TypeError, ValueError):
            rem = None
        if rem is not None and rem > 0:
            return rem
    # limit_remaining is 0 / None / unparseable → try raw_json.remaining_usd
    if raw_json:
        try:
            obj = json.loads(raw_json)
            rem = obj.get("remaining_usd")
            if rem is not None:
                return float(rem)
        except (ValueError, TypeError):
            return None
    return None


def _ffill_series(points, boundaries):
    """Forward-fill sparse (ts, value) points onto boundaries.

    Returns a list aligned to ``boundaries``; entries before the first point
    are None (no data yet).
    """
    vals = []
    idx = 0
    for b in boundaries:
        while idx < len(points) and points[idx][0] <= b:
            idx += 1
        vals.append(points[idx - 1][1] if idx > 0 else None)
    return vals


def render_headroom_weekly(outdir: Path) -> Path:
    """V8b: Dynamic 2-panel headroom over last 7 days.

    Panel A: token lanes remaining-headroom stacked area.
    Panel B: USD balance lanes as absolute USD (never stacked into tokens).
    Flat lanes (opencode_go) drawn as a hatched band, never stacked.

    Query fix: 1 query per pool (SELECT key_name, ts, total_tokens over the
    window) + numpy cumulative/rolling sums in memory — replaces the old
    168-bucket x N-pool per-hour SUM scan (the 1850-q bug).
    """
    payload = _fetch_quota_payload()
    registry = build_lane_registry(payload)

    now = time.time()
    n_hours = 168
    window_s = 7 * 86400
    hour_bins = [now - (n_hours - i) * 3600 for i in range(n_hours)]

    token_lanes = [l for l, e in registry.items() if e["kind"] == "token"]
    usd_lanes = [l for l, e in registry.items() if e["kind"] == "usd"]
    flat_lanes = [l for l, e in registry.items() if e["kind"] == "flat"]

    # ── Panel A data: token lanes (1 query per pool) ──
    token_series: dict[str, list[float]] = {}
    db = _connect_usage_db()
    db.row_factory = sqlite3.Row
    for lane in token_lanes:
        cap = registry[lane]["capacity"]
        if not cap:
            continue
        rows = db.execute(
            "SELECT ts, total_tokens FROM api_calls "
            "WHERE key_name=? AND ts > ? ORDER BY ts",
            (lane, now - window_s - 3600)
        ).fetchall()
        ts_arr = np.array([r["ts"] for r in rows], dtype=np.float64)
        tok_arr = np.array([r["total_tokens"] or 0 for r in rows], dtype=np.float64)
        used = _rolling_window_sums(ts_arr, tok_arr, hour_bins, window_s)
        token_series[lane] = [max(0.0, (cap - u) / 1e9) for u in used]

    # ── Panel B data: USD balance lanes (absolute USD) ──
    usd_series: dict[str, list[Optional[float]]] = {}
    burn_db = _connect_api_burn_db()
    burn_db.row_factory = sqlite3.Row
    for lane in usd_lanes:
        if lane == "telnyx":
            # Self-tracked: remaining = starting(10.0) - cumulative cost_usd.
            rows = db.execute(
                "SELECT ts, cost_usd FROM api_calls "
                "WHERE key_name=? AND cost_usd IS NOT NULL AND ts > ? ORDER BY ts",
                (lane, now - window_s - 3600)
            ).fetchall()
            spent = 0.0
            points = []
            for r in rows:
                spent += r["cost_usd"] or 0.0
                points.append((r["ts"], 10.0 - spent))
            usd_series[lane] = _ffill_series(points, hour_bins)
        else:
            rows = burn_db.execute(
                "SELECT collected_at, limit_remaining, raw_json FROM provider_balances "
                "WHERE provider=? AND collected_at > ? ORDER BY collected_at",
                (lane, now - window_s - 3600)
            ).fetchall()
            points = []
            for r in rows:
                try:
                    raw = r["raw_json"]
                except (KeyError, IndexError):
                    raw = None
                rem = _parse_usd_remaining(r["limit_remaining"], raw)
                if rem is not None:
                    points.append((r["collected_at"], rem))
            if not points and payload and isinstance(payload.get(lane), dict):
                # Fall back to a single live point from the /quota payload.
                rem = payload[lane].get("remaining_usd")
                if rem is not None:
                    points = [(now, rem)]
            usd_series[lane] = _ffill_series(points, hour_bins)
    db.close()
    burn_db.close()

    # ── Figure: two panels ──
    fig, (ax_a, ax_b) = plt.subplots(nrows=2, figsize=(14, 9), sharex=True)
    x = range(n_hours)

    # Panel A: token lanes stacked area
    bottom = np.zeros(n_hours)
    colors = ["#1f77b4", "#2ca02c", "#90c3c4", "#ff7f0e", "#d62728", "#9467bd"]
    ci = 0
    for lane in token_lanes:
        vals = token_series.get(lane)
        if vals is None:
            continue
        arr = np.array(vals)
        color = colors[ci % len(colors)]
        ci += 1
        ax_a.fill_between(x, bottom, bottom + arr, alpha=0.6, color=color, label=lane)
        ax_a.plot(x, bottom + arr, color=color, linewidth=0.8)
        bottom = bottom + arr

    # Flat lanes: hatched band, never stacked
    for lane in flat_lanes:
        ax_a.axhspan(0, max(float(bottom.max()), 1e-9), color="gray",
                     alpha=0.15, hatch="//", label=f"{lane} (flat/included)")

    ax_a.set_ylabel("Remaining Quota (B tokens)")
    ax_a.set_title("Weekly Quota Headroom — Token Lanes (stacked)")
    ax_a.legend(loc="upper right", fontsize=8)
    ax_a.grid(True, alpha=0.2)
    ax_a.set_ylim(bottom=0)

    # Panel B: USD balance lanes (absolute, never stacked)
    for lane in usd_lanes:
        series = usd_series.get(lane)
        if not series or all(v is None for v in series):
            continue
        arr = np.array([v if v is not None else np.nan for v in series], dtype=np.float64)
        ax_b.plot(x, arr, linewidth=1.5, label=lane)
    ax_b.set_ylabel("Balance (USD)")
    ax_b.set_title("USD Balance Lanes (absolute, never token-stacked)")
    ax_b.legend(loc="upper right", fontsize=8)
    ax_b.grid(True, alpha=0.2)

    xticks = range(0, n_hours, 24)
    ax_b.set_xticks(xticks)
    ax_b.set_xticklabels([time.strftime("%m-%d", time.gmtime(hour_bins[i])) for i in xticks], fontsize=8)
    ax_b.set_xlabel("Date (UTC, last 7 days)")
    ax_b.set_xlim(0, n_hours)

    outpath = outdir / "headroom-weekly.png"
    fig.savefig(outpath, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return outpath


# ── V9 model-mix + V10 model x lane ─────────────────────────────────────────
# Per-model usage visualization. The model column is rich (48h calls:
# glm-5.3=4966, dsv4-flash=3497, glm-5.2=1812, glm-4.5-flash=907, kimi-k3=313)
# but was previously invisible. V9 shows token-share evolution over 7d; V10
# shows who-uses-which-model-on-which-key-at-what-cost.

def _load_model_token_series(hours_back: int = 168, bucket_s: int = 86400):
    """Load per-model token totals bucketed over time.

    Returns (series, boundaries) where:
      series: {model: [tokens_per_bucket, ...]} aligned to boundaries
      boundaries: list of bucket-end timestamps (len = hours_back*3600/bucket_s)
    """
    now = time.time()
    cutoff = now - hours_back * 3600
    n_buckets = max(1, int(hours_back * 3600 / bucket_s))
    boundaries = [now - (n_buckets - i) * bucket_s for i in range(n_buckets)]

    db = _connect_usage_db()
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(
            "SELECT model, ts, total_tokens FROM api_calls "
            "WHERE ts > ? AND model IS NOT NULL AND model != '' ORDER BY ts",
            (cutoff,)
        ).fetchall()
    finally:
        db.close()

    series: dict[str, list[float]] = {}
    for r in rows:
        m = r["model"]
        tok = r["total_tokens"] or 0
        if m not in series:
            series[m] = [0.0] * n_buckets
        # Find the bucket this event falls into (last bucket = most recent).
        idx = int((r["ts"] - cutoff) // bucket_s)
        idx = min(max(idx, 0), n_buckets - 1)
        series[m][idx] += tok
    return series, boundaries


def render_model_mix(outdir: Path) -> Path:
    """V9: 7-day stacked area of TOKENS by model (share evolution).

    Answers "is glm-5.3 share growing". Each model is a stacked area; the
    top edge of each band shows that model's cumulative token share over the
    last 7 days. Filename model-mix-7d.png.
    """
    series, boundaries = _load_model_token_series(hours_back=168, bucket_s=86400)
    n_buckets = len(boundaries)
    x = range(n_buckets)

    fig, ax = plt.subplots(figsize=(14, 6))
    if series:
        # Order models by total tokens desc so the biggest sits at the bottom.
        ordered = sorted(series.items(), key=lambda kv: -sum(kv[1]))
        bottom = np.zeros(n_buckets)
        palette = ["#1f77b4", "#2ca02c", "#ff7f0e", "#d62728", "#9467bd",
                   "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"]
        for i, (model, vals) in enumerate(ordered):
            arr = np.array(vals, dtype=np.float64)
            color = palette[i % len(palette)]
            ax.fill_between(x, bottom, bottom + arr, alpha=0.6, color=color,
                            label=model)
            ax.plot(x, bottom + arr, color=color, linewidth=0.8)
            bottom = bottom + arr
        ax.set_ylim(bottom=0)
    else:
        ax.text(0.5, 0.5, "No model token data in last 7d",
                ha="center", va="center", transform=ax.transAxes)

    ax.set_ylabel("Tokens (7d, stacked)")
    ax.set_title("Model Mix — Token Share Evolution (last 7 days)")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(True, alpha=0.2)

    xticks = range(0, n_buckets, max(1, n_buckets // 7))
    ax.set_xticks(list(xticks))
    ax.set_xticklabels([time.strftime("%m-%d", time.gmtime(boundaries[i]))
                        for i in xticks], fontsize=8)
    ax.set_xlabel("Date (UTC, last 7 days)")
    ax.set_xlim(0, n_buckets - 1)

    outpath = outdir / "model-mix-7d.png"
    fig.savefig(outpath, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return outpath


def _load_model_lane_tokens(hours_back: int = 168) -> dict[str, dict[str, float]]:
    """Load per-model token volume split by lane (key_name).

    Returns {model: {lane: total_tokens}} over the window.
    """
    now = time.time()
    cutoff = now - hours_back * 3600
    db = _connect_usage_db()
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(
            "SELECT model, key_name, SUM(total_tokens) AS tok FROM api_calls "
            "WHERE ts > ? AND model IS NOT NULL AND model != '' "
            "AND key_name IS NOT NULL AND key_name != '' "
            "GROUP BY model, key_name",
            (cutoff,)
        ).fetchall()
    finally:
        db.close()
    result: dict[str, dict[str, float]] = {}
    for r in rows:
        result.setdefault(r["model"], {})[r["key_name"]] = r["tok"] or 0.0
    return result


def _load_model_realized_pm(hours_back: int = 168) -> dict[str, float]:
    """Load realized $/M per model from api_calls cost_usd.

    Returns {model: dollars_per_million} for models with positive cost.
    """
    now = time.time()
    cutoff = now - hours_back * 3600
    db = _connect_usage_db()
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(
            "SELECT model, SUM(total_tokens) AS tok, SUM(cost_usd) AS cost "
            "FROM api_calls WHERE ts > ? AND model IS NOT NULL AND model != '' "
            "AND cost_usd > 0 GROUP BY model",
            (cutoff,)
        ).fetchall()
    finally:
        db.close()
    result: dict[str, float] = {}
    for r in rows:
        tok = r["tok"] or 0
        cost = r["cost"] or 0.0
        if tok > 0:
            result[r["model"]] = cost / tok * 1e6
    return result


def render_model_by_lane(outdir: Path) -> Path:
    """V10: HORIZONTAL stacked bars per model, segments = lanes.

    Right column annotates realized $/M per model (from api_calls cost_usd).
    Answers who-uses-which-model-on-which-key-at-what-cost.
    Filename model-by-lane.png.
    """
    m2l = _load_model_lane_tokens(hours_back=168)
    realized_pm = _load_model_realized_pm(hours_back=168)

    # Collect all lanes, ordered by total volume desc.
    lane_totals: dict[str, float] = {}
    for model, lanes in m2l.items():
        for lane, tok in lanes.items():
            lane_totals[lane] = lane_totals.get(lane, 0.0) + tok
    lanes = sorted(lane_totals, key=lambda l: -lane_totals[l])

    # Order models by total tokens desc (biggest at top).
    models = sorted(m2l, key=lambda m: -sum(m2l[m].values()))

    fig, ax = plt.subplots(figsize=(14, max(4, 0.5 * len(models) + 2)))
    if models:
        palette = ["#1f77b4", "#2ca02c", "#ff7f0e", "#d62728", "#9467bd",
                   "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"]
        y_pos = range(len(models))
        for i, model in enumerate(models):
            left = 0.0
            for j, lane in enumerate(lanes):
                tok = m2l[model].get(lane, 0.0)
                if tok <= 0:
                    continue
                color = palette[j % len(palette)]
                ax.barh(i, tok, left=left, color=color, edgecolor="white",
                        linewidth=0.3, label=lane if i == 0 else None)
                left += tok
        ax.set_yticks(list(y_pos))
        ax.set_yticklabels(models, fontsize=9)
        # Annotate realized $/M on the right of each bar.
        for i, model in enumerate(models):
            total = sum(m2l[model].values())
            pm = realized_pm.get(model)
            if pm is not None:
                ax.text(total, i, f"  ${pm:.3f}/M", va="center", fontsize=8,
                        color="#333333")
        ax.set_xlim(0, max(sum(m2l[m].values()) for m in models) * 1.25)
    else:
        ax.text(0.5, 0.5, "No model x lane data in last 7d",
                ha="center", va="center", transform=ax.transAxes)

    ax.set_xlabel("Tokens (7d)")
    ax.set_title("Model x Lane — Token Volume by Key (realized $/M on right)")
    if lanes:
        ax.legend(loc="lower right", fontsize=8)
    ax.grid(True, axis="x", alpha=0.2)

    outpath = outdir / "model-by-lane.png"
    fig.savefig(outpath, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return outpath


# ── V11 ASCII insights strip ────────────────────────────────────────────────
# Triggered-only suggestion engine. render_insights() returns an ASCII strip
# that is EMPTY when no rule fires — it never fabricates a line.
#
# Prefix semantics:
#   ALERT   real money at risk (action needed)
#   EST     derived estimate / trend (labeled as such)
#   SUGGEST tunable waste (reweight / reclaim / tune)
#   NAG     repeated unaddressed issue (weekly dedupe)
#
# Caps:
#   hourly = red-only (ALERT) and <= 3 lines
#   daily  = <= 12 lines and ~1900 chars
#
# Schema-safety: every rule that touches optional columns (cache_hit,
# task_type, session_id, duration_ms) introspects PRAGMA table_info first and
# gracefully skips when the column is absent. cached_tokens is NOT in the live
# schema and is never assumed.

# Rule thresholds (all tunable via env; defaults documented inline).
# These are the 14 suggestion-engine thresholds from VIZ-P0B plus the two
# min-sample / share floor constants that keep rules false-positive damped.
INSIGHTS_MIN_CALLS = int(os.environ.get("VIZ_INSIGHTS_MIN_CALLS", "100"))
INSIGHTS_L1_IDLE_PCT = float(os.environ.get("VIZ_L1_IDLE_PCT", "0.05"))   # twin < 5% used
INSIGHTS_L1_ACTIVE_PCT = float(os.environ.get("VIZ_L1_ACTIVE_PCT", "0.20"))  # twin > 20% used
INSIGHTS_L2_EXHAUST_PCT = float(os.environ.get("VIZ_L2_EXHAUST_PCT", "0.90"))
INSIGHTS_L3_MIN_SAMPLES = int(os.environ.get("VIZ_L3_MIN_SAMPLES", "48"))
INSIGHTS_L3_DAYS = float(os.environ.get("VIZ_L3_DAYS", "14.0"))  # days-to-zero trigger
INSIGHTS_L4_ZOMBIE_H = float(os.environ.get("VIZ_L4_ZOMBIE_H", "72"))
INSIGHTS_C1_MIN_CALLS = int(os.environ.get("VIZ_C1_MIN_CALLS", "1000"))
INSIGHTS_C3_MULT = float(os.environ.get("VIZ_C3_MULT", "10.0"))
INSIGHTS_C3_MIN_SESSIONS = int(os.environ.get("VIZ_C3_MIN_SESSIONS", "10"))
INSIGHTS_M1_PP = float(os.environ.get("VIZ_M1_PP", "15.0"))
INSIGHTS_M1_MIN_SHARE = float(os.environ.get("VIZ_M1_MIN_SHARE", "0.05"))
INSIGHTS_M3_MIN_SHARE = float(os.environ.get("VIZ_M3_MIN_SHARE", "0.30"))  # >=30% traffic on locked lane
INSIGHTS_Q2_MULT = float(os.environ.get("VIZ_Q2_MULT", "2.0"))
INSIGHTS_Q2_MIN_CALLS = int(os.environ.get("VIZ_Q2_MIN_CALLS", "50"))
INSIGHTS_N1_NULL_PCT = float(os.environ.get("VIZ_N1_NULL_PCT", "0.90"))
INSIGHTS_E1_OVER_PCT = float(os.environ.get("VIZ_E1_OVER_PCT", "100.0"))


def _usage_columns() -> set:
    """Introspect api_calls columns (schema-safe). Empty set on failure."""
    try:
        db = _connect_usage_db()
        cols = {r[1] for r in db.execute("PRAGMA table_info(api_calls)").fetchall()}
        db.close()
        return cols
    except Exception:
        return set()


def _build_insights_ctx() -> dict:
    """Load live data into a ctx dict for the rule engine.

    Schema-safe: optional columns are only queried if present in PRAGMA.
    """
    cols = _usage_columns()
    now = time.time()
    h48 = now - 48 * 3600
    h7d = now - 7 * 86400
    h72 = now - 72 * 3600
    h14d = now - 14 * 86400

    ctx = {
        "now": now,
        "usage_columns": cols,
        "quota_state": load_current_quota_state(),
        "seed_rates": dict(SEED_RATES),
        "tiers": dict(PROVIDER_TIER),
        "lane_registry": dict(LANE_REGISTRY_STATIC),
        "quota_payload": _fetch_quota_payload() or {},
        "model_counts_48h": {},
        "model_counts_7d": {},
        "model_latency_48h": {},
        "lane_counts_48h": {},
        "lane_last_seen": {},
        "model_lane_48h": {},
        "session_stats": None,
        "balance_history": {},
        "cache_stats": None,
        "task_type_stats": None,
        "overall_avg_ms": None,
    }

    db = _connect_usage_db()
    db.row_factory = sqlite3.Row
    try:
        # Model counts (48h + 7d) and latency.
        rows = db.execute(
            "SELECT model, COUNT(*) c, AVG(duration_ms) avg_ms "
            "FROM api_calls WHERE ts > ? AND model IS NOT NULL AND model != '' "
            "GROUP BY model", (h48,)).fetchall()
        for r in rows:
            ctx["model_counts_48h"][r["model"]] = r["c"]
            if r["avg_ms"] is not None:
                ctx["model_latency_48h"][r["model"]] = (r["c"], r["avg_ms"])
        rows = db.execute(
            "SELECT model, COUNT(*) c FROM api_calls WHERE ts > ? "
            "AND model IS NOT NULL AND model != '' GROUP BY model", (h7d,)).fetchall()
        for r in rows:
            ctx["model_counts_7d"][r["model"]] = r["c"]

        # Lane counts + last-seen (48h) and model x lane.
        rows = db.execute(
            "SELECT key_name, model, COUNT(*) c, MAX(ts) last_ts "
            "FROM api_calls WHERE ts > ? AND key_name IS NOT NULL "
            "GROUP BY key_name, model", (h48,)).fetchall()
        for r in rows:
            lane = r["key_name"]
            ctx["lane_counts_48h"][lane] = ctx["lane_counts_48h"].get(lane, 0) + r["c"]
            ctx["lane_last_seen"][lane] = max(ctx["lane_last_seen"].get(lane, 0), r["last_ts"])
            if r["model"]:
                ctx["model_lane_48h"].setdefault(r["model"], {})[lane] = r["c"]

        # Overall avg latency (48h).
        row = db.execute(
            "SELECT AVG(duration_ms) a FROM api_calls WHERE ts > ? AND duration_ms IS NOT NULL",
            (h48,)).fetchone()
        if row and row["a"] is not None:
            ctx["overall_avg_ms"] = row["a"]

        # Cache stats — only if cache_hit column exists.
        if "cache_hit" in cols:
            row = db.execute(
                "SELECT COUNT(*) c, COALESCE(SUM(cache_hit),0) h FROM api_calls WHERE ts > ?",
                (h48,)).fetchone()
            if row:
                ctx["cache_stats"] = (row["c"], row["h"])

        # task_type stats — only if task_type column exists.
        if "task_type" in cols:
            row = db.execute(
                "SELECT COUNT(*) c, SUM(CASE WHEN task_type IS NULL OR task_type='' "
                "THEN 1 ELSE 0 END) n FROM api_calls WHERE ts > ?", (h48,)).fetchone()
            if row:
                ctx["task_type_stats"] = (row["c"], row["n"] or 0)

        # Session stats — only if session_id column exists.
        if "session_id" in cols:
            row = db.execute(
                "SELECT COUNT(*) n, AVG(cnt) avg_calls, MAX(cnt) max_calls FROM ("
                "SELECT session_id, COUNT(*) cnt FROM api_calls WHERE ts > ? "
                "AND session_id IS NOT NULL AND session_id != '' GROUP BY session_id)",
                (h48,)).fetchone()
            if row and row["n"]:
                ctx["session_stats"] = (row["n"], row["avg_calls"] or 0.0, row["max_calls"] or 0)
    finally:
        db.close()

    # Balance history (14d) for USD lanes — OLS days-to-zero. Also enrich
    # quota_state with balance-lane usage_fraction so L2 lane-exhaust catches
    # balance-tier providers (e.g. neuralwatt 99.5%) that load_current_quota_state
    # reports as 0.0.
    try:
        bdb = _connect_api_burn_db()
        bdb.row_factory = sqlite3.Row
        rows = bdb.execute(
            "SELECT provider, collected_at, limit_remaining, usage_fraction "
            "FROM provider_balances "
            "WHERE collected_at > ? AND limit_remaining IS NOT NULL ORDER BY collected_at",
            (h14d,)).fetchall()
        bdb.close()
        latest_frac = {}
        for r in rows:
            ctx["balance_history"].setdefault(r["provider"], []).append(
                (r["collected_at"], r["limit_remaining"]))
            # Only overlay usage_fraction if the sample is fresh (<=24h), so a
            # stale balance (e.g. ppq last seen 260h ago) can't trigger a false
            # lane-exhaust ALERT.
            if r["usage_fraction"] is not None and (now - r["collected_at"]) <= 24 * 3600:
                latest_frac[r["provider"]] = r["usage_fraction"]
        # Overlay balance-lane usage_fraction onto quota_state (only where the
        # static loader left 0.0, i.e. balance-tier lanes).
        for prov, frac in latest_frac.items():
            if ctx["quota_state"].get(prov, 0.0) == 0.0:
                ctx["quota_state"][prov] = frac
    except Exception:
        pass

    return ctx


def _ols_slope(points):
    """Least-squares slope of (ts, value) points. Returns (slope_per_s, n)."""
    if len(points) < 2:
        return None, 0
    xs = np.array([p[0] for p in points], dtype=np.float64)
    ys = np.array([p[1] for p in points], dtype=np.float64)
    xm = xs.mean()
    ym = ys.mean()
    denom = float(((xs - xm) ** 2).sum())
    if denom == 0:
        return None, len(points)
    slope = float(((xs - xm) * (ys - ym)).sum() / denom)
    return slope, len(points)


# ── Rules ───────────────────────────────────────────────────────────────────

def rule_l1_idle_twin(ctx) -> Optional[str]:
    """L1: idle twin — same-cost, same-tier lane nearly idle while its twin works.

    Twins are lanes sharing the same seed rate AND the same tier (e.g. two
    ``included`` lanes like ollama_cloud / ollama_cloud_2). A flat lane is not
    a twin of an included lane even if the seed rate matches.
    """
    tiers = ctx["tiers"]
    for lane, entry in ctx["lane_registry"].items():
        tier = tiers.get(lane)
        if tier not in ("included", "flat"):
            continue
        rate = ctx["seed_rates"].get(lane)
        if rate is None:
            continue
        twins = [l for l in ctx["lane_registry"]
                 if l != lane and tiers.get(l) == tier
                 and ctx["seed_rates"].get(l) == rate]
        if not twins:
            continue
        idle = ctx["quota_state"].get(lane, 0.0)
        for twin in twins:
            active = ctx["quota_state"].get(twin, 0.0)
            if idle < INSIGHTS_L1_IDLE_PCT and active > INSIGHTS_L1_ACTIVE_PCT:
                return (f"SUGGEST {lane} idle at {idle*100:.1f}% vs {twin} "
                        f"{active*100:.1f}% (same ${rate:.2f}/mo) — reweight or reclaim")
    return None


def rule_l2_lane_exhaust(ctx) -> Optional[str]:
    """L2: lane exhaust — a lane near/at its cap.

    Emits one ALERT per exhausted lane (multi-line), so e.g. both ours (100%)
    and neuralwatt (99.5%) surface rather than only the first in iteration order.
    """
    alerts = []
    for lane, frac in sorted(ctx["quota_state"].items(), key=lambda kv: -kv[1]):
        if frac >= INSIGHTS_L2_EXHAUST_PCT:
            alerts.append(f"ALERT {lane} at {frac*100:.1f}% used — near exhaust, "
                          f"reweight traffic off this lane")
    return "\n".join(alerts) if alerts else None


def rule_l3_days_to_zero_ols(ctx) -> Optional[str]:
    """L3: days-to-zero OLS projection on rolling window (USD balance lanes)."""
    for lane, hist in ctx["balance_history"].items():
        if len(hist) < INSIGHTS_L3_MIN_SAMPLES:
            continue
        slope, n = _ols_slope(hist)
        if slope is None or slope >= 0:
            continue  # not draining
        last = hist[-1][1]
        if last <= 0:
            continue
        days = last / (-slope * 86400)
        if days < 0:
            continue
        if days <= INSIGHTS_L3_DAYS:  # only surface when meaningful
            return (f"EST {lane} balance ${last:.2f} draining at ${-slope*86400:.2f}/day "
                    f"→ ~{days:.0f} days to zero (OLS, {n} samples)")
    return None


def rule_l4_zombie_key(ctx) -> Optional[str]:
    """L4: zombie key — lane carrying zero traffic for >72h."""
    now = ctx["now"]
    for lane, last_ts in ctx["lane_last_seen"].items():
        if ctx["lane_counts_48h"].get(lane, 0) == 0 and (now - last_ts) > INSIGHTS_L4_ZOMBIE_H * 3600:
            hrs = (now - last_ts) / 3600
            return (f"SUGGEST {lane} zombie — zero traffic for {hrs:.0f}h, "
                    f"reclaim or reweight")
    return None


def rule_c1_cache_worth(ctx) -> Optional[str]:
    """C1: cache-worth — zero cache hits over many calls."""
    if ctx["cache_stats"] is None:
        return None  # cache_hit column absent — graceful skip
    total, hits = ctx["cache_stats"]
    if total < INSIGHTS_C1_MIN_CALLS:
        return None
    if hits == 0:
        return (f"SUGGEST semantic cache 0 hits/{total} calls (48h) — "
                f"enable/tune or drop")
    return None


def rule_c3_runaway_session(ctx) -> Optional[str]:
    """C3: runaway session — single session consuming >> median."""
    if ctx["session_stats"] is None:
        return None
    n, avg, mx = ctx["session_stats"]
    if n < INSIGHTS_C3_MIN_SESSIONS or avg <= 0:
        return None
    if mx > INSIGHTS_C3_MULT * avg:
        return (f"SUGGEST runaway session — top session {mx} calls vs avg {avg:.0f} "
                f"({n} sessions, 48h)")
    return None


def rule_m1_mix_drift(ctx) -> Optional[str]:
    """M1: model-mix drift — share change WoW (7d vs 48h) beyond threshold."""
    if not ctx["model_counts_7d"] or not ctx["model_counts_48h"]:
        return None
    tot7 = sum(ctx["model_counts_7d"].values())
    tot48 = sum(ctx["model_counts_48h"].values())
    if tot7 <= 0 or tot48 <= 0:
        return None
    for model, c7 in ctx["model_counts_7d"].items():
        share7 = c7 / tot7
        if share7 < INSIGHTS_M1_MIN_SHARE:
            continue
        c48 = ctx["model_counts_48h"].get(model, 0)
        share48 = c48 / tot48
        delta_pp = (share48 - share7) * 100
        if abs(delta_pp) >= INSIGHTS_M1_PP:
            direction = "up" if delta_pp > 0 else "down"
            return (f"SUGGEST model-mix drift — {model} share {share7*100:.0f}%→"
                    f"{share48*100:.0f}% ({direction} {abs(delta_pp):.0f}pp WoW)")
    return None


def rule_m2_model_lane_mismatch(ctx) -> Optional[str]:
    """M2: expensive model on a paid lane while a flat lane sits idle."""
    # Find flat lanes with zero traffic.
    flat_idle = [l for l, e in ctx["lane_registry"].items()
                 if e.get("kind") == "flat" and ctx["lane_counts_48h"].get(l, 0) == 0]
    if not flat_idle:
        return None
    # Find paid/quota lanes carrying traffic.
    for model, lanes in ctx["model_lane_48h"].items():
        for lane, cnt in lanes.items():
            tier = ctx["tiers"].get(lane, "per_token")
            if tier in ("quota", "per_token") and cnt >= INSIGHTS_MIN_CALLS:
                return (f"SUGGEST {model} on {lane} (paid) while {flat_idle[0]} "
                        f"(flat) idle — consider reweight")
    return None


def rule_q2_latency_outlier(ctx) -> Optional[str]:
    """Q2: latency outlier — a model's avg latency >> overall avg."""
    if ctx["overall_avg_ms"] is None or ctx["overall_avg_ms"] <= 0:
        return None
    for model, (calls, avg_ms) in ctx["model_latency_48h"].items():
        if calls < INSIGHTS_Q2_MIN_CALLS:
            continue
        if avg_ms > INSIGHTS_Q2_MULT * ctx["overall_avg_ms"]:
            return (f"SUGGEST {model} avg {avg_ms/1000:.1f}s/call vs overall "
                    f"{ctx['overall_avg_ms']/1000:.1f}s — slow lane or giant context")
    return None


def rule_n1_task_type_nag(ctx) -> Optional[str]:
    """N1: task_type NAG — most calls lack task_type (weekly dedupe)."""
    if ctx["task_type_stats"] is None:
        return None  # task_type column absent — graceful skip
    total, null_calls = ctx["task_type_stats"]
    if total < INSIGHTS_MIN_CALLS:
        return None
    if null_calls / total >= INSIGHTS_N1_NULL_PCT:
        return (f"NAG {null_calls/total*100:.1f}% of calls lack task_type — "
                f"1-line router fix unlocks per-task cost")
    return None


def rule_e1_weekly_over_budget(ctx) -> Optional[str]:
    """E1: weekly over-budget projection from the /quota payload."""
    for lane, info in ctx["quota_payload"].items():
        if not isinstance(info, dict):
            continue
        for pred in info.get("predictions", []):
            if pred.get("window") != "weekly":
                continue
            if pred.get("will_exhaust") and pred.get("projected_total_pct", 0) >= INSIGHTS_E1_OVER_PCT:
                return (f"ALERT {lane} weekly projected {pred['projected_total_pct']:.0f}% "
                        f"of quota — over budget, reweight now")
    return None


def rule_m3_heavy_on_locked_lane(ctx) -> Optional[str]:
    """M3: heavy traffic on a locked / over-budget lane while a cheaper twin idles.

    Catches the "54% of traffic on 'ours' while z.ai quota probe inactive" case:
    a quota lane that is locked (weekly 100%) or over-budget still carries a
    large share of 48h traffic, while a same-tier twin (e.g. ollama_cloud) has
    headroom. Suggests reweighting off the exhausted lane.
    """
    # Identify locked / over-budget quota lanes from the payload.
    locked = set()
    for lane, info in ctx["quota_payload"].items():
        if not isinstance(info, dict):
            continue
        if info.get("locked") or info.get("locked_pct", 0) >= 100:
            locked.add(lane)
        for pred in info.get("predictions", []):
            if pred.get("window") == "weekly" and pred.get("will_exhaust"):
                locked.add(lane)
    if not locked:
        return None

    total = sum(ctx["lane_counts_48h"].values())
    if total <= 0:
        return None
    for lane in locked:
        cnt = ctx["lane_counts_48h"].get(lane, 0)
        share = cnt / total
        if share >= INSIGHTS_M3_MIN_SHARE:  # >= threshold share of traffic on an exhausted lane
            return (f"SUGGEST {share*100:.0f}% of 48h traffic on {lane} (locked/"
                    f"over-budget) — reweight to a lane with headroom")
    return None


# All rules, in display order. Each returns a line string or None.
_INSIGHT_RULES = [
    rule_l2_lane_exhaust,      # ALERT
    rule_e1_weekly_over_budget,  # ALERT
    rule_l3_days_to_zero_ols,  # EST
    rule_l1_idle_twin,         # SUGGEST
    rule_l4_zombie_key,        # SUGGEST
    rule_c1_cache_worth,       # SUGGEST
    rule_c3_runaway_session,   # SUGGEST
    rule_m1_mix_drift,         # SUGGEST
    rule_m2_model_lane_mismatch,  # SUGGEST
    rule_m3_heavy_on_locked_lane,  # SUGGEST
    rule_q2_latency_outlier,   # SUGGEST
    rule_n1_task_type_nag,     # NAG
]


def _load_nag_state(path) -> dict:
    path = Path(path)
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return {}


def _save_nag_state(path, state: dict):
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))
    except Exception:
        pass


def render_insights(mode: str = "daily", ctx: Optional[dict] = None,
                    nag_state_file: Optional[Path] = None) -> str:
    """V11: triggered-only ASCII insights strip.

    Returns an empty string when no rule fires. ``mode`` is "hourly" (red-only
    ALERT, <=3 lines) or "daily" (<=12 lines, ~1900 chars). NAG rules are
    deduped weekly via a persistent state file.
    """
    if ctx is None:
        ctx = _build_insights_ctx()

    if nag_state_file is None:
        nag_state_file = DEFAULT_OUTDIR / "insights-nag-state.json"

    lines = []
    for rule in _INSIGHT_RULES:
        try:
            out = rule(ctx)
        except Exception:
            continue  # a rule must never crash the strip
        if not out:
            continue
        # A rule may return multiple lines (e.g. L2 one ALERT per exhausted lane).
        for ln in out.split("\n"):
            if ln.strip():
                lines.append(ln)

    # NAG weekly dedupe.
    if nag_state_file is not None:
        state = _load_nag_state(nag_state_file)
        week = int(ctx["now"] // (7 * 86400))
        deduped = []
        for line in lines:
            if line.startswith("NAG"):
                key = line.split(" ", 1)[0] + ":" + line.split(" ", 1)[1][:40]
                if state.get(key) == week:
                    continue  # already nagged this week
                state[key] = week
                deduped.append(line)
            else:
                deduped.append(line)
        lines = deduped
        _save_nag_state(nag_state_file, state)

    if not lines:
        return ""

    if mode == "hourly":
        # Red-only (ALERT) and <= 3 lines.
        lines = [l for l in lines if l.startswith("ALERT")][:3]
    else:
        # Daily: <= 12 lines, ~1900 chars.
        lines = lines[:12]

    if not lines:
        return ""

    strip = "\n".join(lines)
    if len(strip) > 1900:
        strip = strip[:1900]
    return strip


def render_all(outdir: Path = None, log_y: bool = True) -> list[Path]:
    """Render all visualizations. Returns list of output file paths."""
    if outdir is None:
        outdir = DEFAULT_OUTDIR
    outdir.mkdir(parents=True, exist_ok=True)

    rendered = []
    try:
        p = render_envelope(outdir, log_y=log_y)
        rendered.append(p)
    except Exception as e:
        print(f"V1 envelope: {e}", file=sys.stderr)
    try:
        p = render_price_heatmap(outdir)
        if p:
            rendered.append(p)
    except Exception as e:
        print(f"V2 price heatmap: {e}", file=sys.stderr)
    try:
        p = render_quota_heatmap(outdir)
        if p:
            rendered.append(p)
    except Exception as e:
        print(f"V3 quota heatmap: {e}", file=sys.stderr)

    # ASCII always
    ascii_text = render_ascii()
    (outdir / "ascii-summary.txt").write_text(ascii_text)
    rendered.append(outdir / "ascii-summary.txt")

    # V11: ASCII insights strip — triggered-only, prepended to the digest.
    # Silent (empty) when no rule fires. Written to insights-strip.txt so the
    # sender can prepend it to the digest message.
    try:
        insights = render_insights(mode="daily")
        (outdir / "insights-strip.txt").write_text(insights)
        if insights:
            rendered.append(outdir / "insights-strip.txt")
    except Exception as e:
        print(f"V11 insights: {e}", file=sys.stderr)

    # B4 (provider-clock-alignment): shadow validation — log rolling vs
    # anchored weekly % for any provider with a known anchor. Hourly cron
    # → 48h of side-by-side data helps spot divergence >2 points.
    try:
        import sys as _sys
        _scrp = str(Path(__file__).resolve().parent / "src")
        if _scrp not in _sys.path:
            _sys.path.insert(0, _scrp)
        from quota_clock import next_reset as _qnext, window_start as _qws, _load_state as _qstate
        state = _qstate()
        if state:
            log_path = outdir / "shadow-comparison.jsonl"
            now = time.time()
            db = _connect_usage_db()
            entries = []
            for prov, entry in state.items():
                anchor = entry.get("weekly_anchor_ts")
                if not anchor:
                    continue
                wkly_limit = 14_000_000 if prov in ("ours", "friend") else 3_500_000_000
                anchored_start = _qws(prov, "weekly", now)
                rolling_start = now - 7 * 86400
                anchored_tok = db.execute(
                    "SELECT COALESCE(SUM(total_tokens),0) FROM api_calls WHERE key_name=? AND ts > ?",
                    (prov, anchored_start)).fetchone()[0]
                rolling_tok = db.execute(
                    "SELECT COALESCE(SUM(total_tokens),0) FROM api_calls WHERE key_name=? AND ts > ?",
                    (prov, rolling_start)).fetchone()[0]
                entries.append({
                    "ts": now,
                    "provider": prov,
                    "anchored_pct": min(100.0, anchored_tok / wkly_limit * 100),
                    "rolling_pct": min(100.0, rolling_tok / wkly_limit * 100),
                })
            db.close()
            if entries:
                with open(log_path, "a") as f:
                    for e in entries:
                        f.write(json.dumps(e) + "\n")
                # Cap at ~1000 lines
                if log_path.exists() and log_path.stat().st_size > 30_000:
                    lines = log_path.read_text().splitlines()
                    log_path.write_text("\n".join(lines[-500:]) + "\n")
    except Exception:
        pass

    # V8: headroom-weekly.png — total remaining quota over last 7d
    try:
        p = render_headroom_weekly(outdir)
        if p:
            rendered.append(p)
    except Exception as e:
        print(f"V8 headroom: {e}", file=sys.stderr)

    # V9: model-mix-7d.png — 7d stacked area of tokens by model (share evolution)
    try:
        p = render_model_mix(outdir)
        if p:
            rendered.append(p)
    except Exception as e:
        print(f"V9 model mix: {e}", file=sys.stderr)

    # V10: model-by-lane.png — horizontal stacked bars, model rows x lane cols,
    # with realized $/M annotation column.
    try:
        p = render_model_by_lane(outdir)
        if p:
            rendered.append(p)
    except Exception as e:
        print(f"V10 model by lane: {e}", file=sys.stderr)

    return rendered


if __name__ == "__main__":
    log_mode = "--linear" not in sys.argv
    outdir_arg = None
    for arg in sys.argv[1:]:
        if arg.startswith("--outdir="):
            outdir_arg = Path(arg.split("=", 1)[1])

    files = render_all(outdir=outdir_arg or DEFAULT_OUTDIR, log_y=log_mode)
    print(f"Rendered {len(files)} files:")
    for f in files:
        print(f"  {f}")
    print()
    print(render_ascii())
    print()
    insights = render_insights(mode="daily")
    if insights:
        print("── INSIGHTS ──")
        print(insights)