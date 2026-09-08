#!/usr/bin/env python3
"""test_feedback_loop_gate.py — TDD tests for the Step 8c feedback-loop closure gate.

The gate (scripts/feedback_loop_gate.py) proves a provider's Kalman feedback
loop is provably closed before onboarding/ratification may begin. It fails on
ANY of: non-200 dispatch, NULL cost, non-canonical model logged,
undefined/under-keyed rate table, a balance collector missing where a balance
API exists, or a seed that won zero traffic.

These tests are data-layer: they build throwaway sqlite DBs and parse a
modelled zai_proxy source string so the verdicts are deterministic and need no
live proxy, network, or keys. A healthy (already-correct) provider fixture must
yield all-GREEN; a deliberately-broken fixture (missing rate table / bare model
form / NULL cost) must be flagged RED.

Run:  python3 -m pytest tests/test_feedback_loop_gate.py -v
  or: python3 tests/test_feedback_loop_gate.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "scripts"))

import feedback_loop_gate as g  # noqa: E402

# ── Fixture helpers ─────────────────────────────────────────────────────────


def _make_usage_db(path, rows, schema=None, call_ts=1_788_800_000.0):
    """Create an api_calls table (RP-1 schema) and insert rows.

    Each row: dict with keys key_name, model, total_tokens, cost_usd,
    cost_source, status_code, ts (default to call_ts).
    """
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE api_calls ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, key_name TEXT,"
        "model TEXT, total_tokens INTEGER, cost_usd REAL, cost_source TEXT,"
        "status_code INTEGER)"
    )
    for i, r in enumerate(rows):
        conn.execute(
            "INSERT INTO api_calls (ts, key_name, model, total_tokens, cost_usd,"
            " cost_source, status_code) VALUES (?,?,?,?,?,?,?)",
            (
                r.get("ts", call_ts),
                r.get("key_name", "acme"),
                r.get("model", "acme/acme-1"),
                r.get("total_tokens", 1000),
                r.get("cost_usd"),
                r.get("cost_source"),
                r.get("status_code", 200),
            ),
        )
    conn.commit()
    conn.close()


def _make_burn_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE balance_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " ts REAL NOT NULL, provider TEXT NOT NULL, balance_usd REAL,"
        " total_credits REAL, total_usage REAL, currency TEXT, raw TEXT, error TEXT)"
    )
    for i, r in enumerate(rows):
        conn.execute(
            "INSERT INTO balance_snapshots (ts, provider, balance_usd)"
            " VALUES (?,?,?)",
            (r["ts"], r["provider"], r.get("balance_usd")),
        )
    conn.commit()
    conn.close()


# A modelled zai_proxy source: DEEPSEEK-like rate table defined + keyed.
_HEALTHY_SRC = """\
DEEPSEEK_RATES: dict[str, dict[str, float]] = {
    "acme-1": {"input": 0.10, "output": 0.40},
    "acme-2": {"input": 0.20, "output": 0.60},
}
_PROVIDER_MODEL_NAMES = {
    "acme": {"acme/acme-1": "acme-1", "acme/acme-2": "acme-2"},
}
def _get_provider_cost(name, model_id):
    rates = DEEPSEEK_RATES.get(model_id)
    return _blended_rate(rates["input"], rates["output"])
def _extract_cost(provider, response_buffer, total_tokens=0):
    return ((total_tokens / 1_000_000) * 0.25, "rate_derived")
"""

# Broken A.lint fixture: rate table referenced but never defined (NameError trap).
_UNDEFINED_RATES_SRC = """\
def _get_provider_cost(name, model_id):
    rates = ACME_RATES.get(model_id)   # ACME_RATES never defined -> NameError
    return _blended_rate(rates["input"], rates["output"])
"""

# Broken A.lint fixture: rate table defined but does NOT cover a mapped model.
_UNDERKEYED_RATES_SRC = """\
ACME_RATES: dict[str, dict[str, float]] = {
    "acme-1": {"input": 0.10, "output": 0.40},
}
_PROVIDER_MODEL_NAMES = {
    "acme": {"acme/acme-1": "acme-1", "acme/acme-2": "acme-2"},  # acme-2 not in table
}
def _get_provider_cost(name, model_id):
    rates = ACME_RATES.get(model_id)
    return _blended_rate(rates["input"], rates["output"])
