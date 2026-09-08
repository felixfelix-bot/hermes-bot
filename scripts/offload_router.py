#!/usr/bin/env python3
"""offload_router.py — CANONICAL offload routing module (Phase 1, t_bd4d83c1).

Single source of truth for "should this CPU-heavy kanban task run on a remote
machine?" Consolidates the routing logic that previously lived in 3 divergent
copies of kanban_auto_assigner.py (manager/scripts, ~/.hermes/scripts,
bot/scripts/crons). Zero LLM tokens: deterministic classify + probe.

Pipeline (all zero-token):
    classify(title, body)                    -> kind: build|test|crunch|light|medium|hardware
    probe_targets(targets, ttl)              -> per-target facts (curl :9100 then ssh)
    route(board, title, body, ...)           -> decision local|offload + target/profile/reason

Decision rule (board policy off|auto|force):
  * hardware keywords (flash/serial/pio upload/bootsel/...) -> LOCAL, always
  * board routing.json "offload":"off"  -> LOCAL
  * not a heavy class (build/test/crunch) -> LOCAL
  * target pool probed in order DQ05 -> T470 -> VPS2; first GREEN *routable*
    target (profile set) wins
  * auto: OFFLOAD iff local load/core > 2.0 AND a green target exists
  * force: OFFLOAD iff a green target exists (ignore local load)
  * no green target / no probe facts -> LOCAL fail-soft (never stall the board)

Board escape hatches: ~/.hermes/kanban/boards/<board>/routing.json
    {"offload": "off"|"auto"|"force"}   (missing file => "auto" default;
     excluded hardware/token boards => "off" default)

Multi-target pool (probe decides green; opportunistic targets only used when
they report genuine spare capacity AND the task is short/bounded):
    dq05  : PRIMARY (ssh dq05, curl 192.168.1.218/100.90.22.201:9100)
    t470  : opportunistic service host (relays/routstr) — probe decides
    vps2  : opportunistic PAY-PER-USE host (23.182.128.51) — short/bounded only
Phase-1 routing only ever dispatches to worker-dq05 (DQ05); other green
targets are reported but not dispatched (no adapter worker exists yet).

Importers (do NOT copy this file — import it):
  * kanban_auto_assigner.py copies -> recommend_profile() delegates here
  * import-schedule-to-kanban.py   -> birth-time routing + body append
  * offload-sweep.py               -> 5-min no_agent sweep
"""
import json
import os
import re
import subprocess
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Keyword tables — three EXPLICIT heavy classes (operator extension 2026-09-08)
# ---------------------------------------------------------------------------

# build / compile: cargo, pio (run/build), tsc, go build, make, cmake,
# npm/yarn/pnpm/bun run build, gradle/maven compile
BUILD_KEYWORDS = [
    "cargo", "pio run", "pio build", "tsc", "go build", "make ",
    "cmake", "npm run build", "yarn build", "pnpm build", "bun run build",
    "bun build", "compile", "gradle", "mvn ", "cargo build", "cargo check",
    "cargo fmt --check",
]
# test suites: vitest, bun test, playwright, pytest, jest, cargo test,
# go test, "run tests", e2e, full suite, tdd
TEST_KEYWORDS = [
    "vitest", "bun test", "playwright", "pytest", "jest", "cargo test",
    "go test", "run tests", "run the tests", "test suite", "full suite",
    "e2e", "integration test", "unit test", "tdd",
]
# data crunching: analysis scripts, plot, parse, convert, aggregate,
# transform, benchmark, simulate
CRUNCH_KEYWORDS = [
    "analysis", "analyze", "plot", "parse", "convert", "aggregate",
    "transform", "benchmark", "simulate", "data crunch", "crunch",
    "backfill", "extract", "process the log", "process logs",
]
LIGHT_KEYWORDS = [
    "search", "lookup", "find", "grep", "list", "status", "check", "read",
    "view", "show", "ls", "cat", "report", "summary",
]
# Hardware keywords FORCE local — a board/device in the loop can never be
# offloaded (no remote flash/serial).
HARDWARE_KEYWORDS = [
    "flash", "serial", "pio upload", "bootsel", "uart", "i2c", "spi",
    "solder", "oscilloscope", "jtag", "logic analyzer", "usb device",
    "pio run -t upload", "board is connected", "probe the board",
    "schematic", "pcb", "breadboard",
]

