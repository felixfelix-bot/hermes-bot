"""Tests for dynamic_context_length_governor pin + detection semantics.

2026-09-05: the DELIBERATE_CONTEXT_LENGTHS pin changed from blind exemption
to ACTIVE enforcement (heal-on-drift), and model resolution now prefers the
profile's own configured model over usage-DB detection.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

GOVERNOR_PATH = Path(__file__).resolve().parent.parent / "dynamic_context_length_governor.py"


@pytest.fixture()
def gov(monkeypatch, tmp_path):
    """Load the governor module fresh with isolated paths and no subprocess."""
    spec = importlib.util.spec_from_file_location("dcl_governor_test", GOVERNOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dcl_governor_test"] = mod
    spec.loader.exec_module(mod)

    calls = {"sets": []}

    def fake_set(new_value, profile_name="manager"):
        calls["sets"].append((profile_name, new_value))
        return True

    monkeypatch.setattr(mod, "set_context_length", fake_set)
    monkeypatch.setattr(mod, "DB_PATH", tmp_path / "nonexistent.db")
    return mod, calls


def _pin_ctx(mod, monkeypatch, value):
    monkeypatch.setattr(
        mod, "get_current_context_length", lambda path=None: value
    )


def test_pinned_profile_drift_is_healed(gov, monkeypatch):
    mod, calls = gov
    monkeypatch.setitem(mod.DELIBERATE_CONTEXT_LENGTHS, "manager", 1_048_576)
    _pin_ctx(mod, monkeypatch, 200_000)  # stale value from the old pin

    result = mod.process_profile("manager")

    assert result["applied"] is True
    assert result["reason"] == "pinned-healed"
    assert calls["sets"] == [("manager", 1_048_576)]


def test_pinned_profile_ok_when_matching(gov, monkeypatch):
    mod, calls = gov
    monkeypatch.setitem(mod.DELIBERATE_CONTEXT_LENGTHS, "manager", 1_048_576)
    _pin_ctx(mod, monkeypatch, 1_048_576)

    result = mod.process_profile("manager")

    assert result["applied"] is False
    assert result["reason"] == "pinned-ok"
    assert calls["sets"] == []


def test_non_pinned_profile_uses_own_configured_model(gov, monkeypatch, tmp_path):
    mod, calls = gov
    assert "worker-x" not in mod.DELIBERATE_CONTEXT_LENGTHS

    config = tmp_path / "config.yaml"
    config.write_text(
        "model:\n  default: kimi-k3\n  provider: zai\n  context_length: 128000\n"
    )
    monkeypatch.setattr(mod, "profile_config_path", lambda p: config)

    def _pollution(profile_name, db_path=None):
        raise AssertionError("usage-DB detection must not run when config model exists")

    monkeypatch.setattr(mod, "detect_active_model_for_profile", _pollution)

    registry = {"kimi-k3": 262_144}
    monkeypatch.setattr(mod, "get_model_context_length", lambda m: registry.get(m))

    result = mod.process_profile("worker-x")

    assert result["detected_model"] == "kimi-k3"
    assert result["new_ctx"] == 262_144
    assert calls["sets"] == [("worker-x", 262_144)]


def test_get_configured_model_reads_mapping_and_bare_string(gov, tmp_path):
    mod, _ = gov
    mapping = tmp_path / "a.yaml"
    mapping.write_text("model:\n  default: glm-5.3\n  context_length: 1048576\n")
    bare = tmp_path / "b.yaml"
    bare.write_text("model: glm-4.5-air\n")

    assert mod.get_configured_model(mapping) == "glm-5.3"
    assert mod.get_configured_model(bare) == "glm-4.5-air"
    assert mod.get_configured_model(tmp_path / "missing.yaml") is None
