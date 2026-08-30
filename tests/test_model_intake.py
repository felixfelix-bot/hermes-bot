"""Tests for INTAKE-1: model_intake.json stage store + drift-checker intake wiring.

Covers the four required behaviors from the plan:
  - new upstream model (live probe, unknown to model_context_registry.json AND
    flat_router PROVIDER_MODELS) -> staged entry written
  - non-chat (embedding) model -> status=rejected immediately
  - existing/known model -> no entry written
  - rerun idempotent (entry not duplicated, last_seen updated)
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
    """Point the intake store at a temp file and return a writer helper."""
    store_path = tmp_path / "model_intake.json"
    monkeypatch.setattr(cdc, "INTAKE_FILE", store_path)

    def write(content):
        store_path.write_text(json.dumps(content))
        return store_path

    return write, store_path


def _live(provider, canonical_ids, probe_status="ok"):
    """Build a single-provider live entry in the drift-checker shape."""
    return {
        provider: {
            "probe_status": probe_status,
            "canonical": sorted(canonical_ids),
            "chat_canonical": sorted(c for c in canonical_ids if cdc._is_chat_model(c)),
        }
    }


def _now():
    return datetime.now(timezone.utc).isoformat()


class TestIntakeStage:
    def test_new_upstream_chat_model_staged(self, intake_file):
        _, store_path = intake_file
        live = _live("opencode_go", ["deepseek-v5"])
        store = cdc.stage_new_models(live, {"opencode_go": set()}, {}, now_iso=_now())
        assert "deepseek-v5" in store
        rec = store["deepseek-v5"]
        assert rec["status"] == "staged"
        assert rec["modality"] == "chat"
        assert rec["advertised"] is False
        assert rec["first_seen"] == rec["last_seen"]
        assert rec["missing_since"] is None
        # persisted to disk
        assert "deepseek-v5" in json.loads(store_path.read_text())

    def test_embedding_model_rejected(self, intake_file):
        _, store_path = intake_file
        live = _live("neuralwatt", ["text-embedding-3-large"])
        store = cdc.stage_new_models(live, {"neuralwatt": set()}, {}, now_iso=_now())
        assert "text-embedding-3-large" in store
        assert store["text-embedding-3-large"]["status"] == "rejected"
        assert store["text-embedding-3-large"]["modality"] == "non-chat"

    def test_known_routable_model_no_entry(self, intake_file):
        _, store_path = intake_file
        # model already in PROVIDER_MODELS -> not new, no entry
        live = _live("ours", ["glm-5.3"])
        store = cdc.stage_new_models(
            live, {"ours": {"glm-5.3"}}, {}, now_iso=_now()
        )
        assert "glm-5.3" not in store
        assert json.loads(store_path.read_text()) == {}

    def test_known_context_registry_model_no_entry(self, intake_file):
        _, store_path = intake_file
        # model in model_context_registry.json but not routable -> known, no entry
        live = _live("ollama_cloud", ["kimi-k3"])
        store = cdc.stage_new_models(
            live, {"ollama_cloud": set()}, {"kimi-k3": 262144}, now_iso=_now()
        )
        assert "kimi-k3" not in store
        assert json.loads(store_path.read_text()) == {}

    def test_rerun_idempotent_updates_last_seen(self, intake_file):
        _, store_path = intake_file
        live = _live("opencode_go", ["deepseek-v5"])
        t1 = "2026-08-31T00:00:00+00:00"
        t2 = "2026-08-31T06:00:00+00:00"
        store1 = cdc.stage_new_models(live, {"opencode_go": set()}, {}, now_iso=t1)
        store2 = cdc.stage_new_models(live, {"opencode_go": set()}, {}, now_iso=t2)
        # single entry, not duplicated
        assert list(store2.keys()) == ["deepseek-v5"]
        # first_seen preserved, last_seen advanced
        assert store2["deepseek-v5"]["first_seen"] == t1
        assert store2["deepseek-v5"]["last_seen"] == t2
        assert store2["deepseek-v5"]["status"] == "staged"

    def test_failed_probe_skipped(self, intake_file):
        _, store_path = intake_file
        live = _live("neuralwatt", [], probe_status="error")
        store = cdc.stage_new_models(live, {"neuralwatt": set()}, {}, now_iso=_now())
        assert store == {}
        assert json.loads(store_path.read_text()) == {}
