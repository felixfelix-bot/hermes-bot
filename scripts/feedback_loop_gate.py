#!/usr/bin/env python3
"""feedback_loop_gate.py — Step 8c feedback-loop closure gate for provider onboarding.

Implements the exact probes added to the adding-api-key-to-live-router skill
(Step 8c, commit fbbd08a). This data-layer check makes provider onboarding FAIL
unless the Kalman feedback loop is provably closed, so no API key can ever be
added with inaccurate prices again.

A provider is onboarded only when the feedback loop is CLOSED:
  (A)     a live dispatch returns 200 AND api_calls rows carry a real,
          non-fallback, correctly-valued cost (kills the $1.0 catch-all and the
          $1.2654 inflated fallback — pitfalls 17/19/20);
  (A.lint) every *_RATES dict referenced in _get_provider_cost / _extract_cost
          is DEFINED at module scope AND covers every model the provider maps
          (kills the referenced-but-undefined NameError trap);
  (B)     the canonical _PROVIDER_MODEL_NAMES form is what is logged, and
          real_price_tracker.get_real_rate(provider) returns a finite float;
  (C)     a balance collector is present where a balance API exists (fresh,
          non-negative rows), or a documented verified-negative 404 probe for a
          no-balance-API provider (Chutes-class) — never an assumption;
  (D)     the seed wins traffic: >=1 ROUTED 200 AND a non-seed measurement in
          the trailing window (data-collection deadlock prevention);
  (E)     SSE cost extraction logs non-NULL cost on streaming rows.

This module is IMPORT-SAFE: it never imports zai_proxy (which would load keys /
start side effects). It reads the two sqlite DBs read-only and statically parses
a zai_proxy.py source file with the AST for the lint/reference probes. The one
safe runtime dependency it may import for measured rates is
src.real_price_tracker (a pure calculator — opens sqlite only).

Usage:
    python3 scripts/feedback_loop_gate.py --provider deepseek,ppq
    python3 scripts/feedback_loop_gate.py --provider chutes --no-balance chutes \
        --negative-proof chutes=gate/chutes-negative-probe.json
    python3 scripts/feedback_loop_gate.py --provider deepseek --usage-db /x/u.db

Exit code is 0 ONLY if every applicable probe is GREEN. A JSON evidence block
(probes, raw queries, results, timestamps) is written to gate/<provider>.json.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, asdict, field
from typing import Any

# ── Repo-root resolution ────────────────────────────────────────────────────
# The gate lives in <repo>/scripts/ but imports flat_router and src.* which sit
# at the repo root (sibling of scripts/). When run as `python3
# scripts/feedback_loop_gate.py`, sys.path[0] is scripts/ and those imports
# would fail silently — making _load_seed_rates() return {} and the reference
# fall back to rate-table list blends instead of the operator-set seed. Put the
# repo root on sys.path so seed resolution works identically from CLI and tests.
_BOT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BOT_ROOT not in sys.path:
    sys.path.insert(0, _BOT_ROOT)

# ── Tunables / constants ────────────────────────────────────────────────────

#: Production usage DB (api_calls) — the per-call real-cost ground truth.
DEFAULT_USAGE_DB: str = os.path.expanduser("~/.hermes/bot/zai_usage.db")

#: Production balance DB (balance_snapshots) — the optional wallet ground truth.
DEFAULT_BURN_DB: str = os.path.expanduser("~/.hermes/bot/api_burn.db")

#: Provenance source file that is statically linted.
DEFAULT_SOURCE: str = os.path.expanduser("~/.hermes/bot/zai_proxy.py")

#: Default output directory for the JSON evidence blocks.
DEFAULT_OUT_DIR: str = os.path.expanduser("~/.hermes/bot/gate")

#: Relative deviation tolerance for probe A (skill: "within ±50%").
RATE_TOLERANCE: float = 0.50

#: Blended 3:1 input:output ratio — mirrors zai_proxy._blended_rate.
BLENDED_INPUT_RATIO: float = 0.75
BLENDED_OUTPUT_RATIO: float = 0.25

#: cost_source values that mean "this is NOT a real measured/estimated cost".
#: `rate_derived_fallback` is the $1.0-ish catch-all; NULL means invisible burn.
FORBIDDEN_COST_SOURCES: set[str] = {"rate_derived_fallback"}

#: How many of the newest routed rows probe A examines (per-M vs reference).
PROBE_A_MAX_ROWS: int = 20

#: Freshness window for balance_snapshots (skill: "ts within ~10 min").
BALANCE_WINDOW_MIN: float = 10.0

#: Trailing window for the "seed wins traffic" / canonical checks.
TRAFFIC_WINDOW_HOURS: float = 168.0

#: Providers known to have an OpenAI-compatible balance API (ground-truth wallet
#: layer). Mirrors scripts/api_burn_collector.py:PROVIDERS + the collector set.
BALANCE_PROVIDERS: set[str] = {"ppq", "openrouter", "routstr", "deepseek"}

#: Providers with NO balance API (Chutes-class) — require a documented
#: verified-negative 404 probe rather than a balance row.
NO_BALANCE_PROVIDERS: set[str] = {"chutes"}

#: Providers known to stream (SSE). Probe E applies to these.
STREAMING_PROVIDERS: set[str] = {
    "deepseek", "neuralwatt", "telnyx", "chutes", "openrouter",
    "ollama_cloud", "ollama_cloud_2", "ollama_cloud_3", "ollama_cloud_4",
}


@dataclass
class Evidence:
    """One probe's verdict block, serialised into gate/<provider>.json.

    ``status`` is one of GREEN / RED / SKIP / NA:
      * GREEN  — the probe's closure condition provably holds;
      * RED    — the probe's closure condition provably fails (blocks ratify);
      * SKIP   — not applicable to this provider (e.g. E for a non-streamer) —
                 reported but does NOT block;
      * NA     — no data was available to judge — treated as RED for the
                 verdict because an unmeasured/unknown provider is untrusted.
    ``required`` marks whether a non-GREEN blocks the final verdict.
    """

    name: str
    status: str
    required: bool = True
    detail: str = ""
    data: Any = None
    query: str = ""
    ts: float = field(default_factory=time.time)


def _is_green(status: str) -> bool:
    return status == "GREEN"


def _finite_rate(val: Any) -> float | None:
    """Coerce to a finite non-negative float, else None."""
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f < 0:
        return None
    return f


def _open_ro(path: str) -> sqlite3.Connection:
    """Open a sqlite DB read-only (WAL-safe, no lock contention on writers)."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
    conn.row_factory = sqlite3.Row
    return conn


