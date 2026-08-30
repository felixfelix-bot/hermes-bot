"""Tests for flat_router._passes_live_catalog_guard (catalog-drift D1/D2)."""
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import flat_router as fr


@pytest.fixture
def catalog_file(tmp_path, monkeypatch):
    """Point the guard at a temp state file with controlled content."""
    state_path = tmp_path / "live_catalog_state.json"
    now = time.time()
    state = {
        "fetched_at": now,
        "providers": {
            "ollama_cloud": {
                "models": ["glm-5.3", "glm-5.2"],
                "fetched_at": now,
                "catalog_complete": True,
                "allowlist_extra": [],
            },
            "ours": {
                "models": ["glm-5.2"],
                "fetched_at": now,
                "catalog_complete": False,   # incomplete upstream listing
                "allowlist_extra": ["glm-4.6v"],
            },
        },
    }
    monkeypatch.setattr(fr, "_LIVE_CATALOG_PATH", state_path)
    monkeypatch.setattr(fr, "_live_catalog_cache", {"mtime": 0.0, "providers": {}})
    def write(content):
        state_path.write_text(json.dumps(content))
        return state_path
    return write, state, state_path


class TestLiveCatalogGuard:
    def test_fresh_complete_passes_live_model(self, catalog_file):
        write, state, _ = catalog_file
        write(state)
        assert fr._passes_live_catalog_guard("ollama_cloud", "glm-5.3") is True

    def test_fresh_complete_blocks_phantom(self, catalog_file):
        write, state, _ = catalog_file
        write(state)
        assert fr._passes_live_catalog_guard("ollama_cloud", "kimi-k3") is False

    def test_allowlist_extra_passes(self, catalog_file):
        write, state, _ = catalog_file
        write(state)
        # ours snapshot is incomplete but allowlisted models can be checked?
        # NO — catalog_complete=False → fail-open for everything (see D2)
        assert fr._passes_live_catalog_guard("ours", "glm-4.6v") is True

    def test_incomplete_catalog_failopen(self, catalog_file):
        write, state, _ = catalog_file
        write(state)
        # catalog_complete=False → guard never blocks, even unknown models
        assert fr._passes_live_catalog_guard("ours", "totally-unknown") is True

    def test_missing_provider_failopen(self, catalog_file):
        write, state, _ = catalog_file
        write(state)
        assert fr._passes_live_catalog_guard("friend", "glm-5.3") is True

    def test_missing_file_failopen(self, catalog_file):
        write, state, _ = catalog_file
        # never create the file
        assert fr._passes_live_catalog_guard("ollama_cloud", "glm-5.3") is True

    def test_stale_snapshot_failopen(self, catalog_file):
        write, state, _ = catalog_file
        old = json.loads(json.dumps(state))
        old["fetched_at"] = time.time() - 49 * 3600
        write(old)
        assert fr._passes_live_catalog_guard("ollama_cloud", "kimi-k3") is True