# Boards whose work is hardware/token-bound: NEVER auto-offloaded unless a
# routing.json explicitly sets "offload":"force".
EXCLUDED_BOARDS = {"balloon", "e2e-bench", "microfips", "llm-routing"}

DEFAULT_BOARDS_ROOT = Path.home() / ".hermes" / "kanban" / "boards"

# Phase-1 target pool. profile=None means "probe/report but do NOT dispatch
# (no adapter worker yet)".
DEFAULT_TARGET_POOL = [
    {
        "name": "dq05",
        "profile": "worker-dq05",
        "ssh_alias": "dq05",
        "cores": 4,
        "curl_hosts": "192.168.1.218 100.90.22.201",
        "opportunistic": False,
    },
    {
        "name": "t470",
        "profile": None,          # service host — no adapter worker yet
        "ssh_alias": "t470",
        "cores": 2,
        "curl_hosts": "",
        "opportunistic": True,
    },
    {
        "name": "vps2",
        "profile": None,          # pay-per-use — no adapter worker yet
        "ssh_alias": "testserver2",
        "cores": 2,
        "curl_hosts": "23.182.128.51",
        "opportunistic": True,
    },
]

# Probe thresholds (match dq05-capacity.sh decision rule)
LOCAL_STRESS_LOAD_PER_CORE = 2.0
TARGET_MAX_LOAD_PER_CORE = 1.0
TARGET_MIN_FREE_RAM_MB = 2048
TARGET_MIN_FREE_DISK_MB = 2048

CAPACITY_PROBE = Path(__file__).resolve().parent / "dq05-capacity.sh"


# ---------------------------------------------------------------------------
# classify()
# ---------------------------------------------------------------------------

def _hit(text, keywords):
    """Substring match but require word boundaries for short bare tokens so
    'read' doesn't fire on 'readme', 'spi' on 'despite', etc."""
    hits = []
    for kw in keywords:
        if " " not in kw and len(kw) <= 8:
            if re.search(rf"\b{re.escape(kw)}\b", text):
                hits.append(kw)
        else:
            if kw in text:
                hits.append(kw)
    return hits


def classify(title, body=""):
    """Return {kind, heavy, matched}. kind is one of:
    hardware, build, test, crunch, light, medium.

    Order matters: hardware > test > build > crunch > light > medium.
    'cargo test' must classify test (not build), hence TEST before BUILD.
    """
    text = (str(title) + " " + str(body)).lower()
    hw = _hit(text, HARDWARE_KEYWORDS)
    if hw:
        return {"kind": "hardware", "heavy": False, "matched": hw}
    t = _hit(text, TEST_KEYWORDS)
    if t:
        return {"kind": "test", "heavy": True, "matched": t}
    b = _hit(text, BUILD_KEYWORDS)
    if b:
        return {"kind": "build", "heavy": True, "matched": b}
    c = _hit(text, CRUNCH_KEYWORDS)
    if c:
        return {"kind": "crunch", "heavy": True, "matched": c}
    l = _hit(text, LIGHT_KEYWORDS)
    if l:
        return {"kind": "light", "heavy": False, "matched": l}
    return {"kind": "medium", "heavy": False, "matched": []}


# ---------------------------------------------------------------------------
# Board routing.json escape hatches
# ---------------------------------------------------------------------------

def load_board_rule(board, boards_root=None):
    """Return 'off' | 'auto' | 'force' for a board. Reads
    <boards_root>/<board>/routing.json {"offload": ...}; missing file uses
    EXCLUDED_BOARDS default ('off') else 'auto'."""
    if board in EXCLUDED_BOARDS:
        default = "off"
    else:
        default = "auto"
    root = Path(boards_root) if boards_root else DEFAULT_BOARDS_ROOT
    try:
        rule_file = root / board / "routing.json"
        if not rule_file.exists():
            return default
        with open(rule_file) as f:
            rule = json.load(f).get("offload", default)
        return rule if rule in ("off", "auto", "force") else default
    except Exception:
        return default


# ---------------------------------------------------------------------------
# probe_targets() — multi-target health facts, cached 30s
# ---------------------------------------------------------------------------