def _safe_query(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list:
    try:
        cur = conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()] or []
    except Exception:
        return []


# ── Static parsing of zai_proxy.py (AST) ─────────────────────────────────────
# These probes parse the source so the gate can lint it without executing it
# (never import zai_proxy — it loads keys and starts side effects).

_RATE_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*_RATES$")


def _parse_rate_tables(source: str) -> dict[str, dict[str, dict[str, float] | None]]:
    """Parse module-scope ``*_RATES`` dicts from zai_proxy.py source.

    Returns ``{table_name: {model_key: {field: number}}}``. Only literal
    ``"model": {..}`` sub-dicts with numeric leaves are captured; dynamic values
    (function calls, variables) are recorded with a ``None`` value so the lint
    knows the table exists but cannot statically confirm coverage.
    """
    import ast

    tables: dict[str, dict[str, dict[str, float] | None]] = {}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return tables
    for node in ast.walk(tree):
        # Handle both `NAME = {..}` (Assign) and `NAME: T = {..}` (AnnAssign).
        tgt = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            tgt = node.targets[0]
        elif isinstance(node, ast.AnnAssign) and node.target is not None:
            tgt = node.target
        if isinstance(tgt, ast.Name) and _RATE_TABLE_RE.match(tgt.id):
            tables[tgt.id] = _walk_table(node.value)
    return tables


def _walk_table(value_node) -> dict[str, dict[str, float] | None]:
    import ast

    out: dict[str, dict[str, float] | None] = {}
    if not isinstance(value_node, ast.Dict):
        return out
    for k, v in zip(value_node.keys, value_node.values):
        if not isinstance(k, ast.Constant):
            continue
        key = str(k.value)
        if isinstance(v, ast.Dict):
            inner: dict[str, float] = {}
            for ik, iv in zip(v.keys, v.values):
                if isinstance(ik, ast.Constant) and isinstance(iv, ast.Constant) \
                        and isinstance(iv.value, (int, float)):
                    inner[str(ik.value)] = float(iv.value)
            out[key] = inner
        else:
            out[key] = None  # dynamic value — can't statically confirm coverage
    return out


def _parse_provider_model_names(source: str) -> dict[str, dict[str, str]]:
    """Parse ``_PROVIDER_MODEL_NAMES`` from source -> {provider: {canonical: native}}."""
    import ast

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            tgt = node.targets[0]
            if isinstance(tgt, ast.Name) and tgt.id == "_PROVIDER_MODEL_NAMES":
                return _walk_strdictdict(node.value)
    return {}


def _walk_strdictdict(value_node) -> dict[str, dict[str, str]]:
    import ast

    out: dict[str, dict[str, str]] = {}
    if not isinstance(value_node, ast.Dict):
        return out
    for k, v in zip(value_node.keys, value_node.values):
        if not isinstance(k, ast.Constant):
            continue
        prov = str(k.value)
        if isinstance(v, ast.Dict):
            inner: dict[str, str] = {}
            for ik, iv in zip(v.keys, v.values):
                if isinstance(ik, ast.Constant) and isinstance(iv, ast.Constant):
                    inner[str(ik.value)] = str(iv.value)
            out[prov] = inner
    return out


def _blend_rate(rates: dict[str, float] | None) -> float | None:
    """3:1 input:output blended $/M from a rate table entry (as zai_proxy does)."""
    if not rates:
        return None
    in_r = _finite_rate(rates.get("input"))
    out_r = _finite_rate(rates.get("output"))
    if in_r is None or out_r is None:
        return None
    return BLENDED_INPUT_RATIO * in_r + BLENDED_OUTPUT_RATIO * out_r


