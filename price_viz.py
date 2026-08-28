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
  V4: 3D surface (session × weekly → price, per provider)
  V7: ASCII block for Signal/terminal embedding

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
    # Mirror load_current_quota_state's limits (2M/14M for ours+friend,
    # 500M/3.5B for general quota/included/flat providers).
    QUOTA_LIMITS = {
        "ours":           (2_000_000,    14_000_000),
        "friend":         (2_000_000,    14_000_000),
        "ollama_cloud":   (500_000_000, 3_500_000_000),
        "ollama_cloud_2": (500_000_000, 3_500_000_000),
        "opencode_go":    (500_000_000, 3_500_000_000),
    }
    for prov in ("ours", "friend", "ollama_cloud", "ollama_cloud_2", "opencode_go"):
        sess_limit, weekly_limit = QUOTA_LIMITS.get(prov, (5_000_000, 35_000_000))
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
            sess_limit = 500_000_000
            weekly_limit = 3_500_000_000
            if provider in ("ours", "friend"):
                sess_limit = 2_000_000
                weekly_limit = 14_000_000
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


def render_pressure_surface(outdir: Path, provider: str = "ollama_cloud", log_z: bool = True) -> Path:
    """V4: 3D surface — session quota × weekly quota → price for a single provider."""
    from mpl_toolkits.mplot3d import Axes3D

    sess_vals = np.linspace(0, 1.2, 80)
    weekly_vals = np.linspace(0, 1.2, 80)
    S, W = np.meshgrid(sess_vals, weekly_vals)
    Z = np.zeros_like(S)

    onset = QUOTA_PRESSURE_ONSET
    asymptote = QUOTA_PRESSURE_ASYMPTOTE
    k = asymptote - 1.0

    for i in range(len(sess_vals)):
        for j in range(len(weekly_vals)):
            s, w = S[i, j], W[i, j]
            if s >= 1.0 or w >= 1.0:
                Z[i, j] = MIN_EFFECTIVE_PRICE * asymptote
            elif s > onset and w > onset:
                ts = (s - onset) / (1.0 - onset)
                tw = (w - onset) / (1.0 - onset)
                fs = 1.0 + k * ts / max(1e-6, 1.0 - ts)
                fw = 1.0 + k * tw / max(1e-6, 1.0 - tw)
                Z[i, j] = MIN_EFFECTIVE_PRICE * fs * fw
            elif s > onset:
                ts = (s - onset) / (1.0 - onset)
                Z[i, j] = MIN_EFFECTIVE_PRICE * (1.0 + k * ts / max(1e-6, 1.0 - ts))
            elif w > onset:
                tw = (w - onset) / (1.0 - onset)
                Z[i, j] = MIN_EFFECTIVE_PRICE * (1.0 + k * tw / max(1e-6, 1.0 - tw))
            else:
                Z[i, j] = MIN_EFFECTIVE_PRICE

    fig = plt.figure(figsize=(10, 8))
    ax = fig.add_subplot(111, projection="3d")
    surf = ax.plot_surface(S * 100, W * 100, Z, cmap="YlOrRd", alpha=0.7,
                           linewidth=0, antialiased=True)
    ax.set_xlabel("Session Quota (%)")
    ax.set_ylabel("Weekly Quota (%)")
    ax.set_zlabel("$/M tokens")
    ax.set_title(f"Pressure Surface — {provider}" + (" (log z)" if log_z else " (linear z)"))
    if log_z:
        ax.set_zscale("log")
    fig.colorbar(surf, ax=ax, shrink=0.5, aspect=10, label="$/M")

    outpath = outdir / f"surface-{provider}.png"
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

def render_headroom_weekly(outdir: Path) -> Path:
    """V8: Total remaining quota headroom over last 7 days (stacked area)."""
    db = _connect_usage_db()
    db.row_factory = sqlite3.Row
    now = time.time()
    n_hours = 168
    pools = [
        ("ollama_cloud",   3_500_000_000, "#2ca02c"),
        ("ollama_cloud_2", 3_500_000_000, "#90c3c4"),
        ("ours",           14_000_000,    "#1f77b4"),
    ]
    hour_bins = [now - (n_hours - i) * 3600 for i in range(n_hours)]
    series = {}
    for pool_name, cap, _ in pools:
        vals = []
        for h in range(n_hours):
            t_end = now - (n_hours - h - 1) * 3600
            used = db.execute(
                "SELECT COALESCE(SUM(total_tokens),0) FROM api_calls "
                "WHERE key_name=? AND ts BETWEEN ? AND ?",
                (pool_name, t_end - 7*86400, t_end)
            ).fetchone()[0]
            vals.append(max(0, (cap - used) / 1e9))
        series[pool_name] = vals
    db.close()

    fig, ax = plt.subplots(figsize=(14, 5))
    x = range(n_hours)
    bottom = np.zeros(n_hours)
    colors = []
    labels = []
    for pool_name, _, color in pools:
        vals = series[pool_name]
        ax.fill_between(x, bottom, bottom + np.array(vals), alpha=0.6, color=color, label=pool_name)
        ax.plot(x, bottom + np.array(vals), color=color, linewidth=0.8)
        bottom = bottom + np.array(vals)

    ax.axhline(y=sum(bottom[-1:]), color="white", linestyle="--", alpha=0.3)
    total_vals = sum(np.array(series[p[0]]) for p in pools)
    ax.plot(x, total_vals, color="white", linewidth=2, alpha=0.7, label="Total")

    xticks = range(0, n_hours, 24)
    ax.set_xticks(xticks)
    ax.set_xticklabels([time.strftime("%m-%d", time.gmtime(hour_bins[i])) for i in xticks], fontsize=8)
    ax.set_xlabel("Date (UTC, last 7 days)")
    ax.set_ylabel("Remaining Quota (B tokens)")
    ax.set_title("Weekly Quota Headroom — Remaining vs Time")
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(True, alpha=0.2)
    ax.set_xlim(0, n_hours)
    ax.set_ylim(bottom=0)

    outpath = outdir / "headroom-weekly.png"
    fig.savefig(outpath, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return outpath


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
    try:
        p = render_pressure_surface(outdir, provider="ollama_cloud", log_z=log_y)
        rendered.append(p)
    except Exception as e:
        print(f"V4 surface: {e}", file=sys.stderr)

    # ASCII always
    ascii_text = render_ascii()
    (outdir / "ascii-summary.txt").write_text(ascii_text)
    rendered.append(outdir / "ascii-summary.txt")

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