class _Probe:
    """Probe orchestration. `_run_capacity_probe` is swappable for tests."""

    def __init__(self):
        self._cache = {}

    def _run_capacity_probe(self, env, timeout=6):
        """Run dq05-capacity.sh with env overrides; returns stdout text."""
        env = {k: str(v) for k, v in env.items()}
        merged = dict(os.environ)
        merged.update(env)
        proc = subprocess.run(
            [str(CAPACITY_PROBE)],
            capture_output=True, text=True, timeout=timeout, env=merged,
        )
        # last non-empty stdout line is the JSON verdict
        lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
        return lines[-1] if lines else ""

    def probe_target(self, target, cache_ttl=30):
        """Probe one target; return normalized fact dict."""
        name = target["name"]
        now = time.time()
        cached = self._cache.get(name)
        if cache_ttl and cached and (now - cached["ts"]) < cache_ttl:
            return cached["fact"]
        env = {
            "DQ05_CAP_SSH_HOST": target.get("ssh_alias", name),
            "DQ05_CAP_DQ05_CORES": target.get("cores", 2),
            "DQ05_CAP_CURL_HOSTS": target.get("curl_hosts", ""),
            "DQ05_CAP_SELF_CONTAINED": "1",
            "DQ05_CAP_SSH_TIMEOUT": "4",
            "DQ05_CAP_CURL_TIMEOUT": "3",
        }
        fact = {
            "name": name,
            "profile": target.get("profile"),
            "ssh_alias": target.get("ssh_alias", name),
            "opportunistic": target.get("opportunistic", False),
            "reachable": False,
            "decision": "error",          # error|green|not_green|down
            "load_per_core": None,
            "free_ram_mb": None,
            "free_disk_mb": None,
            "source": "none",
            "reason": "",
        }
        try:
            out = self._run_capacity_probe(env)
            data = json.loads(out)
            fact["reachable"] = bool(data.get("reachable", False))
            fact["source"] = data.get("source", "none")
            fact["load_per_core"] = data.get("load_per_core")
            fact["free_ram_mb"] = data.get("free_ram_mb")
            fact["free_disk_mb"] = data.get("free_disk_mb")
            fact["cores"] = data.get("cores")
            if not fact["reachable"]:
                fact["decision"] = "down"
                fact["reason"] = data.get("reason", "unreachable")
            elif data.get("decision") == "OFFLOAD-OK":
                fact["decision"] = "green"
                fact["reason"] = "OFFLOAD-OK"
            else:
                fact["decision"] = "not_green"
                fact["reason"] = data.get("reason", "target not green")
        except Exception as e:  # noqa: BLE001 — probe error => fail-soft
            fact["decision"] = "error"
            fact["reason"] = f"probe error: {e}"
        if cache_ttl:
            self._cache[name] = {"ts": now, "fact": fact}
        return fact

    def probe_targets(self, targets=None, cache_ttl=30):
        """Probe the target pool in order; return list of fact dicts."""
        if targets is None:
            targets = DEFAULT_TARGET_POOL
        return [self.probe_target(t, cache_ttl=cache_ttl) for t in targets]


probe = _Probe()


def first_green(facts):
    """First fact that is reachable+green AND has a dispatchable profile AND
    satisfies capacity thresholds (guard against a fact that claims green but
    carries bad numbers)."""
    for f in facts:
        if f["decision"] != "green" or not f["profile"]:
            continue
        lp = f.get("load_per_core")
        if lp is not None and lp >= TARGET_MAX_LOAD_PER_CORE:
            continue
        ram = f.get("free_ram_mb")
        if ram is not None and ram < TARGET_MIN_FREE_RAM_MB:
            continue
        disk = f.get("free_disk_mb")
        if disk is not None and disk < TARGET_MIN_FREE_DISK_MB:
            continue
        return f
    return None


# ---------------------------------------------------------------------------
# route() — pure decision (facts injected for hermetic tests)
# ---------------------------------------------------------------------------