def reference_rates_for_provider(
    provider: str,
    source_path: str,
    seed_rates: dict[str, float] | None = None,
) -> dict[str, float]:
    """Return ``{model_key: blended_$/M}`` reference rates for a provider.

    Probe A compares each row's ``cost_usd/total_tokens*1e6`` against the model's
    reference to catch the "$1.0 catch-all" / "$1.2654 inflated fallback".

    Reference priority (highest first):
      1. A provider **seed** (``flat_router._SEED_RATES`` /
         ``real_price_tracker.SEED_RATES``). The operator sets this to the REAL
         blended $/M from the platform dashboard (skill: DeepSeek $0.30->$0.05,
         Chutes $0.096), so it is the authoritative 'seeded/blended real rate'.
         For cache-heavy providers the list-price blend is far ABOVE the real
         cost (deepseek warm-context rows log ~$0.03-0.05/M vs list $0.175-1.50),
         so the list blend would false-positive a healthy loop. The seed avoids
         this and still catches the ~6-20x inflated catch-all.
      2. Per-model list blended rate from the provider's ``*_RATES`` table,
         when no seed is known.
      3. ``--reference-rate`` overrides are merged by the caller on top.
    Returns both canonical and native forms mapped to the same reference so probe A
    resolves whatever alias a row logged.
    """
    # Highest priority: the provider seed (real blended).
    seed = None
    if seed_rates and isinstance(seed_rates.get(provider), (int, float)):
        seed = float(seed_rates[provider])

    import ast

    try:
        with open(source_path, "r") as f:
            source = f.read()
    except OSError:
        return {"__provider__": seed} if seed is not None else {}

    tables = _parse_rate_tables(source)
    names = _parse_provider_model_names(source)

    # Build a per-alias -> blended-rate map from the rate tables.
    list_rates: dict[str, float] = {}
    canon = names.get(provider, {})  # canonical -> native
    native_vals = set(canon.values()) if canon else set()
    for tname, table in tables.items():
        if not isinstance(table, dict):
            continue
        named_for = provider.upper() in tname.upper().replace("_RATES", "")
        for model_key, entry in table.items():
            if not isinstance(entry, dict):
                continue
            if named_for or model_key in native_vals:
                br = _blend_rate(entry)
                if br is not None:
                    list_rates[model_key] = br
    # Map native keys back to canonical (what is logged).
    for cform, native in canon.items():
        if native in list_rates:
            list_rates[cform] = list_rates[native]

    result: dict[str, float] = {}
    if seed is not None:
        # Seed is authoritative: every row compares against the real blended rate.
        result["__provider__"] = seed
        for mkey in list_rates:
            result[mkey] = seed
        for cform in canon:
            result[cform] = seed
        for native in canon.values():
            result[native] = seed
    else:
        result = dict(list_rates)
    return result


def canonical_forms_for_provider(provider: str, source_path: str) -> set[str]:
    """Return the canonical model forms for a provider from _PROVIDER_MODEL_NAMES.

    The canonical forms are the *keys* of ``_PROVIDER_MODEL_NAMES[provider]``.
    If the provider is absent there, returns empty (the caller then requires the
    logged forms to match flat_router PROVIDER_MODELS instead).
    """
    try:
        with open(source_path, "r") as f:
            source = f.read()
    except OSError:
        return set()
    names = _parse_provider_model_names(source)
    return set(names.get(provider, {}).keys())


# ── Probe A — live dispatch 200 + correct cost ──────────────────────────────