"""

_REF_RATES = {"acme/acme-1": 0.25, "acme/acme-2": 0.425, "acme-1": 0.25}


class TestProbeA_LiveDispatchCost(unittest.TestCase):
    """(A) Live dispatch 200 + correct cost (kills the $1.0 catch-all / $1.2654)."""

    def test_red_when_unknown_provider_has_no_rows(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "u.db")
            _make_usage_db(db, [])
            ev = g.probe_live_dispatch_cost(
                "acme", usage_db=db, reference_rates=_REF_RATES, _now=1_788_880_000.0
            )
            self.assertEqual(ev.status, "RED")
            self.assertTrue(ev.required)

    def test_green_when_rows_have_correct_non_fallback_cost(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "u.db")
            _make_usage_db(
                db,
                [
                    # acme-1 real blended ~$0.25/M: 250 tokens -> $0.0000625
                    {"key_name": "acme", "model": "acme/acme-1",
                     "total_tokens": 250, "cost_usd": 0.0000625,
                     "cost_source": "rate_derived", "status_code": 200},
                ],
            )
            ev = g.probe_live_dispatch_cost(
                "acme", usage_db=db, reference_rates=_REF_RATES, _now=1_788_880_000.0
            )
            self.assertEqual(ev.status, "GREEN")

    def test_red_on_rate_derived_fallback_source(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "u.db")
            _make_usage_db(
                db,
                [{"key_name": "acme", "model": "acme/acme-1", "total_tokens": 250,
                  "cost_usd": 0.0000625, "cost_source": "rate_derived_fallback",
                  "status_code": 200}],
            )
            ev = g.probe_live_dispatch_cost(
                "acme", usage_db=db, reference_rates=_REF_RATES, _now=1_788_880_000.0
            )
            self.assertEqual(ev.status, "RED", ev.detail)

    def test_red_on_null_cost(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "u.db")
            _make_usage_db(
                db,
                [{"key_name": "acme", "model": "acme/acme-1", "total_tokens": 250,
                  "cost_usd": None, "cost_source": None, "status_code": 200}],
            )
            ev = g.probe_live_dispatch_cost(
                "acme", usage_db=db, reference_rates=_REF_RATES, _now=1_788_880_000.0
            )
            self.assertEqual(ev.status, "RED", ev.detail)

    def test_red_on_inflated_fallback_cost(self):
        """The $1.2654 inflated fallback: per-M far above the real blended rate."""
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "u.db")
            # 1000 tokens at $1.2654/M -> $0.0012654; acme-1 real is $0.25/M.
            _make_usage_db(
                db,
                [{"key_name": "acme", "model": "acme/acme-1", "total_tokens": 1000,
                  "cost_usd": 0.0012654, "cost_source": "rate_derived",
                  "status_code": 200}],
            )
            ev = g.probe_live_dispatch_cost(
                "acme", usage_db=db, reference_rates=_REF_RATES, _now=1_788_880_000.0
            )
            self.assertEqual(ev.status, "RED", ev.detail)

    def test_red_on_catch_all_one_dollar(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "u.db")
            _make_usage_db(
                db,
                [{"key_name": "acme", "model": "acme/acme-1", "total_tokens": 1000,
                  "cost_usd": 0.001, "cost_source": "rate_derived",
                  "status_code": 200}],  # $1.0/M vs real $0.25 -> RED
            )
            ev = g.probe_live_dispatch_cost(
                "acme", usage_db=db, reference_rates=_REF_RATES, _now=1_788_880_000.0
            )
            self.assertEqual(ev.status, "RED", ev.detail)


class TestProbeALint_RateTableDefined(unittest.TestCase):
    """(A.lint) Every referenced _RATES table defined + covers mapped models."""

    def test_green_when_table_defined_and_keyed(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "zai_proxy.py")
            with open(src, "w") as f:
                f.write(_HEALTHY_SRC)
            ev = g.probe_rate_table_lint("acme", source=src)
            self.assertEqual(ev.status, "GREEN", ev.detail)

    def test_red_when_table_referenced_but_undefined(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "zai_proxy.py")
            with open(src, "w") as f:
                f.write(_UNDEFINED_RATES_SRC)
            ev = g.probe_rate_table_lint("acme", source=src)
            self.assertEqual(ev.status, "RED", ev.detail)
            self.assertIn("ACME_RATES", ev.detail)

    def test_red_when_table_underkeyed(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "zai_proxy.py")
            with open(src, "w") as f:
                f.write(_UNDERKEYED_RATES_SRC)
            ev = g.probe_rate_table_lint("acme", source=src)
            self.assertEqual(ev.status, "RED", ev.detail)
            self.assertIn("acme-2", ev.detail)

    def test_red_when_source_missing(self):
        ev = g.probe_rate_table_lint("acme", source="/nonexistent/zai_proxy.py")
        self.assertEqual(ev.status, "RED", ev.detail)


class TestProbeB_CanonicalModel(unittest.TestCase):
    """(B) Canonical model form logged + real_price_tracker returns finite float."""

    CANON = {"acme/acme-1", "acme/acme-2"}

    def test_green_when_all_canonical_and_rate_finite(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "u.db")
            _make_usage_db(
                db,
                [{"key_name": "acme", "model": "acme/acme-1", "total_tokens": 100,
                  "cost_usd": 0.001, "cost_source": "rate_derived",
                  "status_code": 200}],
            )
            ev = g.probe_canonical_model(
                "acme", usage_db=db, canonical_forms=self.CANON,
                measured_rate=0.25, _now=1_788_880_000.0
            )
            self.assertEqual(ev.status, "GREEN", ev.detail)

    def test_red_on_bare_model_form(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "u.db")
            _make_usage_db(
                db,
                [{"key_name": "acme", "model": "acme-1", "total_tokens": 100,
                  "cost_usd": 0.001, "cost_source": "rate_derived",
                  "status_code": 200}],  # bare form, not canonical acme/acme-1
            )
            ev = g.probe_canonical_model(
                "acme", usage_db=db, canonical_forms=self.CANON,
                measured_rate=0.25, _now=1_788_880_000.0
            )
            self.assertEqual(ev.status, "RED", ev.detail)

    def test_red_when_measured_rate_none(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "u.db")
            _make_usage_db(
                db,
                [{"key_name": "acme", "model": "acme/acme-1", "total_tokens": 100,
                  "cost_usd": 0.001, "cost_source": "rate_derived",
                  "status_code": 200}],
            )
            ev = g.probe_canonical_model(
                "acme", usage_db=db, canonical_forms=self.CANON,
                measured_rate=None, _now=1_788_880_000.0
            )
            self.assertEqual(ev.status, "RED", ev.detail)

    def test_red_when_no_rows(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "u.db")
            _make_usage_db(db, [])
            ev = g.probe_canonical_model(
                "acme", usage_db=db, canonical_forms=self.CANON,
                measured_rate=0.25, _now=1_788_880_000.0
            )
            self.assertEqual(ev.status, "RED", ev.detail)


class TestProbeC_Balance(unittest.TestCase):
    """(C) Balance collector where a balance API exists; verified-negative elsewhere."""

    def test_green_with_fresh_balance_row(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "b.db")
            _make_burn_db(
                db,
                [{"ts": 1_788_879_800.0, "provider": "acme", "balance_usd": 5.0}],
            )
            ev = g.probe_balance(
                "acme", burn_db=db, balance_providers={"acme"},
                no_balance_providers=set(), negative_proofs=[],
                _now=1_788_880_000.0
            )
            self.assertEqual(ev.status, "GREEN", ev.detail)

    def test_red_when_balance_api_exists_but_no_rows(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "b.db")
            _make_burn_db(db, [])
            ev = g.probe_balance(
                "acme", burn_db=db, balance_providers={"acme"},
                no_balance_providers=set(), negative_proofs=[],
                _now=1_788_880_000.0
            )
            self.assertEqual(ev.status, "RED", ev.detail)

    def test_red_on_stale_balance_row(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "b.db")
            _make_burn_db(
                db,
                [{"ts": 1_788_000_000.0, "provider": "acme", "balance_usd": 5.0}],
            )  # >10 min stale -> RED
            ev = g.probe_balance(
                "acme", burn_db=db, balance_providers={"acme"},
                no_balance_providers=set(), negative_proofs=[],
                _now=1_788_880_000.0
            )
            self.assertEqual(ev.status, "RED", ev.detail)

    def test_red_on_negative_balance(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "b.db")
            _make_burn_db(
                db,
                [{"ts": 1_788_879_800.0, "provider": "acme", "balance_usd": -1.0}],
            )
            ev = g.probe_balance(
                "acme", burn_db=db, balance_providers={"acme"},
                no_balance_providers=set(), negative_proofs=[],
                _now=1_788_880_000.0
            )
            self.assertEqual(ev.status, "RED", ev.detail)

    def test_green_no_balance_provider_with_documented_negative_proof(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "b.db")
            _make_burn_db(db, [])
            proof = os.path.join(td, "acme-negative-probe.json")
            with open(proof, "w") as f:
                json.dump({"probe": "GET /v1/balance -> 404", "status": 404}, f)
            ev = g.probe_balance(
                "chutes", burn_db=db, balance_providers=set(),
                no_balance_providers={"chutes"}, negative_proofs=[proof],
                _now=1_788_880_000.0
            )
            self.assertEqual(ev.status, "GREEN", ev.detail)

    def test_red_no_balance_provider_without_proof(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "b.db")
            _make_burn_db(db, [])
            ev = g.probe_balance(
                "chutes", burn_db=db, balance_providers=set(),
                no_balance_providers={"chutes"}, negative_proofs=[],
                _now=1_788_880_000.0
            )
            self.assertEqual(ev.status, "RED", ev.detail)

    def test_skip_when_provider_class_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "b.db")
            _make_burn_db(db, [])
            ev = g.probe_balance(
                "mystery", burn_db=db, balance_providers=set(),
                no_balance_providers=set(), negative_proofs=[],
                _now=1_788_880_000.0
            )
            self.assertEqual(ev.status, "SKIP", ev.detail)


class TestProbeD_SeedWinsTraffic(unittest.TestCase):
    """(D) >=1 ROUTED 200 exists AND a non-seed measurement in the trailing window."""

    def test_green_with_routed_200_and_measured_rate(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "u.db")
            _make_usage_db(
                db,
                [{"key_name": "acme", "model": "acme/acme-1", "total_tokens": 100,
                  "cost_usd": 0.001, "cost_source": "rate_derived",
                  "status_code": 200}],
            )
            ev = g.probe_seed_wins_traffic(
                "acme", usage_db=db, measured_rate=0.25, _now=1_788_880_000.0
            )
            self.assertEqual(ev.status, "GREEN", ev.detail)

    def test_red_when_zero_routed_200(self):
        # Provider won NO traffic (seed never beats incumbent) -> deadlock.
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "u.db")
            _make_usage_db(
                db,
                [{"key_name": "acme", "model": "acme/acme-1", "total_tokens": 100,
                  "cost_usd": 0.001, "cost_source": "rate_derived",
                  "status_code": 404}],  # not a routed 200
            )
            ev = g.probe_seed_wins_traffic(
                "acme", usage_db=db, measured_rate=0.25, _now=1_788_880_000.0
            )
            self.assertEqual(ev.status, "RED", ev.detail)

    def test_red_when_no_measured_rate(self):
        # Traffic exists but only seed (no Kalman measurement) -> deadlock.
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "u.db")
            _make_usage_db(
                db,
                [{"key_name": "acme", "model": "acme/acme-1", "total_tokens": 100,
                  "cost_usd": 0.001, "cost_source": "rate_derived",
                  "status_code": 200}],
            )
            ev = g.probe_seed_wins_traffic(
                "acme", usage_db=db, measured_rate=None, _now=1_788_880_000.0
            )
            self.assertEqual(ev.status, "RED", ev.detail)


class TestProbeE_SSECost(unittest.TestCase):
    """(E) SSE cost extraction: streaming request logs non-NULL cost."""

    def test_green_when_streaming_row_logs_cost(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "u.db")
            _make_usage_db(
                db,
                [{"key_name": "acme", "model": "acme/acme-1", "total_tokens": 100,
                  "cost_usd": 0.001, "cost_source": "rate_derived",
                  "status_code": 200}],
            )
            ev = g.probe_sse_cost(
                "acme", usage_db=db, streaming_providers={"acme"},
                _now=1_788_880_000.0
            )
            self.assertEqual(ev.status, "GREEN", ev.detail)

    def test_red_when_streaming_row_logs_null_cost(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "u.db")
            _make_usage_db(
                db,
                [{"key_name": "acme", "model": "acme/acme-1", "total_tokens": 100,
                  "cost_usd": None, "cost_source": None, "status_code": 200}],
            )
            ev = g.probe_sse_cost(
                "acme", usage_db=db, streaming_providers={"acme"},
                _now=1_788_880_000.0
            )
            self.assertEqual(ev.status, "RED", ev.detail)

    def test_skip_for_non_streaming_provider(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "u.db")
            _make_usage_db(db, [])
            ev = g.probe_sse_cost(
                "acme", usage_db=db, streaming_providers=set(),
                _now=1_788_880_000.0
            )
            self.assertEqual(ev.status, "SKIP", ev.detail)


class TestRunGate_Baseline(unittest.TestCase):
    """run_gate() verdict + gate/<provider>.json output."""

    def _run(self, provider, usage_rows, burn_rows, src, td, now=1_788_880_000.0,
             **kw):
        u = os.path.join(td, "usage.db")
        b = os.path.join(td, "burn.db")
        s = os.path.join(td, "zai_proxy.py")
        _make_usage_db(u, usage_rows)
        _make_burn_db(b, burn_rows)
        with open(s, "w") as f:
            f.write(src)
        defaults = dict(
            usage_db=u, burn_db=b, source_path=s, out_dir=os.path.join(td, "gate"),
            balance_providers={"acme"}, no_balance_providers=set(),
            streaming_providers={"acme"}, _now=now,
            reference_rates=_REF_RATES,
            measured_rate=0.25,
        )
        defaults.update(kw)
        return g.run_gate(provider, **defaults)

    def _healthy_usage(self):
        return [
            {"key_name": "acme", "model": "acme/acme-1", "total_tokens": 250,
             "cost_usd": 0.0000625, "cost_source": "rate_derived",
             "status_code": 200},
        ]

    def _healthy_burn(self):
        return [{"ts": 1_788_879_800.0, "provider": "acme", "balance_usd": 5.0}]

    def test_all_green_on_healthy_provider(self):
        with tempfile.TemporaryDirectory() as td:
            res = self._run("acme", self._healthy_usage(), self._healthy_burn(),
                            _HEALTHY_SRC, td)
            self.assertTrue(res["ok"], res)
            for name, ev in res["probes"].items():
                self.assertEqual(ev["status"], "GREEN",
                                 f"{name} should be GREEN: {ev['detail']}")

    def test_writes_gate_json(self):
        with tempfile.TemporaryDirectory() as td:
            self._run("acme", self._healthy_usage(), self._healthy_burn(),
                      _HEALTHY_SRC, td)
            path = os.path.join(td, "gate", "acme.json")
            self.assertTrue(os.path.exists(path), path)
            with open(path) as f:
                blob = json.load(f)
            self.assertEqual(blob["provider"], "acme")
            self.assertTrue(blob["ok"])
            self.assertIn("probes", blob)
            # Every probe carries raw queries + timestamps in the evidence block.
            for name, ev in blob["probes"].items():
                self.assertIn("ts", ev)
                self.assertIn("query", ev)

    def test_red_on_deliberately_broken_provider(self):
        # Broken: rate_derived_fallback source (like the $1.0 catch-all) + a
        # bare (non-canonical) model form logged.
        with tempfile.TemporaryDirectory() as td:
            usage = [
                {"key_name": "acme", "model": "acme-1", "total_tokens": 1000,
                 "cost_usd": 0.001, "cost_source": "rate_derived_fallback",
                 "status_code": 200},
            ]
            res = self._run("acme", usage, self._healthy_burn(), _HEALTHY_SRC, td)
            self.assertFalse(res["ok"], res)
            statuses = {n: ev["status"] for n, ev in res["probes"].items()}
            # A and B must be RED; verdict not all-green.
            self.assertEqual(statuses.get("A"), "RED")
            self.assertEqual(statuses.get("B"), "RED")

    def test_red_when_rate_table_undefined(self):
        with tempfile.TemporaryDirectory() as td:
            res = self._run("acme", self._healthy_usage(), self._healthy_burn(),
                            _UNDEFINED_RATES_SRC, td, reference_rates=_REF_RATES)
            self.assertFalse(res["ok"], res)
            self.assertEqual(res["probes"]["A.lint"]["status"], "RED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