def route(board, title, body="", board_rule=None, facts=None,
          local_load_per_core=None, target_pool=None):
    """Decide local vs offload. Pure: board_rule, facts and
    local_load_per_core may be injected (tests inject); when facts is None
    the caller is expected to have probed separately and route() fails-soft
    to LOCAL (it never performs I/O itself). Callers: offload-sweep.py and
    import-schedule-to-kanban.py probe first, then call route() with facts.
    Returns {decision: local|offload, profile, target, reason}."""
    cls = classify(title, body)
    if cls["kind"] == "hardware":
        return {"decision": "local", "profile": None, "target": None,
                "reason": f"hardware task ({cls['matched'][0]}) must run local"}
    rule = board_rule if board_rule is not None else load_board_rule(board)
    if rule == "off":
        return {"decision": "local", "profile": None, "target": None,
                "reason": f"board {board} routing.json offload=off"}
    if not cls["heavy"]:
        return {"decision": "local", "profile": None, "target": None,
                "reason": f"task kind={cls['kind']} is not heavy"}
    if facts is None:
        # No probe facts supplied: caller must probe. Fail-soft local rather
        # than dispatch blind.
        return {"decision": "local", "profile": None, "target": None,
                "reason": "no probe facts supplied; fail-soft local"}
    green = first_green(facts)
    if green is None:
        return {"decision": "local", "profile": None, "target": None,
                "reason": "no green dispatchable target; fail-soft local"}
    if rule == "force":
        return {"decision": "offload", "profile": green["profile"],
                "target": green["name"],
                "reason": f"board force + {green['name']} green"}
    # auto: require local stress
    if local_load_per_core is None:
        local_load_per_core = _local_load_per_core()
    if local_load_per_core <= LOCAL_STRESS_LOAD_PER_CORE:
        return {"decision": "local", "profile": None, "target": None,
                "reason": f"local load/core {local_load_per_core:.2f} not "
                          f"stressed (> {LOCAL_STRESS_LOAD_PER_CORE})"}
    return {"decision": "offload", "profile": green["profile"],
            "target": green["name"],
            "reason": f"local load/core {local_load_per_core:.2f} stressed "
                      f"&& {green['name']} green"}


def _local_load_per_core():
    try:
        with open("/proc/loadavg") as f:
            load1 = float(f.read().split()[0])
        cores = os.cpu_count() or 1
        return load1 / max(1, cores)
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# assignment helper — fixes board-preferred-over-stress bug (scope item e)
# ---------------------------------------------------------------------------

def pick_assignment_target(route_result, board_preferred, idle_profiles,
                           hardware_local=False,
                           remote_profiles=("worker-dq05",)):
    """Given a route result + idle worker set, choose the profile to assign.

    FIX (e): when the router says OFFLOAD (stress reason), worker-dq05 must
    BEAT an idle board-preferred profile — the old code always let the idle
    board profile win, so worker-dq05 was fallback-only and offload never
    happened. Only when no offload applies do we fall back to the
    board-preferred profile, then worker-base, then any idle worker.

    remote_profiles (worker-dq05 and friends) are ONLY ever chosen via an
    explicit offload verdict; they are excluded from the generic fallbacks so
    a light/hardware task never lands on the remote-compute adapter.
    """
    idle = set(idle_profiles or ())
    if (not hardware_local
            and route_result["decision"] == "offload"
            and route_result["profile"] in idle):
        return (route_result["profile"],
                f"offload_route ({route_result['reason']})")
    nonremote = idle - set(remote_profiles)
    if board_preferred in nonremote:
        return (board_preferred, "board_preferred_idle")
    if "worker-base" in nonremote:
        return ("worker-base", "fallback_base")
    for name in sorted(nonremote):
        if name.startswith("worker-"):
            return (name, "any_idle_worker")
    return (None, "no_idle_local_worker")


# ---------------------------------------------------------------------------
# delegation-body append (root cause 5 fix)
# ---------------------------------------------------------------------------

_DELEGATION_TMPL = (
    "\n\nOFFLOAD EXECUTION (auto-appended by offload_router): this task is "
    "classified CPU-heavy. Run ALL build/compile/test/data-crunch steps on "
    "{target} via: ssh {alias} '<command>'. DQ05 repos are pre-cloned under "
    "~/repos on the target. Commit+push from your side after remote "
    "execution; never run the heavy step on the local T470 box."
)


def delegation_body(alias="dq05", target="DQ05"):
    return _DELEGATION_TMPL.format(alias=alias, target=target)


def append_delegation_body(body, alias="dq05", target="DQ05"):
    """Idempotently append the delegation snippet to a task body."""
    snippet = delegation_body(alias, target)
    if snippet in body:
        return body
    return body + snippet


if __name__ == "__main__":
    # CLI: classify a task
    import sys as _sys
    if len(_sys.argv) >= 3:
        res = classify(_sys.argv[1], _sys.argv[2])
        print(json.dumps(res))
    else:
        print(json.dumps(classify("", " ".join(_sys.argv[1:]))))