def probe_live_dispatch_cost(
    provider: str,
    *,
    usage_db: str,
    reference_rates: dict[str, float] | None = None,
    _now: float | None = None,
    max_rows: int = PROBE_A_MAX_ROWS,
) -> Evidence:
    """(A) Live dispatch 200 + correct cost (NOT the $1.0 catch-all).

    Compares the **aggregate** token-weighted per-M over the recent routed 200
    rows against the provider's seeded/blended real rate. Using the aggregate
    (``SUM(cost_usd)/SUM(total_tokens)*1e6``) — rather than per-row — is
    deliberate: cache-hit rows legitimately log far below list price, and those
    average back to the real blended rate, so a healthy loop stays in-band. A
    *systematic* extraction failure (the $1.0 catch-all / $1.2654 inflated
    fallback applied to every row) inflates the aggregate 6-20x above the
    reference and is caught.

    GREEN iff:
      * >=1 ROUTED (status 200) row exists,
      * EVERY examined row has non-NULL cost_usd and a non-forbidden
        cost_source (no invisible burn / catch-all), AND
      * the aggregate per-M is within ``RATE_TOLERANCE`` (±50%) of the
        provider-level reference (seed first, else a known model reference).
    """
    now = _now if _now is not None else time.time()
    q = (
        "SELECT id, ts, key_name, model, total_tokens, cost_usd, cost_source, "
        "status_code FROM api_calls WHERE key_name=? AND status_code=200 "
        "ORDER BY id DESC LIMIT ?"
    )
    try:
        conn = _open_ro(usage_db)
        rows = _safe_query(conn, q, (provider, max_rows))
        conn.close()
    except Exception as exc:  # DB missing/locked -> unmeasured -> RED
        return Evidence(
            name="A", status="NA", required=True,
            detail=f"cannot open usage db {usage_db}: {exc}", query=q,
            ts=now,
        )

    if not rows:
        return Evidence(
            name="A", status="RED", required=True,
            detail=f"no routed (status=200) rows for provider '{provider}' — "
                   "no live dispatch evidence; cannot confirm the loop is closed",
            data=[], query=q, ts=now,
        )

    # 1. Every row must carry a real, non-fallback cost.
    bad: list[dict] = []
    sum_cost = 0.0
    sum_tokens = 0
    for r in rows:
        model = r.get("model")
        cost = _finite_rate(r.get("cost_usd"))
        source = r.get("cost_source")
        tokens = r.get("total_tokens") or 0
        if cost is not None and tokens > 0:
            sum_cost += cost
            sum_tokens += tokens
        reason = None
        if cost is None:
            reason = "cost_usd is NULL (invisible burn)"
        elif source in FORBIDDEN_COST_SOURCES or source is None:
            reason = f"cost_source={source!r} is a forbidden catch-all"
        if reason:
            bad.append({"id": r.get("id"), "model": model,
                        "cost_usd": r.get("cost_usd"), "cost_source": source,
                        "reason": reason})

    if bad:
        return Evidence(
            name="A", status="RED", required=True,
            detail=f"{len(bad)}/{len(rows)} routed rows fail the non-fallback "
                   f"cost check; first: {bad[0]['reason']}",
            data=bad, query=q, ts=now,
        )

    # 2. Aggregate token-weighted per-M vs the seeded/blended real rate.
    agg_per_m = (sum_cost / sum_tokens * 1e6) if sum_tokens > 0 else None
    ref = None
    if reference_rates:
        ref = reference_rates.get("__provider__")
        if ref is None:
            # No provider-level seed — use the first known model reference, or
            # the aggregate itself if nothing statically resolvable.
            ref = next((v for v in reference_rates.values() if v is not None), None)
    if ref is None:
        return Evidence(
            name="A", status="RED", required=True,
            detail=f"no seeded/blended reference rate known for '{provider}' — "
                   "cannot judge whether the logged cost is the $1.0 catch-all; "
                   "set the provider seed in flat_router._SEED_RATES or pass "
                   "--reference-rate <provider>=<rate>",
            data={"rows": [{"id": r["id"], "model": r["model"],
                            "cost_usd": r["cost_usd"],
                            "cost_source": r["cost_source"]} for r in rows]},
            query=q, ts=now,
        )
    if agg_per_m is None:
        return Evidence(
            name="A", status="RED", required=True,
            detail="routed rows have no usable cost/tokens aggregate — cannot "
                   "compute per-M", data={}, query=q, ts=now,
        )
    if abs(agg_per_m - ref) > RATE_TOLERANCE * ref:
        return Evidence(
            name="A", status="RED", required=True,
            detail=f"aggregate per-M ${agg_per_m:.4f} deviates >{RATE_TOLERANCE*100:.0f}% "
                   f"from the seeded/blended real rate ${ref:.4f} — this is the "
                   "'$1.0 catch-all' / '$1.2654 inflated fallback' signature "
                   "(pitfalls 17/19/20)",
            data={"aggregate_per_m": agg_per_m, "reference": ref,
                  "sum_cost_usd": sum_cost, "sum_total_tokens": sum_tokens,
                  "rows_checked": len(rows)},
            query=q, ts=now,
        )
    return Evidence(
        name="A", status="GREEN", required=True,
        detail=f"{len(rows)} routed 200 rows aggregate to ${agg_per_m:.4f}/M, "
               f"within ±{RATE_TOLERANCE*100:.0f}% of the seeded/blended real "
               f"rate ${ref:.4f} (loop is learning real prices)",
        data={"aggregate_per_m": agg_per_m, "reference": ref,
              "sum_cost_usd": sum_cost, "sum_total_tokens": sum_tokens,
              "rows_checked": len(rows)},
        query=q, ts=now,
    )


# ── Probe A.lint — rate table defined + fully keyed (NameError trap) ─────────

