#!/usr/bin/env python3
"""Tests for scripts/crons/kanban_auto_assigner.py — canonical consolidation.

Verifies that the assigner DELEGATES offload decisions to offload_router
(classify->probe->route) and that recommend_profile fixes the
board-preferred-over-stress bug (worker-dq05 beats an idle board profile
when the router says offload). All hermetic: no board DB, no CLI, no network.

Run:  python3 -m pytest tests/test_kanban_auto_assigner.py -q
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "crons"))

import offload_router
import kanban_auto_assigner as kaa


def _green_facts(profile="worker-dq05", decision="green"):
    return [{
        "name": "dq05", "profile": profile, "ssh_alias": "dq05",
        "reachable": decision == "green", "decision": decision,
        "load_per_core": 0.1, "free_ram_mb": 8192, "free_disk_mb": 200000,
        "cores": 4, "source": "ssh", "opportunistic": False,
    }]


def test_assigner_imports_offload_router():
    assert offload_router is not None
    assert hasattr(kaa, "recommend_profile")


def test_recommend_offloads_heavy_to_dq05_when_stressed_and_idle(monkeypatch):
    # board 'plebeian' prefers worker-plebeian; router says OFFLOAD because
    # cargo build + local stressed + dq05 green -> worker-dq05 MUST win.
    monkeypatch.setattr(offload_router, "load_board_rule", lambda b, **k: "auto")
    prof, why = kaa.recommend_profile(
        "plebeian", "cargo build the market indexer", "t_x",
        body="full compile of the rust crate",
        idle_profiles={"worker-dq05", "worker-plebeian"},
        probe_facts=_green_facts(),
        local_load_per_core=4.2,
    )
    assert prof == "worker-dq05"
    assert "offload_route" in why


def test_recommend_board_preferred_when_local_idle(monkeypatch):
    monkeypatch.setattr(offload_router, "load_board_rule", lambda b, **k: "auto")
    prof, why = kaa.recommend_profile(
        "plebeian", "cargo build the market indexer", "t_x",
        idle_profiles={"worker-dq05", "worker-plebeian"},
        probe_facts=_green_facts(),
        local_load_per_core=0.5,   # not stressed -> no offload
    )
    assert prof == "worker-plebeian"
    assert "board_preferred_idle" in why or "keyword_route" in why


def test_recommend_never_offloads_when_dq05_profile_busy(monkeypatch):
    monkeypatch.setattr(offload_router, "load_board_rule", lambda b, **k: "auto")
    prof, why = kaa.recommend_profile(
        "plebeian", "cargo build the market indexer", "t_x",
        idle_profiles={"worker-plebeian"},          # worker-dq05 NOT idle
        probe_facts=_green_facts(),
        local_load_per_core=4.2,
    )
    assert prof == "worker-plebeian"


def test_recommend_board_off_stays_local(monkeypatch):
    monkeypatch.setattr(offload_router, "load_board_rule", lambda b, **k: "off")
    prof, why = kaa.recommend_profile(
        "balloon", "cargo build the balloon node", "t_x",
        idle_profiles={"worker-dq05", "worker-balloon"},
        probe_facts=_green_facts(),
        local_load_per_core=6.0,
    )
    assert prof != "worker-dq05"


def test_recommend_hardware_task_never_offloads(monkeypatch):
    monkeypatch.setattr(offload_router, "load_board_rule", lambda b, **k: "force")
    prof, why = kaa.recommend_profile(
        "microfips", "flash the board via pio upload", "t_x",
        idle_profiles={"worker-dq05", "worker-fips"},
        probe_facts=_green_facts(),
        local_load_per_core=6.0,
    )
    assert prof != "worker-dq05"


def test_keyword_route_fallback_without_offload_router():
    """If offload_router import failed (degraded), routing is local-only."""
    saved = kaa.offload_router
    kaa.offload_router = None
    try:
        prof, why = kaa.recommend_profile("plebeian", "market react pr",
                                          "t_x", idle_profiles=set())
        assert prof == "worker-plebeian"
    finally:
        kaa.offload_router = saved
