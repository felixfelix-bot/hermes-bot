#!/usr/bin/env python3
"""Tests for scripts/offload_router.py — canonical offload routing (Phase 1).

Covers the pure decision logic with zero network: probe results are injected
as facts. LAN-down / probe-error / ssh-fail paths are exercised by feeding
unreachable target facts.

Run:  python3 -m pytest tests/test_offload_router.py -q
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import offload_router as orr


# ---------------------------------------------------------------------------
# classify() — heavy classes (operator requirement: three explicit classes)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected_kind", [
    # build / compile
    ("Run cargo build for the mesh node", "build"),
    ("pio run -e native for the host build", "build"),
    ("tsc --noEmit typecheck the SDK", "build"),
    ("go build ./cmd/relay", "build"),
    ("make -j4 in the firmware dir", "build"),
    ("cmake -B build && make", "build"),
    ("npm run build the wizard", "build"),
    # test suites
    ("run vitest for the UI package", "test"),
    ("bun test merchant-routing", "test"),
    ("playwright e2e suite", "test"),
    ("pytest tests/offload -q", "test"),
    ("jest --runInBand", "test"),
    ("cargo test --all-features", "test"),
    ("go test ./...", "test"),
    ("full suite before merge", "test"),
    ("write tdd tests first", "test"),
    # data crunching
    ("analyze the burn log and plot trends", "crunch"),
    ("parse 3 GB of capture and aggregate", "crunch"),
    ("convert the csv to parquet", "crunch"),
    ("transform raw telemetry into metrics", "crunch"),
    ("run the benchmark simulation", "crunch"),
    # light / medium
    ("search the codebase for usage", "light"),
    ("show task status", "light"),
    ("update the readme wording", "medium"),
])
def test_classify_heavy_classes(text, expected_kind):
    assert orr.classify("x", text)["kind"] == expected_kind


def test_classify_requires_local_for_hardware_keywords():
    for kw in ["flash", "serial", "pio upload", "bootsel", "uart", "solder"]:
        assert orr.classify("x", f"do a {kw} readback")["kind"] == "hardware", kw


def test_classify_hardware_wins_over_build():
    # "build + flash" must route LOCAL even though build is heavy
    c = orr.classify("flash the built image", "pio upload the firmware")
    assert c["kind"] == "hardware"
    assert c["heavy"] is False


def test_classify_no_false_positive_on_dq05_word():
    # our own delegation copy must NOT self-classify as heavy
    assert orr.classify("task", "run ALL builds on dq05 via ssh dq05")["kind"] in (
        "light", "medium")


# ---------------------------------------------------------------------------
# board routing.json escape hatches
# ---------------------------------------------------------------------------

def _write_rule(tmp_path, rule):
    board_dir = Path(tmp_path) / "myboard"
    board_dir.mkdir(parents=True, exist_ok=True)
    (board_dir / "routing.json").write_text(json.dumps({"offload": rule}))
    return board_dir


def test_board_rule_default_auto_when_no_file(tmp_path):
    assert orr.load_board_rule("nonexistent-board", boards_root=tmp_path) == "auto"


def test_board_rule_reads_json(tmp_path):
    d = _write_rule(tmp_path, "off")
    assert orr.load_board_rule("myboard", boards_root=tmp_path) == "off"
    (d / "routing.json").write_text(json.dumps({"offload": "force"}))
    assert orr.load_board_rule("myboard", boards_root=tmp_path) == "force"


def test_excluded_boards_default_off(tmp_path):
    for b in ["balloon", "e2e-bench", "microfips", "llm-routing"]:
        assert orr.load_board_rule(b, boards_root=tmp_path) == "off", b


# ---------------------------------------------------------------------------
# route() — pure decision from injected facts (no network)
# ---------------------------------------------------------------------------

def _facts(decision="green", load=0.1, ram_mb=4096, disk_mb=200000):
    return [{
        "name": "dq05", "profile": "worker-dq05", "ssh_alias": "dq05",
        "reachable": decision != "down", "decision": decision,
        "load_per_core": load, "free_ram_mb": ram_mb, "free_disk_mb": disk_mb,
        "cores": 4, "source": "ssh", "opportunistic": False,
    }]


def _route(title, body="", rules=None, local_stress=None, target_facts=None):
    with tempfile.TemporaryDirectory() as td:
        # seed board rule if given
        rule = None
        if rules:
            for board, r in rules.items():
                bd = Path(td) / board
                bd.mkdir(parents=True, exist_ok=True)
                (bd / "routing.json").write_text(json.dumps({"offload": r}))
                if board == "board-x":
                    rule = r
        # route() is pure; board_rule derived from the seeded routing.json
        from offload_router import load_board_rule
        if rule is None:
            rule = load_board_rule("board-x", boards_root=td)
        return orr.route(
            "board-x", title, body,
            board_rule=rule,
            facts=target_facts,
            local_load_per_core=local_stress,
        )


def test_route_offloads_heavy_when_local_stressed_and_target_green():
    r = _route("cargo build the node", local_stress=3.5, target_facts=_facts())
    assert r["decision"] == "offload"
    assert r["profile"] == "worker-dq05"
    assert r["target"] == "dq05"


def test_route_keeps_local_when_local_idle():
    # auto rule: local NOT stressed -> no offload even if target green
    r = _route("cargo build the node", local_stress=1.1, target_facts=_facts())
    assert r["decision"] == "local"
    assert "not stressed" in r["reason"]


def test_route_local_when_probe_down():
    r = _route("cargo build the node", local_stress=4.0,
               target_facts=_facts("down"))
    assert r["decision"] == "local"
    assert "unreachable" in r["reason"] or "no green" in r["reason"]


def test_route_local_when_probe_error_no_facts():
    r = _route("cargo build the node", local_stress=4.0, target_facts=None)
    assert r["decision"] == "local"
    assert "probe" in r["reason"].lower() or "no green" in r["reason"]


def test_route_never_offloads_light():
    r = _route("search the codebase", local_stress=9.0, target_facts=_facts())
    assert r["decision"] == "local"


def test_route_never_offloads_hardware_even_when_forced():
    r = _route("flash the built image", local_stress=9.0, target_facts=_facts())
    assert r["decision"] == "local"
    assert "hardware" in r["reason"]


def test_route_board_off_never_offloads_even_when_stressed():
    r = _route("cargo build the node", local_stress=9.0, target_facts=_facts(),
               rules={"board-x": "off"})
    assert r["decision"] == "local"
    assert "off" in r["reason"]


def test_route_force_offloads_heavy_without_local_stress():
    r = _route("cargo build the node", local_stress=0.5, target_facts=_facts(),
               rules={"board-x": "force"})
    assert r["decision"] == "offload"
    assert r["profile"] == "worker-dq05"


def test_route_target_green_requires_headroom():
    # target load/core >= 1.0 -> NOT green -> local (fail-soft)
    r = _route("cargo build the node", local_stress=5.0,
               target_facts=_facts(load=1.4))
    assert r["decision"] == "local"
    assert "green" in r["reason"] or "headroom" in r["reason"]


def test_route_target_green_requires_ram():
    r = _route("cargo build the node", local_stress=5.0,
               target_facts=_facts(ram_mb=512))
    assert r["decision"] == "local"


def test_route_picks_first_green_in_pool_order():
    # dq05 down, t470 green, vps2 green -> picks t470? t470 is opportunistic
    # non-routable by default -> falls to vps2? vps2 also non-routable in
    # phase-1 defaults. With only dq05 routable, decision must be local.
    facts = [
        {"name": "dq05", "profile": "worker-dq05", "reachable": False,
         "decision": "down", "opportunistic": False},
        {"name": "t470", "profile": None, "reachable": True,
         "decision": "green", "opportunistic": True, "load_per_core": 0.2,
         "free_ram_mb": 8192, "free_disk_mb": 10000, "cores": 2},
    ]
    r = _route("cargo build the node", local_stress=5.0, target_facts=facts)
    assert r["decision"] == "local"  # no ROUTABLE green target in phase 1


# ---------------------------------------------------------------------------
# delegation body append
# ---------------------------------------------------------------------------

def test_delegation_body_snippet_names_ssh_alias():
    b = orr.delegation_body("dq05")
    assert "ssh dq05" in b
    assert "cargo" not in b


def test_body_append_is_idempotent_marker():
    body = "Do the thing."
    marker = orr.delegation_body("dq05")
    once = orr.append_delegation_body(body, "dq05")
    twice = orr.append_delegation_body(once, "dq05")
    assert once.count(marker) == 1
    assert twice.count(marker) == 1
    assert once.startswith(body)


# ---------------------------------------------------------------------------
# multi-target probe orchestration (mocked subprocess -> no network)
# ---------------------------------------------------------------------------

def test_probe_orchestrator_calls_capacity_probe_per_target(monkeypatch, tmp_path):
    calls = []

    def fake_run(env, timeout=None):
        calls.append(env.get("DQ05_CAP_SSH_HOST", "?"))
        # one dq05-capacity JSON line per target (returned as stdout text)
        return json.dumps({"reachable": True, "source": "ssh", "load": 0.2,
                           "load_per_core": 0.1, "cores": 4, "free_ram_mb": 4096,
                           "free_disk_mb": 200000, "local_load1": 0.4,
                           "local_cores": 2, "local_load_per_core": 0.2,
                           "decision": "OFFLOAD-OK", "reason": ""})

    monkeypatch.setattr(orr.probe, "_run_capacity_probe", fake_run)
    targets = [{"name": "dq05", "ssh_alias": "dq05", "cores": 4,
                "curl_hosts": "192.168.1.218", "profile": "worker-dq05",
                "opportunistic": False}]
    facts = orr.probe.probe_targets(targets, cache_ttl=0)
    assert calls == ["dq05"]
    assert facts[0]["name"] == "dq05"
    assert facts[0]["reachable"] is True
    assert facts[0]["decision"] == "green"


def test_probe_cache_ttl_short_circuits(monkeypatch, tmp_path):
    calls = {"n": 0}

    def fake_run(env, timeout=None):
        calls["n"] += 1
        return json.dumps({"reachable": True, "source": "ssh", "load": 0.2,
                           "load_per_core": 0.1, "cores": 4, "free_ram_mb": 4096,
                           "free_disk_mb": 200000, "local_load1": 0.4,
                           "local_cores": 2, "local_load_per_core": 0.2,
                           "decision": "OFFLOAD-OK", "reason": ""})

    monkeypatch.setattr(orr.probe, "_run_capacity_probe", fake_run)
    targets = [{"name": "dq05", "ssh_alias": "dq05", "cores": 4,
                "curl_hosts": "192.168.1.218", "profile": "worker-dq05",
                "opportunistic": False}]
    orr.probe.probe_targets(targets, cache_ttl=300)
    orr.probe.probe_targets(targets, cache_ttl=300)
    assert calls["n"] == 1  # second call served from cache


def test_probe_error_fails_soft_local(monkeypatch, tmp_path):
    def boom(env, timeout=None):
        raise RuntimeError("ssh refused")

    monkeypatch.setattr(orr.probe, "_run_capacity_probe", boom)
    targets = [{"name": "dq05", "ssh_alias": "dq05", "cores": 4,
                "curl_hosts": "192.168.1.218", "profile": "worker-dq05",
                "opportunistic": False}]
    facts = orr.probe.probe_targets(targets, cache_ttl=0)
    assert facts[0]["reachable"] is False
    assert facts[0]["decision"] == "error"


# ---------------------------------------------------------------------------
# assigner integration helper — board-preferred-over-stress fix
# ---------------------------------------------------------------------------

def test_pick_target_fix_board_preferred_over_stress():
    """(e) worker-dq05 with a stress/offload reason must beat an idle
    board-preferred profile whenever the router says offload."""
    route = {"decision": "offload", "profile": "worker-dq05",
             "target": "dq05", "reason": "local stressed"}
    idle = ["worker-admin", "worker-dq05"]   # board-preferred is ALSO idle
    chosen, why = orr.pick_assignment_target(
        route, board_preferred="worker-admin", idle_profiles=idle,
        hardware_local=False)
    assert chosen == "worker-dq05"
    assert "offload" in why


def test_pick_target_falls_back_to_board_preferred_when_no_offload():
    route = {"decision": "local", "profile": None, "target": None,
             "reason": "local idle"}
    idle = ["worker-admin", "worker-dq05"]
    chosen, why = orr.pick_assignment_target(
        route, board_preferred="worker-admin", idle_profiles=idle,
        hardware_local=False)
    assert chosen == "worker-admin"


def test_pick_target_never_offloads_to_missing_profile():
    route = {"decision": "offload", "profile": "worker-dq05",
             "target": "dq05", "reason": "stressed"}
    idle = ["worker-admin"]  # worker-dq05 NOT idle
    chosen, why = orr.pick_assignment_target(
        route, board_preferred="worker-admin", idle_profiles=idle,
        hardware_local=False)
    assert chosen == "worker-admin"  # fall back, don't stall