def probe_rate_table_lint(
    provider: str,
    *,
    source: str,
    reference_rates: dict[str, float] | None = None,
) -> Evidence:
    """(A.lint) Every referenced *_RATES table is defined AND covers mapped models.

    Walks _get_provider_cost / _extract_cost for ``*_RATES`` name references;
    every table referenced must be DEFINED at module scope, and the provider's
    mapped models must each have a resolvable rate (no KeyError/NameError on
    first run).
    """
    import ast

    try:
        with open(source, "r") as f:
            src = f.read()
    except OSError as exc:
        return Evidence(
            name="A.lint", status="RED", required=True,
            detail=f"cannot read source {source}: {exc}", query="ast parse", 
        )

    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return Evidence(
            name="A.lint", status="RED", required=True,
            detail=f"source is not parseable: {exc}", query="ast parse",
        )

    tables = _parse_rate_tables(src)
    names = _parse_provider_model_names(src)

    # 1. Collect every *_RATES name referenced inside the two cost functions.
    referenced: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef,)) and \
                node.name in ("_get_provider_cost", "_extract_cost", "_estimate_cost_usd"):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and _RATE_TABLE_RE.match(sub.id):
                    referenced.add(sub.id)
                elif isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) \
                        and _RATE_TABLE_RE.match(sub.value.id):
                    referenced.add(sub.value.id)

    unexplained: list[str] = []
    for tname in sorted(referenced):
        if tname not in tables:
            unexplained.append(f"{tname} referenced but UNDEFINED (NameError trap)")

    # 2. The provider's mapped models must each resolve to a rate.
    canon = names.get(provider, {})  # canonical -> native
    missing_models: list[str] = []
    if canon:
        for cform in canon:
            has_rate = False
            for tname, table in tables.items():
                if not isinstance(table, dict):
                    continue
                if cform in table or canon[cform] in table:
                    has_rate = True
                    break
            if not has_rate:
                missing_models.append(cform)

    if unexplained:
        return Evidence(
            name="A.lint", status="RED", required=True,
            detail="; ".join(unexplained),
            data={"referenced": sorted(referenced), "defined": sorted(tables)},
            query="ast walk of _get_provider_cost/_extract_cost",
        )
    if missing_models:
        return Evidence(
            name="A.lint", status="RED", required=True,
            detail=f"provider maps models with no resolvable rate: {missing_models} "
                   "(KeyError on first run)",
            data={"missing_models": missing_models,
                  "provider_maps": canon, "defined_tables": sorted(tables)},
            query="ast walk of _PROVIDER_MODEL_NAMES[provider] vs *_RATES coverage",
        )
    return Evidence(
        name="A.lint", status="GREEN", required=True,
        detail=f"all referenced rate tables defined and cover the provider's "
               f"{len(canon)} mapped models",
        data={"referenced": sorted(referenced), "defined": sorted(tables),
              "provider_maps": canon},
        query="ast parse of zai_proxy.py",
    )


# ── Probe B — canonical model form logged + real_price_tracker rate finite ───

def probe_canonical_model(
    provider: str,
    *,
    usage_db: str,
    canonical_forms: set[str],
    measured_rate: float | None,
    _now: float | None = None,
) -> Evidence:
    """(B) Every logged model is the canonical form; get_real_rate is finite.

    GREEN iff every distinct model logged for the provider in the trailing
    window is an element of ``canonical_forms`` (the _PROVIDER_MODEL_NAMES keys),
    AND ``measured_rate`` (real_price_tracker.get_real_rate) is a finite float —
    proving the aggregation key matches what is logged.
    """
    now = _now if _now is not None else time.time()
    since = now - TRAFFIC_WINDOW_HOURS * 3600.0
    q = ("SELECT model, COUNT(*) AS n FROM api_calls WHERE key_name=? AND ts>? "
         "GROUP BY model")
    try:
        conn = _open_ro(usage_db)
        rows = _safe_query(conn, q, (provider, since))
        conn.close()
    except Exception as exc:
        return Evidence(name="B", status="NA", required=True,
                        detail=f"cannot open usage db: {exc}", query=q, ts=now)

    if not rows:
        return Evidence(
            name="B", status="RED", required=True,
            detail=f"no models logged for '{provider}' in the trailing window — "
                   "cannot confirm canonical form; real_price_tracker can't match",
            data=[], query=q, ts=now,
        )

    rate = _finite_rate(measured_rate)
    bad_forms = [r["model"] for r in rows if r["model"] not in canonical_forms]
    problems: list[str] = []
    if bad_forms:
        problems.append(f"non-canonical model forms logged: {bad_forms} "
                        f"(expected one of {sorted(canonical_forms)})")
    if rate is None:
        problems.append("real_price_tracker.get_real_rate returned non-finite/None "
                        "(aggregation key mismatches logged form)")

    if problems:
        return Evidence(
            name="B", status="RED", required=True, detail="; ".join(problems),
            data={"logged": [r for r in rows],
                  "canonical_forms": sorted(canonical_forms),
                  "measured_rate": measured_rate},
            query=q, ts=now,
        )
    return Evidence(
        name="B", status="GREEN", required=True,
        detail=f"all {len(rows)} logged model forms are canonical and "
               f"get_real_rate={rate:.6f} is finite",
        data={"logged": [r for r in rows],
              "canonical_forms": sorted(canonical_forms),
              "measured_rate": rate},
        query=q, ts=now,
    )


# ── Probe C — balance collector where a balance API exists ──────────────────

