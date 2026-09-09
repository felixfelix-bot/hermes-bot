#!/usr/bin/env python3
"""scripts/model_promote.py — INTAKE-3 human promotion CLI.

Manages the model_intake.json promotion pipeline. Human-batched, gated
promotion of eligible (probed, >=2 healthy providers) models into the flat
routing overlay + /v1/models advertisement.

Commands:
  list                    human digest: eligible batch + provider breadth +
                          measured-price status + already-promoted entries
  apply <canonical>       promote an eligible model -> status=promoted_routing;
                          recomputes the advertised flag (tier wall + measured
                          price gate)
  deny <canonical>        permanently reject a model -> status=rejected
                          (removes it from any overlay / advertisement)

This module only WRITES model_intake.json (the store). It does NOT edit any
runtime source. Promotion takes effect at import time of flat_router.py and
via refresh_intake_overlay() (proxy manager restarts/reloads after this).

Run from the repo root:  python3 scripts/model_promote.py list
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Repo root is two levels up from scripts/ (scripts/<this file>).
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
INTAKE_FILE = Path(__file__).resolve().parent.parent / "model_intake.json"

# Reuse tier-wall + measured-price gate from catalog_drift_check.
import catalog_drift_check as cdc  # noqa: E402


def _load() -> dict:
    if INTAKE_FILE.exists():
        try:
            return json.loads(INTAKE_FILE.read_text())
        except Exception:
            pass
    return {}


def _save(store: dict) -> None:
    INTAKE_FILE.write_text(json.dumps(store, indent=2))


def _provider_breadth(rec: dict) -> int:
    return len(rec.get("raw_ids", {}) or {})


def _healthy_providers() -> set:
    """Return the set of currently-healthy providers (best-effort)."""
    healthy = set()
    try:
        import flat_router as fr
        for name in fr.PROVIDER_MODELS:
            try:
                if fr._is_provider_healthy(name):
                    healthy.add(name)
            except Exception:
                pass
    except Exception:
        pass
    return healthy


def _extract_measured_model_names(got: dict) -> set:
    """Flatten the real_price_tracker nested shape into a set of measured model
    names.

    ``get_all_trailing_rates_per_model()`` returns ``{provider: {model: $/M,
    '_default': $/M}}``. Advertising requires the canonical to be IN the
    measured set, so we collect the *model* keys (dropping ``_default``), never
    the provider names.
    """
    return {
        model
        for prov in (got or {}).values()
        if isinstance(prov, dict)
        for model in prov
        if model != "_default"
    }


def _measured_models() -> set:
    """Best-effort set of models with a measured price (n>=50 real data)."""
    measured = set()
    try:
        import real_price_tracker as rpt
        got = rpt.get_all_trailing_rates_per_model()
        if isinstance(got, dict):
            measured = _extract_measured_model_names(got)
    except Exception:
        pass
    return measured


def _print_list(store: dict) -> int:
    now = datetime.now(timezone.utc).isoformat()
    healthy = _healthy_providers()
    measured = _measured_models()
    # recompute advertised flags over a fresh in-memory copy (no persist yet)
    live = json.loads(json.dumps(store))
    live = cdc.refresh_advertised_flags(live, healthy_providers=healthy,
                                        measured_models=measured, now_iso=now)

    eligible = []
    promoted = []
    for mid, g in live.items():
        if (g.get("status") == "eligible"
                and g.get("modality") == "chat"):
            eligible.append(mid)
        elif g.get("status") == "promoted_routing":
            promoted.append(mid)

    print("=== MODEL INTAKE — PROMOTION DIGEST ===")
    print(f"healthy_providers: {sorted(healthy)}")
    print(f"measured_models:   {sorted(measured) if measured else '(none)'}")
    print()
    print(f"ELIGIBLE for promotion ({len(eligible)}):")
    if not eligible:
        print("  (none)")
    for mid in sorted(eligible):
        g = live[mid]
        breadth = _provider_breadth(g)
        mstatus = "MEASURED" if mid in measured else "unmeasured(never advertises)"
        print(f"  {mid:50s} providers={breadth} [{mstatus}]")
    print()
    print("PROMOTED_ROUTING entries:")
    if not promoted:
        print("  (none)")
    for mid in sorted(promoted):
        g = live[mid]
        adv = "ADVERTISED" if g.get("advertised") else "not-advertised"
        print(f"  {mid:50s} advertised={adv}")
    print()
    print(f"(digest time {now})")
    return 0


def _cmd_apply(store: dict, canonical: str) -> int:
    rec = store.get(canonical)
    if rec is None:
        print(f"error: {canonical} not in store", file=sys.stderr)
        return 2
    if rec.get("status") == "rejected":
        print(f"error: {canonical} is rejected — use deny-then-restage to override",
              file=sys.stderr)
        return 2
    if rec.get("modality") != "chat":
        print(f"error: {canonical} is non-chat — cannot promote", file=sys.stderr)
        return 2
    if rec.get("status") != "eligible":
        print(f"error: {canonical} status={rec.get('status')}; only 'eligible' models "
              f"can be promoted (run the probe/drift first)", file=sys.stderr)
        return 2
    if _provider_breadth(rec) < 2:
        print(f"warn: {canonical} has only {_provider_breadth(rec)} provider(s); "
              f"promoting anyway (probe evidence kept)", file=sys.stderr)

    now = datetime.now(timezone.utc).isoformat()
    rec["status"] = "promoted_routing"
    rec["decided_by"] = "human"
    rec["decided_at"] = now
    # recompute advertise flag (tier wall + measured-price gate)
    healthy = _healthy_providers()
    measured = _measured_models()
    out = cdc.refresh_advertised_flags(store, healthy_providers=healthy,
                                       measured_models=measured, now_iso=now)
    _save(out)
    advert = out[canonical].get("advertised")
    print(f"promoted {canonical} -> status=promoted_routing, "
          f"advertised={advert}")
    if not advert:
        print(f"  not advertised: needs >=1 healthy non-z.ai provider AND measured "
              f"price (healthy={sorted(healthy)}, measured={'yes' if canonical in measured else 'no'})")
    print("  NOTE: reload flat_router via refresh_intake_overlay() / proxy restart "
          "to make it routable; /v1/models overlay is read live.")
    return 0


def _cmd_deny(store: dict, canonical: str) -> int:
    rec = store.get(canonical)
    if rec is None:
        print(f"error: {canonical} not in store", file=sys.stderr)
        return 2
    now = datetime.now(timezone.utc).isoformat()
    rec["status"] = "rejected"
    rec["decided_by"] = "human"
    rec["decided_at"] = now
    rec["advertised"] = False
    _save(store)
    print(f"denied {canonical} -> status=rejected (removed from overlay/advertising)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="model_promote.py",
                                 description="INTAKE-3 promotion CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="human digest of eligible/promoted entries")
    p_apply = sub.add_parser("apply", help="promote an eligible model")
    p_apply.add_argument("canonical")
    p_deny = sub.add_parser("deny", help="permanently reject a model")
    p_deny.add_argument("canonical")
    args = ap.parse_args(argv)

    store = _load()
    if args.cmd == "list":
        return _print_list(store)
    if args.cmd == "apply":
        return _cmd_apply(store, args.canonical)
    if args.cmd == "deny":
        return _cmd_deny(store, args.canonical)
    return 1


if __name__ == "__main__":
    sys.exit(main())
