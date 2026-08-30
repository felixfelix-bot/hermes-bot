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


# ── INTAKE-2: 1-token probe + cross-provider merge + eligibility ─────────────
# Probe staged (chat) models per provider: max_tokens=1 completion, record
# ts/pass/http/model_field. model_field must match the requested canonical
# family (catches silent substitution at probe time — mismatch = probe FAIL).
# Budget: ≤1 probe per (model, provider) per run. Never probe non-chat.
# Eligibility: ≥2 DISTINCT providers w/ pass=true -> status=eligible; else
# stays staged (probes retried next run, fail evidence kept).
class TestIntakeProbe:
    def _store(self, mid="deepseek-v5", status="staged", modality="chat"):
        return {
            mid: {
                "raw_ids": {"opencode_go": mid},
                "modality": modality,
                "status": status,
                "first_seen": "2026-08-31T00:00:00+00:00",
                "last_seen": "2026-08-31T00:00:00+00:00",
                "missing_since": None,
                "probes": {},
                "advertised": False,
                "decided_by": None,
                "decided_at": None,
            }
        }

    def _providers(self):
        return {
            "opencode_go": {"base_url": "https://opencode.ai/zen/go/v1", "key_env": "OPENCODE_GO_API_KEY"},
            "neuralwatt": {"base_url": "https://api.neuralwatt.com/v1", "key_env": "NEURALWATT_API_KEY"},
        }

    def _keys(self):
        return {"OPENCODE_GO_API_KEY": "k1", "NEURALWATT_API_KEY": "k2"}

    def test_two_passes_eligible(self, intake_file, monkeypatch):
        _, store_path = intake_file
        store = self._store()
        calls = []

        def fake_probe(url, key, provider, model_id, canonical_family):
            calls.append((provider, model_id))
            return {"ts": "t", "pass": True, "http": 200, "model_field": model_id}

        monkeypatch.setattr(cdc, "probe_chat", fake_probe)
        result = cdc.run_intake_probes(
            store, self._providers(), {}, self._keys(),
            now_iso="2026-08-31T06:00:00+00:00")
        assert result["deepseek-v5"]["status"] == "eligible"
        assert result["deepseek-v5"]["decided_by"] == "auto-rule"
        assert result["deepseek-v5"]["decided_at"] == "2026-08-31T06:00:00+00:00"
        assert len(calls) == 2
        # persisted to disk
        assert json.loads(store_path.read_text())["deepseek-v5"]["status"] == "eligible"

    def test_one_pass_stays_staged(self, intake_file, monkeypatch):
        _, store_path = intake_file
        store = self._store()

        def fake_probe(url, key, provider, model_id, canonical_family):
            ok = provider == "opencode_go"
            return {"ts": "t", "pass": ok,
                    "http": 200 if ok else 500, "model_field": model_id}

        monkeypatch.setattr(cdc, "probe_chat", fake_probe)
        result = cdc.run_intake_probes(
            store, self._providers(), {}, self._keys(), now_iso="t")
        assert result["deepseek-v5"]["status"] == "staged"
        assert result["deepseek-v5"]["probes"]["opencode_go"]["pass"] is True
        assert result["deepseek-v5"]["probes"]["neuralwatt"]["pass"] is False
        assert result["deepseek-v5"]["decided_by"] is None

    def test_model_field_mismatch_is_fail(self):
        # direct unit test of the family matcher (silent-substitution catch)
        assert cdc._model_field_matches("deepseek-v5", "deepseek-v5") is True
        assert cdc._model_field_matches("deepseek/deepseek-v5", "deepseek-v5") is True
        assert cdc._model_field_matches("deepseek-v5", "glm-5.3") is False
        assert cdc._model_field_matches("deepseek-v5", "") is False
        # tagged ollama form still matches the family
        assert cdc._model_field_matches("deepseek/deepseek-v5", "deepseek-v5:0731") is True

    def test_mismatch_probe_not_counted(self, intake_file, monkeypatch):
        _, store_path = intake_file
        store = self._store()

        def fake_probe(url, key, provider, model_id, canonical_family):
            # provider silently substitutes a DIFFERENT model
            return {"ts": "t", "pass": False, "http": 200, "model_field": "glm-5.3"}

        monkeypatch.setattr(cdc, "probe_chat", fake_probe)
        result = cdc.run_intake_probes(
            store, self._providers(), {}, self._keys(), now_iso="t")
        assert result["deepseek-v5"]["status"] == "staged"  # 0 real passes
        assert result["deepseek-v5"]["probes"]["opencode_go"]["pass"] is False
        assert result["deepseek-v5"]["probes"]["opencode_go"]["model_field"] == "glm-5.3"

    def test_non_chat_and_rejected_never_probed(self, intake_file, monkeypatch):
        _, store_path = intake_file
        store = self._store(status="rejected", modality="non-chat")
        calls = []

        def fake_probe(url, key, provider, model_id, canonical_family):
            calls.append(provider)
            return {"ts": "t", "pass": True, "http": 200, "model_field": model_id}

        monkeypatch.setattr(cdc, "probe_chat", fake_probe)
        cdc.run_intake_probes(
            store, {"opencode_go": {"base_url": "u1", "key_env": "K1"}}, {},
            {"K1": "k1"}, now_iso="t")
        assert calls == []  # never probed

    def test_budget_one_probe_per_model_provider(self, intake_file, monkeypatch):
        _, store_path = intake_file
        store = self._store()
        calls = []

        def fake_probe(url, key, provider, model_id, canonical_family):
            calls.append(provider)
            return {"ts": "t", "pass": True, "http": 200, "model_field": model_id}

        monkeypatch.setattr(cdc, "probe_chat", fake_probe)
        cdc.run_intake_probes(
            store, {"opencode_go": {"base_url": "u1", "key_env": "K1"}}, {},
            {"K1": "k1"}, now_iso="t")
        assert calls.count("opencode_go") == 1
