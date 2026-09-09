"""Tests for INTAKE-4: removal grace + PROMOTION BATCH/REMOVALS digest.

Covers the required behaviors from the plan:
  - Model absent from ALL live provider catalogs -> missing_since set, status
    becomes 'grace', and it appears in the grace alert (drift report line).
  - A model present in at least one live catalog is NOT graced (missing_since
    stays None; a previously-graced model that reappears clears its grace).
  - Grace > 7 days (non-promoted) -> entry dropped from the store (auto).
  - Grace > 7 days AND status==promoted_routing -> KEPT in store but listed in
    the human digest (REMOVALS section) for MANUAL registry removal — routing
    entries are NEVER auto-removed.
  - Drift-cron digest stdout gains 'PROMOTION BATCH:' section (eligible models:
    canonical, provider breadth, measured-price y/n) and 'REMOVALS:' section.
    Both sections are OMITTED when empty, so the drift cron stays
    empty-stdout-when-clean for the clean case.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

# catalog_drift_check.py lives in src/; import it directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import catalog_drift_check as cdc


@pytest.fixture
def intake_file(tmp_path, monkeypatch):
    store_path = tmp_path / "model_intake.json"
    monkeypatch.setattr(cdc, "INTAKE_FILE", store_path)
    return store_path


def _live(providers):
    """Build a live-provider dict in the drift-checker shape.

    providers: {name: [canonical_ids]} for OK-probed providers. A separate
    dict may also include failed/skipped probes (ignored for grace calc).
    """
    out = {}
    for name, canon in providers.items():
        out[name] = {"probe_status": "ok", "canonical": sorted(canon)}
    return out


def _rec(canonical, status="staged", modalities=None, last_seen="2026-08-31T00:00:00+00:00",
         raw_ids=None):
    return {
        canonical: {
            "raw_ids": raw_ids or {p: canonical for p in (modalities or {"x"})},
            "modality": "chat",
            "status": status,
            "first_seen": "2026-08-31T00:00:00+00:00",
            "last_seen": last_seen,
            "missing_since": None,
            "probes": {},
            "advertised": False,
            "decided_by": "auto-rule" if status != "promoted_routing" else "human",
            "decided_at": "2026-08-31T00:00:00+00:00",
        }
    }


def _now(day="2026-09-05T00:00:00+00:00"):
    return day


# ── removal grace tracking ─────────────────────────────────────────────────
class TestRemovalGrace:
    def test_absent_from_all_catalogs_graced(self, intake_file):
        store_path = intake_file
        store = _rec("deepseek-v5", status="staged", raw_ids={"neuralwatt": "deepseek-v5"})
        live = _live({"neuralwatt": ["glm-5.3"]})  # deepseek-v5 gone everywhere
        out, _manual = cdc.apply_removal_grace(store, live, now_iso=_now())
        rec = out["deepseek-v5"]
        assert rec["status"] == "grace"
        assert rec["missing_since"] == _now()
        # persisted
        assert json.loads(store_path.read_text())["deepseek-v5"]["status"] == "grace"

    def test_present_in_any_catalog_not_graced(self, intake_file):
        store_path = intake_file
        store = _rec("deepseek-v5", raw_ids={"neuralwatt": "deepseek-v5",
                                             "opencode_go": "deepseek-v5"})
        # present in opencode_go but not neuralwatt
        live = _live({"neuralwatt": ["glm-5.3"], "opencode_go": ["deepseek-v5"]})
        out, _manual = cdc.apply_removal_grace(store, live, now_iso=_now())
        rec = out["deepseek-v5"]
        assert rec["status"] == "staged"
        assert rec["missing_since"] is None

    def test_grace_persists_missing_since_across_runs(self, intake_file):
        # once missing_since is set it is NOT reset on subsequent runs
        store_path = intake_file
        store = _rec("deepseek-v5", raw_ids={"neuralwatt": "deepseek-v5"})
        t1 = _now("2026-09-01T00:00:00+00:00")
        t2 = _now("2026-09-03T00:00:00+00:00")
        out1, _ = cdc.apply_removal_grace(store, _live({"neuralwatt": ["glm-5.3"]}), now_iso=t1)
        out2, _ = cdc.apply_removal_grace(out1, _live({"neuralwatt": ["glm-5.3"]}), now_iso=t2)
        assert out2["deepseek-v5"]["missing_since"] == t1  # earliest mark kept
        assert out2["deepseek-v5"]["status"] == "grace"

    def test_reappearing_model_clears_grace(self, intake_file):
        store_path = intake_file
        store = _rec("deepseek-v5", raw_ids={"neuralwatt": "deepseek-v5"})
        # promoted_routing model transiently disappeared, now back
        store["deepseek-v5"].update({"status": "grace", "pre_grace_status": "promoted_routing",
                                     "missing_since": _now("2026-09-01T00:00:00+00:00")})
        # model reappears in a live catalog
        live = _live({"neuralwatt": ["deepseek-v5"]})
        out, _manual = cdc.apply_removal_grace(store, live, now_iso=_now())
        rec = out["deepseek-v5"]
        assert rec["missing_since"] is None
        # promoted routing status is RESTORED (never lost on a transient blip)
        assert rec["status"] == "promoted_routing"

    def test_failed_or_skipped_probe_does_not_force_grace(self, intake_file):
        # a provider whose probe failed should not be treated as "missing".
        store = _rec("deepseek-v5", raw_ids={"neuralwatt": "deepseek-v5"})
        live = {"neuralwatt": {"probe_status": "error", "canonical": []}}
        out, _manual = cdc.apply_removal_grace(store, live, now_iso=_now())
        assert out["deepseek-v5"]["status"] == "staged"
        assert out["deepseek-v5"]["missing_since"] is None


# ── grace eviction (>7d) ───────────────────────────────────────────────────
class TestGraceEviction:
    def test_grace_over_7d_non_promoted_dropped(self, intake_file):
        store_path = intake_file
        store = _rec("old-model", raw_ids={"neuralwatt": "old-model"})
        store["old-model"].update({"status": "grace", "pre_grace_status": "staged",
                                   "missing_since": _now("2026-08-20T00:00:00+00:00")})
        live = _live({"neuralwatt": ["glm-5.3"]})
        out, manual = cdc.apply_removal_grace(
            store, live, now_iso=_now("2026-09-05T00:00:00+00:00"))
        assert "old-model" not in out  # dropped from store
        assert manual == []
        assert "old-model" not in json.loads(store_path.read_text())

    def test_grace_under_7d_kept(self, intake_file):
        store = _rec("recent-model", raw_ids={"neuralwatt": "recent-model"})
        store["recent-model"].update({"status": "grace", "pre_grace_status": "staged",
                                      "missing_since": _now("2026-09-03T00:00:00+00:00")})
        live = _live({"neuralwatt": ["glm-5.3"]})
        out, manual = cdc.apply_removal_grace(
            store, live, now_iso=_now("2026-09-05T00:00:00+00:00"))
        assert out["recent-model"]["status"] == "grace"  # kept during grace window
        assert manual == []

    def test_grace_over_7d_promoted_routing_kept_and_flagged_manual(
            self, intake_file):
        store = _rec("dead-routed", status="promoted_routing",
                     raw_ids={"neuralwatt": "dead-routed", "opencode_go": "dead-routed"})
        store["dead-routed"].update({"status": "grace", "pre_grace_status": "promoted_routing",
                                     "missing_since": _now("2026-08-15T00:00:00+00:00")})
        live = _live({"neuralwatt": ["glm-5.3"], "opencode_go": ["glm-5.3"]})
        out, manual = cdc.apply_removal_grace(
            store, live, now_iso=_now("2026-09-05T00:00:00+00:00"))
        # routing entry NEVER auto-removed from the store
        assert "dead-routed" in out
        # but it is listed for MANUAL registry removal
        assert "dead-routed" in manual

    def test_no_promoted_over_grace_no_manual_removals(self, intake_file):
        store = _rec("deepseek-v5", status="staged", raw_ids={"neuralwatt": "deepseek-v5"})
        store["deepseek-v5"].update({"status": "grace", "pre_grace_status": "staged",
                                     "missing_since": _now("2026-09-03T00:00:00+00:00")})
        live = _live({"neuralwatt": ["glm-5.3"]})
        out, manual_removals = cdc.apply_removal_grace(
            store, live, now_iso=_now("2026-09-05T00:00:00+00:00"))
        assert manual_removals == []


# ── digest stdout sections ─────────────────────────────────────────────────
class TestDigest:
    def test_promotion_batch_omitted_when_no_eligible(self):
        store = _rec("deepseek-v5", status="staged", raw_ids={"neuralwatt": "deepseek-v5"})
        digest = cdc.format_digest(store, measured_models=set(), manual_removals=[])
        assert "PROMOTION BATCH" not in digest

    def test_removals_omitted_when_none(self):
        store = _rec("deepseek-v5", status="staged", raw_ids={"neuralwatt": "deepseek-v5"})
        digest = cdc.format_digest(store, measured_models=set(), manual_removals=[])
        assert "REMOVALS" not in digest

    def test_promotion_batch_lists_eligible_with_breadth_and_price(self):
        # deepseek-v5 eligible on 2 providers, measured
        store = _rec("deepseek-v5", status="eligible",
                     raw_ids={"neuralwatt": "deepseek-v5", "opencode_go": "deepseek-v5"})
        digest = cdc.format_digest(store, measured_models={"deepseek-v5"}, manual_removals=[])
        assert "PROMOTION BATCH:" in digest
        assert "deepseek-v5" in digest
        # provider breadth 2 shown
        assert "2" in digest
        # measured-price y/n shown
        assert "measured" in digest

    def test_promotion_batch_measures_price_flag(self):
        store = _rec("glm-5.4", status="eligible",
                     raw_ids={"neuralwatt": "glm-5.4", "opencode_go": "glm-5.4"})
        digest = cdc.format_digest(store, measured_models=set(), manual_removals=[])  # unmeasured
        assert "PROMOTION BATCH:" in digest
        assert "glm-5.4" in digest
        assert "unmeasured" in digest.lower() or "no" in digest.lower()

    def test_removals_lists_models_for_manual_removal(self):
        store = {}
        digest = cdc.format_digest(store, measured_models=set(),
                                   manual_removals=["dead-routed"])
        assert "REMOVALS:" in digest
        assert "dead-routed" in digest
        # routing model is never auto-removed, only flagged for manual action
        assert "PROMOTION BATCH" not in digest

    def test_clean_case_has_no_digest_section(self):
        store = _rec("glm-5.3", status="promoted_routing",
                     raw_ids={"neuralwatt": "glm-5.3", "opencode_go": "glm-5.3"})
        digest = cdc.format_digest(store, measured_models=set(), manual_removals=[])
        assert digest == ""  # empty = drift cron stays silent when clean


# ── grace alert line in drift report ───────────────────────────────────────
class TestGraceAlert:
    def test_grace_models_reported_as_alert_line(self):
        graced = cdc.format_grace_alert(["deepseek-v5", "old-model"])
        assert "deepseek-v5" in graced
        assert "old-model" in graced
        assert "grace" in graced.lower()

    def test_grace_alert_empty_when_none(self):
        assert cdc.format_grace_alert([]) == ""
