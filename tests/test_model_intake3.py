"""Tests for INTAKE-3: promotion CLI + routing/advertise overlay + tier wall.

Covers the required behaviors from the plan:
  - overlay loads promoted_routing entries into PROVIDER_MODELS + dispatch
    translation (raw_id registered in _PROVIDER_MODEL_NAMES from probe evidence)
  - kill switch .disable_intake_overlay skips the overlay (on/off)
  - tier wall: a z.ai-only model (ours/friend/manager/worker*) is NEVER advertised
  - measured-price gate: unmeasured (n<50) never advertised; measured + a
    healthy non-z.ai provider -> advertised=true
  - failure injection: dominant (z.ai) provider unhealthy -> candidate breadth
    >=2 and the response model field (outgoing provider-native name) is correct
  - SUBST-marker audit: every non-identity dispatch mapping in
    _PROVIDER_MODEL_NAMES carries a dated # SUBST comment
"""
import ast
import json
import re
import sys
from pathlib import Path

import pytest

MRE = Path(__file__).resolve().parent.parent
# Import from THIS repo (worktree / clone), NOT the deployed ~/.hermes/bot
# copy (which may be on a different branch). Inserting ~/.hermes/bot last at
# position 0 would shadow the repo under test — so we only add the repo root
# and its src/ dir.
for _p in [str(MRE), str(MRE / "src")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import flat_router
import catalog_drift_check as cdc


# ── helpers ─────────────────────────────────────────────────────────────────
def _rec(canonical, providers, status="promoted_routing", advertised=False):
    """Build a model_intake.json record."""
    return {
        canonical: {
            "raw_ids": {p: canonical for p in providers},
            "modality": "chat",
            "status": status,
            "first_seen": "2026-08-31T00:00:00+00:00",
            "last_seen": "2026-08-31T00:00:00+00:00",
            "missing_since": None,
            "probes": {p: {"ts": "t", "pass": True, "http": 200,
                           "model_field": canonical} for p in providers},
            "advertised": advertised,
            "decided_by": "human",
            "decided_at": "2026-08-31T00:00:00+00:00",
        }
    }


@pytest.fixture
def intake_file(tmp_path, monkeypatch):
    """Point flat_router + cdc at a temp intake file and a temp switch path."""
    store_path = tmp_path / "model_intake.json"
    switch_path = tmp_path / ".disable_intake_overlay"
    monkeypatch.setattr(flat_router, "_INTAKE_FILE", store_path)
    monkeypatch.setattr(flat_router, "_DISABLE_INTAKE_OVERLAY", switch_path)
    monkeypatch.setattr(cdc, "INTAKE_FILE", store_path)
    return store_path, switch_path


@pytest.fixture
def restore_registries():
    """Snapshot PROVIDER_MODELS + _PROVIDER_MODEL_NAMES and restore after."""
    pm_snap = {k: set(v) for k, v in flat_router.PROVIDER_MODELS.items()}
    import zai_proxy
    nm_snap = {k: dict(v) for k, v in zai_proxy._PROVIDER_MODEL_NAMES.items()}
    yield
    flat_router.PROVIDER_MODELS.clear()
    flat_router.PROVIDER_MODELS.update(pm_snap)
    zai_proxy._PROVIDER_MODEL_NAMES.clear()
    zai_proxy._PROVIDER_MODEL_NAMES.update(nm_snap)


# ── overlay on/off w/ switch ───────────────────────────────────────────────
class TestOverlaySwitch:
    def test_loads_promoted_when_no_switch(self, intake_file):
        store_path, _ = intake_file
        store_path.write_text(json.dumps(_rec("deepseek-v5", ["neuralwatt", "opencode_go"])))
        overlay = flat_router._load_intake_overlay()
        assert "deepseek-v5" in overlay
        assert overlay["deepseek-v5"]["status"] == "promoted_routing"

    def test_skips_when_switch_present(self, intake_file):
        store_path, switch_path = intake_file
        store_path.write_text(json.dumps(_rec("deepseek-v5", ["neuralwatt"])))
        switch_path.touch()
        assert flat_router._load_intake_overlay() == {}

    def test_skips_when_file_missing(self, intake_file):
        _, switch_path = intake_file
        assert flat_router._load_intake_overlay() == {}

    def test_ignores_non_promoted_entries(self, intake_file):
        store_path, _ = intake_file
        store = _rec("deepseek-v5", ["neuralwatt"])
        store["deepseek-v5"]["status"] = "eligible"  # not yet promoted
        store_path.write_text(json.dumps(store))
        assert flat_router._load_intake_overlay() == {}


# ── overlay applies to PROVIDER_MODELS + dispatch translation ───────────────
class TestOverlayApply:
    def test_adds_to_provider_models_and_registers_translation(
            self, intake_file, restore_registries):
        store_path, _ = intake_file
        store_path.write_text(json.dumps(
            _rec("deepseek-v5", ["neuralwatt", "opencode_go"])))
        flat_router._apply_intake_overlay()
        assert "deepseek-v5" in flat_router.PROVIDER_MODELS["neuralwatt"]
        assert "deepseek-v5" in flat_router.PROVIDER_MODELS["opencode_go"]
        import zai_proxy
        assert zai_proxy._PROVIDER_MODEL_NAMES["neuralwatt"]["deepseek-v5"] == "deepseek-v5"

    def test_apply_respects_switch(self, intake_file, restore_registries):
        store_path, switch_path = intake_file
        store_path.write_text(json.dumps(
            _rec("deepseek-v5", ["neuralwatt"])))
        switch_path.touch()
        flat_router._apply_intake_overlay()
        assert "deepseek-v5" not in flat_router.PROVIDER_MODELS.get("neuralwatt", set())


# ── tier wall + measured-price gate (advertise flag) ─────────────────────────
class TestAdvertiseGate:
    def test_zai_only_never_advertised(self):
        # ours/friend/manager/worker* are ALL z.ai — tier wall, NEVER public
        store = _rec("glm-5.3", ["ours", "friend"])
        out = cdc.refresh_advertised_flags(
            store, healthy_providers={"ours", "friend"},
            measured_models={"glm-5.3"}, now_iso="t")
        assert out["glm-5.3"]["advertised"] is False

    def test_unmeasured_never_advertised(self):
        # measured_models does NOT include the model -> stays false (auto-retry)
        store = _rec("deepseek-v5", ["neuralwatt", "opencode_go"])
        out = cdc.refresh_advertised_flags(
            store, healthy_providers={"neuralwatt", "opencode_go"},
            measured_models=set(), now_iso="t")
        assert out["deepseek-v5"]["advertised"] is False

    def test_measured_plus_non_zai_advertised(self):
        store = _rec("deepseek-v5", ["neuralwatt", "opencode_go"])
        out = cdc.refresh_advertised_flags(
            store, healthy_providers={"neuralwatt", "opencode_go"},
            measured_models={"deepseek-v5"}, now_iso="t")
        assert out["deepseek-v5"]["advertised"] is True

    def test_measured_but_no_healthy_non_zai_not_advertised(self):
        # only z.ai provider healthy -> tier wall blocks
        store = _rec("deepseek-v5", ["ours", "neuralwatt"])
        out = cdc.refresh_advertised_flags(
            store, healthy_providers={"ours"},  # neuralwatt unhealthy
            measured_models={"deepseek-v5"}, now_iso="t")
        assert out["deepseek-v5"]["advertised"] is False

    def test_non_promoted_entries_untouched(self):
        store = _rec("deepseek-v5", ["neuralwatt"], status="eligible")
        out = cdc.refresh_advertised_flags(
            store, healthy_providers={"neuralwatt"},
            measured_models={"deepseek-v5"}, now_iso="t")
        assert out["deepseek-v5"]["status"] == "eligible"
        assert out["deepseek-v5"]["advertised"] is False

    def test_is_zai_provider(self):
        assert cdc.is_zai_provider("ours") is True
        assert cdc.is_zai_provider("friend") is True
        assert cdc.is_zai_provider("manager") is True
        assert cdc.is_zai_provider("worker-merchant") is True
        assert cdc.is_zai_provider("zai") is True
        assert cdc.is_zai_provider("neuralwatt") is False
        assert cdc.is_zai_provider("opencode_go") is False


# ── failure injection: dominant provider unhealthy ──────────────────────────
class TestFailureInjection:
    def test_dominant_zai_unhealthy_breadth_ge2_and_model_field(
            self, intake_file, restore_registries, monkeypatch):
        store_path, _ = intake_file
        # deepseek-v5 served by dominant z.ai (ours) + 2 non-z.ai providers
        store_path.write_text(json.dumps(
            _rec("deepseek-v5", ["ours", "neuralwatt", "opencode_go"])))
        flat_router._apply_intake_overlay()

        # dominant z.ai provider unhealthy; non-z.ai healthy
        def fake_health(name):
            return name != "ours"
        monkeypatch.setattr(flat_router, "_is_provider_healthy", fake_health)
        monkeypatch.setattr(flat_router, "_passes_live_catalog_guard",
                            lambda p, m: True)

        candidates = flat_router.select_provider(model="deepseek-v5")
        non_fallback = [c for c in candidates if c.name != "fallback"]
        assert len(non_fallback) >= 2, \
            f"expected >=2 candidates, got {[c.name for c in non_fallback]}"
        # no z.ai provider in the candidate set (tier wall / unhealthy)
        assert all(not cdc.is_zai_provider(c.name) for c in non_fallback)
        # response model field = provider-native raw_id (identity here)
        for c in non_fallback:
            assert c.model == "deepseek-v5", \
                f"outgoing model {c.model!r} != raw_id 'deepseek-v5'"


# ── SUBST-marker audit ──────────────────────────────────────────────────────
class TestSubstMarkerAudit:
    def test_every_non_identity_mapping_has_dated_subst_comment(self):
        """Every non-identity dispatch translation in _PROVIDER_MODEL_NAMES
        must carry a dated # SUBST comment on its source line, so silent
        substitutions are auditable."""
        src_path = MRE / "zai_proxy.py"
        src = src_path.read_text()
        tree = ast.parse(src)
        lines = src.splitlines()

        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for t in node.targets:
                if not (isinstance(t, ast.Name) and t.id == "_PROVIDER_MODEL_NAMES"):
                    continue
                d = node.value
                if not isinstance(d, ast.Dict):
                    continue
                # _PROVIDER_MODEL_NAMES is {provider: {canonical: native, ...}}.
                # Descend into each per-provider nested dict to audit the actual
                # model mappings (top-level keys are provider names, identity).
                for _prov_key, _prov_val in zip(d.keys, d.values):
                    if not isinstance(_prov_val, ast.Dict):
                        continue
                    for key, val in zip(_prov_val.keys, _prov_val.values):
                        if not (isinstance(key, ast.Constant)
                                and isinstance(val, ast.Constant)):
                            continue
                        canonical = str(key.value)
                        native = str(val.value)
                        if native == canonical:
                            continue  # identity mapping — no SUBST needed
                        line = lines[val.lineno - 1]
                        has_subst = "# SUBST" in line
                        has_date = bool(re.search(r"\d{4}-\d{2}-\d{2}", line))
                        found.append((canonical, native, val.lineno,
                                      has_subst, has_date))
                        assert has_subst and has_date, \
                            f"non-identity mapping {canonical!r}->{native!r} at line " \
                            f"{val.lineno} lacks a dated # SUBST comment: {line.strip()!r}"
        assert found, "no non-identity mappings found — audit is vacuous"

# ── measured-price model extraction (CLI helper) ──────────────────────────
class TestMeasuredModelsExtraction:
    """The CLI's _measured_models() must extract MODEL names from the
    real_price_tracker nested shape {provider: {model: rate, '_default': rate}},
    NOT provider names. Advertising a model requires its canonical to be in the
    measured set; extracting provider names would make the gate always-false."""

    def test_extracts_model_names(self):
        import scripts.model_promote as mp
        nested = {
            "neuralwatt": {"deepseek-v5": 1.2, "_default": 1.5},
            "opencode_go": {"deepseek-v5": 1.1, "_default": 1.3},
            "ours":        {"deepseek-v5": 0.4, "_default": 0.6},
        }
        got = mp._extract_measured_model_names(nested)
        assert got == {"deepseek-v5"}

    def test_measured_models_provider_names_never_leak(self):
        import scripts.model_promote as mp
        nested = {"neuralwatt": {"deepseek-v5": 1.2, "_default": 1.5}}
        got = mp._extract_measured_model_names(nested)
        assert "neuralwatt" not in got
        assert "_default" not in got
        assert got == {"deepseek-v5"}