def probe_balance(
    provider: str,
    *,
    burn_db: str,
    balance_providers: set[str],
    no_balance_providers: set[str],
    negative_proofs: list[str],
    _now: float | None = None,
    window_min: float = BALANCE_WINDOW_MIN,
) -> Evidence:
    """(C) Balance/usage collector where a balance API exists.

    For a **balance-API provider**: GREEN iff fresh (ts within ``window_min``)
    balance_snapshots rows exist with balance_usd >= 0.
    For a **no-balance-API provider (Chutes-class)**: GREEN iff a documented
    verified-negative 404 probe file is supplied — recorded as evidence, never
    assumed.
    Unknown classification -> SKIP (reported, does not block the verdict).
    """
    now = _now if _now is not None else time.time()

    if provider in balance_providers:
        q = ("SELECT provider, balance_usd, ts FROM balance_snapshots "
             "WHERE provider=? ORDER BY ts DESC LIMIT 5")
        try:
            conn = _open_ro(burn_db)
            rows = _safe_query(conn, q, (provider,))
            conn.close()
        except Exception as exc:
            return Evidence(name="C", status="NA", required=True,
                            detail=f"cannot open burn db: {exc}", query=q, ts=now)
        if not rows:
            return Evidence(
                name="C", status="RED", required=True,
                detail=f"balance API exists for '{provider}' but NO rows in "
                       "balance_snapshots — wallet ground-truth is missing "
                       "(collector not wired or not polling)",
                data=[], query=q, ts=now,
            )
        fresh = [r for r in rows if (now - r["ts"]) <= window_min * 60.0]
        nonneg = []
        for r in fresh:
            bal = _finite_rate(r["balance_usd"])
            if bal is not None and bal >= 0:
                nonneg.append(r)
        if not fresh:
            return Evidence(
                name="C", status="RED", required=True,
                detail=f"balance_snapshots rows for '{provider}' are stale "
                       f"(oldest {now - rows[0]['ts']:.0f}s > {window_min}min)",
                data=rows, query=q, ts=now,
            )
        if not nonneg:
            return Evidence(
                name="C", status="RED", required=True,
                detail="balance_snapshots rows exist but balance_usd is "
                       "negative/unknown (unfunded or broken collector)",
                data=rows, query=q, ts=now,
            )
        return Evidence(
            name="C", status="GREEN", required=True,
            detail=f"{len(nonneg)} fresh balance_snapshots rows with balance_usd>=0",
            data=rows, query=q, ts=now,
        )

    if provider in no_balance_providers:
        proven = []
        for p in negative_proofs:
            try:
                with open(p) as f:
                    blob = json.load(f)
                if blob.get("status") == 404 or blob.get("404"):
                    proven.append({"file": p, "detail": blob.get("detail", "")})
            except Exception:
                continue
        if proven:
            return Evidence(
                name="C", status="GREEN", required=True,
                detail="no-balance-API provider with a documented verified-negative "
                       f"404 probe: {proven}",
                data={"negative_proofs": proven}, query="", ts=now,
            )
        return Evidence(
            name="C", status="RED", required=True,
            detail=f"'{provider}' is a no-balance-API provider (Chutes-class) but "
                   "NO documented verified-negative 404 probe was attached — "
                   "the absence must be proven, never assumed",
            data={"provided_proofs": negative_proofs}, query="", ts=now,
        )

    return Evidence(
        name="C", status="SKIP", required=True,
        detail=f"provider '{provider}' classification unknown (not in "
               "balance_providers nor no_balance_providers) — report and ask, "
               "do not assume", query="", ts=now,
    )


# ── Probe D — seed wins traffic (data-collection deadlock prevention) ────────

def probe_seed_wins_traffic(
    provider: str,
    *,
    usage_db: str,
    measured_rate: float | None,
    window_hours: float = TRAFFIC_WINDOW_HOURS,
    _now: float | None = None,
) -> Evidence:
    """(D) >=1 ROUTED 200 AND a non-seed measurement in the trailing window.

    A seed only learns if it WINS traffic and produces a measured rate. Zero
    routed 200s, or traffic with no non-seed measurement (still running on the
    seed / NULL cost), means Kalman can never correct the seed -> deadlock.
    """
    now = _now if _now is not None else time.time()
    since = now - window_hours * 3600.0
    q = ("SELECT COUNT(*) AS routed200 FROM api_calls "
         "WHERE key_name=? AND status_code=200 AND ts>?")
    try:
        conn = _open_ro(usage_db)
        routed = _safe_query(conn, q, (provider, since)) or [{"routed200": 0}]
        conn.close()
        routed_cnt = routed[0].get("routed200") or 0
    except Exception as exc:
        return Evidence(name="D", status="NA", required=True,
                        detail=f"cannot open usage db: {exc}", query=q, ts=now)

    rate = _finite_rate(measured_rate)
    problems: list[str] = []
    if routed_cnt < 1:
        problems.append("seed won ZERO routed traffic in the trailing window "
                        "(deadlock — lower the seed until it beats the incumbent)")
    if rate is None:
        problems.append("no non-seed measurement in the trailing window "
                        "(Kalman never updated — still on seed)")

    if problems:
        return Evidence(
            name="D", status="RED", required=True, detail="; ".join(problems),
            data={"routed_200": routed_cnt, "measured_rate": measured_rate},
            query=q, ts=now,
        )
    return Evidence(
        name="D", status="GREEN", required=True,
        detail=f"{routed_cnt} routed 200s and a measured rate of ${rate:.6f}/M — "
               "the seed won traffic and Kalman measured it",
        data={"routed_200": routed_cnt, "measured_rate": rate}, query=q, ts=now,
    )


# ── Probe E — SSE cost extraction ────────────────────────────────────────────

def probe_sse_cost(
    provider: str,
    *,
    usage_db: str,
    streaming_providers: set[str],
    _now: float | None = None,
) -> Evidence:
    """(E) Streaming (SSE) requests log non-NULL cost (pitfall 17).

    SKIP (not required) for non-streaming providers. For streaming providers,
    GREEN iff the newest row (the SSE-extraction result) logs non-NULL cost_usd.
    """
    now = _now if _now is not None else time.time()
    if provider not in streaming_providers:
        return Evidence(
            name="E", status="SKIP", required=False,
            detail=f"provider '{provider}' is not classified as streaming — "
                   "SSE cost extraction probe not applicable", query="", ts=now,
        )
    q = ("SELECT cost_usd, cost_source FROM api_calls WHERE key_name=? "
         "ORDER BY id DESC LIMIT 1")
    try:
        conn = _open_ro(usage_db)
        rows = _safe_query(conn, q, (provider,))
        conn.close()
    except Exception as exc:
        return Evidence(name="E", status="NA", required=True,
                        detail=f"cannot open usage db: {exc}", query=q, ts=now)
    if not rows:
        return Evidence(
            name="E", status="RED", required=True,
            detail="streaming provider has no logged rows — cannot confirm SSE "
                   "cost extraction", data=[], query=q, ts=now,
        )
    cost = _finite_rate(rows[0].get("cost_usd"))
    if cost is None:
        return Evidence(
            name="E", status="RED", required=True,
            detail="newest streaming row logs NULL cost_usd — SSE extraction "
                   "crashed (json.loads on 'data:' chunks) or fell through to "
                   "unknown", data=rows[0], query=q, ts=now,
        )
    return Evidence(
        name="E", status="GREEN", required=True,
        detail=f"newest streaming row logs cost_usd=${cost:.6f} — SSE cost "
               "extraction worked",
        data=rows[0], query=q, ts=now,
    )


# ── Orchestration ────────────────────────────────────────────────────────────

def _load_seed_rates() -> dict[str, float]:
    """Best-effort seed $/M from flat_router / real_price_tracker (safe imports)."""
    seeds: dict[str, float] = {}
    for mod, attr in (("flat_router", "_SEED_RATES"),
                      ("src.real_price_tracker", "SEED_RATES")):
        try:
            m = __import__(mod, fromlist=[attr])
            d = getattr(m, attr, {})
            if isinstance(d, dict):
                seeds.update({k: v for k, v in d.items()
                              if isinstance(v, (int, float))})
        except Exception:
            continue
    return seeds


def _get_measured_rate(provider: str, usage_db: str, _now: float | None = None) \
        -> float | None:
    """real_price_tracker.get_real_rate(provider) for the given DB (safe import)."""
    try:
        from src.real_price_tracker import get_real_rate, clear_cache, DEFAULT_DB_PATH
        real_db = usage_db if usage_db != DEFAULT_USAGE_DB else DEFAULT_DB_PATH
        clear_cache()
        val = get_real_rate(provider, db_path=real_db, _now=_now)
        return _finite_rate(val)
    except Exception:
        return None


def run_gate(
    provider: str,
    *,
    usage_db: str = DEFAULT_USAGE_DB,
    burn_db: str = DEFAULT_BURN_DB,
    source_path: str = DEFAULT_SOURCE,
    out_dir: str | None = DEFAULT_OUT_DIR,
    balance_providers: set[str] | None = None,
    no_balance_providers: set[str] | None = None,
    streaming_providers: set[str] | None = None,
    negative_proofs: list[str] | None = None,
    reference_rates: dict[str, float] | None = None,
    seed_rates: dict[str, float] | None = None,
    measured_rate: float | None = None,
    _now: float | None = None,
) -> dict:
    """Run every applicable probe for ``provider`` and write gate/<provider>.json.

    Returns ``{"provider", "ok", "probes": {name: evidence}, "needed": [...]}``
    where ``ok`` is True only if ALL *required* probes are GREEN (SKIP/NA from a
    classification we cannot decide are surfaced as needed).
    """
    now = _now if _now is not None else time.time()
    bp = balance_providers if balance_providers is not None else BALANCE_PROVIDERS
    nbp = no_balance_providers if no_balance_providers is not None else \
        NO_BALANCE_PROVIDERS
    sp = streaming_providers if streaming_providers is not None else \
        STREAMING_PROVIDERS
    proofs = negative_proofs if negative_proofs is not None else []

    seeds = seed_rates if seed_rates is not None else _load_seed_rates()

    if measured_rate is not None:
        measured = _finite_rate(measured_rate)
    else:
        measured = _get_measured_rate(provider, usage_db, _now=now)

    if reference_rates is None:
        reference_rates = reference_rates_for_provider(
            provider, source_path, seed_rates=seeds)

    canon = canonical_forms_for_provider(provider, source_path)

    probes: dict[str, Evidence] = {}
    probes["A"] = probe_live_dispatch_cost(
        provider, usage_db=usage_db, reference_rates=reference_rates, _now=now)
    probes["A.lint"] = probe_rate_table_lint(
        provider, source=source_path, reference_rates=reference_rates)
    probes["B"] = probe_canonical_model(
        provider, usage_db=usage_db, canonical_forms=canon,
        measured_rate=measured, _now=now)
    probes["C"] = probe_balance(
        provider, burn_db=burn_db, balance_providers=bp,
        no_balance_providers=nbp, negative_proofs=proofs, _now=now)
    probes["D"] = probe_seed_wins_traffic(
        provider, usage_db=usage_db, measured_rate=measured, _now=now)
    probes["E"] = probe_sse_cost(
        provider, usage_db=usage_db, streaming_providers=sp, _now=now)

    # Verdict: ok iff every REQUIRED probe is GREEN. NA is treated as RED
    # (unmeasured == untrusted); SKIP is excluded from the blocking set.
    needed = []
    for name, ev in probes.items():
        if ev.required and ev.status != "GREEN":
            needed.append(name)
    ok = not needed

    result = {
        "provider": provider,
        "ok": ok,
        "ts": now,
        "generated_by": f"feedback_loop_gate v{__version__}",
        "probes": {name: asdict(ev) for name, ev in probes.items()},
        "needed_if_green_blocked": needed,
    }

    if out_dir:
        try:
            os.makedirs(out_dir, exist_ok=True)
            with open(os.path.join(out_dir, f"{provider}.json"), "w") as f:
                json.dump(result, f, indent=2, default=str)
        except OSError as exc:
            result["_gate_json_write_error"] = str(exc)
    return result


__version__ = "1.0.0"


def _parse_ref_rate_override(specs: list[str]) -> dict[str, float]:
    """Parse --reference-rate 'acme=0.25' style overrides."""
    out: dict[str, float] = {}
    for s in specs or []:
        if "=" in s:
            k, v = s.split("=", 1)
            try:
                out[k] = float(v)
            except ValueError:
                pass
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Step 8c feedback-loop closure gate — exit 0 only if every "
                    "applicable probe is GREEN.")
    ap.add_argument("--provider", required=True,
                    help="comma-separated provider names to gate")
    ap.add_argument("--usage-db", default=DEFAULT_USAGE_DB)
    ap.add_argument("--burn-db", default=DEFAULT_BURN_DB)
    ap.add_argument("--source", default=DEFAULT_SOURCE,
                    help="path to zai_proxy.py source to lint")
    ap.add_argument("--out", default=DEFAULT_OUT_DIR,
                    help="output dir for gate/<provider>.json")
    ap.add_argument("--no-balance", action="append", default=[],
                    help="provider has NO balance API (Chutes-class); requires "
                         "a verified-negative 404 proof")
    ap.add_argument("--negative-proof", action="append", default=[],
                    help="path to a documented verified-negative 404 probe JSON "
                         "(e.g. acme=gate/acme-negative.json or bare path)")
    ap.add_argument("--streams", action="append", default=[],
                    help="extra provider classified as streaming")
    ap.add_argument("--reference-rate", action="append", default=[],
                    help="provider-level reference $/M override, e.g. acme=0.25")
    ap.add_argument("--json", action="store_true",
                    help="print the full JSON evidence block to stdout")
    args = ap.parse_args(argv)

    providers = [p.strip() for p in args.provider.split(",") if p.strip()]
    if not providers:
        print("error: --provider is required", file=sys.stderr)
        return 2

    bp = set(BALANCE_PROVIDERS)
    nbp = set(NO_BALANCE_PROVIDERS) | set(args.no_balance)
    sp = set(STREAMING_PROVIDERS) | set(args.streams)
    ref_override = _parse_ref_rate_override(args.reference_rate)
    seeds = _load_seed_rates()
    seeds.update(ref_override)

    all_ok = True
    for prov in providers:
        res = run_gate(
            prov, usage_db=args.usage_db, burn_db=args.burn_db,
            source_path=args.source, out_dir=args.out,
            balance_providers=bp, no_balance_providers=nbp,
            streaming_providers=sp,
            negative_proofs=args.negative_proof,
            seed_rates=seeds,
        )
        status = "PASS" if res["ok"] else "FAIL"
        all_ok = all_ok and res["ok"]
        print(f"[feedback-loop-gate] {prov}: {status}")
        for name, ev in res["probes"].items():
            print(f"  {name:6s} {ev['status']:<5s} req={int(ev['required'])} "
                  f"- {ev['detail']}")
        if args.json:
            print(json.dumps(res, indent=2, default=str))

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
