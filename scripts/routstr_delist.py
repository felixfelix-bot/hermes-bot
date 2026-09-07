#!/usr/bin/env python3
"""src/routstr_delist.py — T-D routstr dynamic delist (ADR-007 Gate 4).

Pulls the routstr SALE (the advertised model listing on the routstr sell node)
programmatically within 5 minutes of a trigger, WITHOUT disabling the routstr
provider in the T470 flat-router ladder.

Distinction that matters (task body):
    delisting the routstr SALE  !=  disabling the routstr provider.
    The provider stays in the ladder (still routable / still a candidate);
    only the PUBLIC advertisement of models-for-sale is pulled.

Triggers (ADR-007):
    1. Burn-rate accelerates  — predicted exhaustion moves FORWARD
       (hours-to-exhaustion decreases) past a threshold between consecutive
       readings of burn_predictor.predict_exhaustion().
    2. Kalman variance jumps  — a prediction window's `uncertainty` (the
       Kalman filter's variance/uncertainty term) exceeds a threshold.
    3. Manual override        — a marker file / --manual flag.

Implementation (task note): "a flag in the objects that publishes the listing,
plus a websocket/event-based delist." Concretely:
  * flag  — a durable delist flag in the state/kalman state the listing
            publisher publishes (road `delisted` on the publish state) so the
            publisher's advertisement reflects delisted=true.
  * event — an event-based delist: the script emits a kind-30315
            `d=routstr-delist` Nostr event (via nak) that sell-side consumers
            react to, in addition to the direct SSH delist of the sell node.

The remote delist targets the sell node's `models` table only (enabled=0 for
all advertised rows) — it never touches upstream_providers or the T470 router.

The module is deliberately split so every trigger/decision/state/helper is
pure and unit-testable (Gate 1 TDD); the only side-effecting function is the
backend (SSH docker exec on the sell node), which is injected so tests stay
hermetic.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

# ── Thresholds / constants ────────────────────────────────────────────────
EXHAUST_MOVE_HOURS = 2.0       # predicted exhaustion moved forward by >= this many
                               # hours -> burn-acceleration trigger (default)
VARIANCE_THRESHOLD = 0.30      # Kalman `uncertainty` above this -> variance-jump trigger
TRIGGER_ACTION_WINDOW_S = 300  # 5 minutes: script must pull within this of a trigger
DELIST_EVENT_KIND = 30315
DELIST_EVENT_TAG = "routstr-delist"

_DEFAULT_STATE_PATH = os.path.expanduser("~/.hermes/bot/routstr_delist_state.json")
_DEFAULT_OVERRIDE_PATH = os.path.expanduser("~/.hermes/bot/routstr_delist.override")
_DEFAULT_SSH_TARGET = "debian@23.182.128.51"   # VPS2 (routstr-public sell node)
_DEFAULT_CONTAINER = "routstr-public"
_DEFAULT_NAK = os.path.expanduser("~/.local/bin/nak")


# ── Trigger 1: burn-rate accelerates (predicted exhaustion moves forward) ──
def exhaustion_accelerated(preds_now, preds_prev, *, move_hours=EXHAUST_MOVE_HOURS):
    """List of windows whose predicted exhaustion moved FORWARD (hours left
    decreased) by `move_hours` or more between the previous and current
    prediction sets.

    Args:
        preds_now:  list of prediction dicts (burn_predictor.predict_exhaustion
                    shape: key, window, exhausts_in_hours, uncertainty, ...).
        preds_prev: previous list of prediction dicts, or [] / None if none.
        move_hours: minimum hours-forward movement to count as acceleration.

    Returns a list of dicts:
        {"key", "window", "prev_hours", "now_hours"}
    Empty when there is no acceleration (or no baseline to compare).
    """
    if not preds_now or not preds_prev:
        return []

    prev_map = {_pred_key(p): _hours(p) for p in preds_prev if _hours(p) is not None}
    fired = []
    for p in preds_now:
        h_now = _hours(p)
        if h_now is None:
            continue
        h_prev = prev_map.get(_pred_key(p))
        if h_prev is None:
            continue
        # hours left went DOWN => exhaustion got SOONER => moved forward.
        moved = h_prev - h_now
        if moved >= move_hours:
            fired.append({
                "key": p.get("key"),
                "window": p.get("window"),
                "prev_hours": h_prev,
                "now_hours": h_now,
            })
    return fired


# ── Trigger 2: Kalman variance jumps ──────────────────────────────────────
def variance_jumped(preds, *, threshold=VARIANCE_THRESHOLD, key=None):
    """List of prediction windows whose Kalman `uncertainty` (variance term)
    exceeds `threshold`.

    Args:
        preds:     list of prediction dicts.
        threshold: uncertainty above which a jump is flagged.
        key:       optional filter — only check predictions for this key.

    Returns list of dicts {"key", "window", "uncertainty"}.
    """
    if not preds:
        return []
    fired = []
    for p in preds:
        if key is not None and p.get("key") != key:
            continue
        unc = p.get("uncertainty")
        if unc is None:
            continue
        if unc > threshold:
            fired.append({
                "key": p.get("key"),
                "window": p.get("window"),
                "uncertainty": unc,
            })
    return fired


# ── Trigger 3: manual override ────────────────────────────────────────────
def manual_override(override_path):
    """True when a manual-override marker file exists.

    Presence of the marker (not its contents) is the override. A None path
    or a missing/unreadable path is never an override.
    """
    if not override_path:
        return False
    return os.path.isfile(override_path)


# ── Snapshot / state helpers ──────────────────────────────────────────────
def _pred_key(p):
    return f"{p.get('key')}|{p.get('window')}"


def _hours(p):
    """Return exhausts_in_hours or None when unusable."""
    h = p.get("exhausts_in_hours")
    return h if isinstance(h, (int, float)) else None


def snapshot_preds(preds):
    """Collapse a prediction list into {key|window: exhausts_in_hours} for a
    durable per-window baseline used on the NEXT comparison."""
    snap = {}
    if preds:
        for p in preds:
            h = _hours(p)
            if h is not None:
                snap[_pred_key(p)] = h
    return snap


def load_state(path):
    """Load the durable state dict; never raises — returns {} on any failure."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(path, state):
    """Persist state dict; returns True on success, False on any error."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
        return True
    except Exception:
        return False


def update_state(state, *, ts, preds, delisted, last_reason=None):
    """Return a NEW state dict carrying the next baseline + delist flag.

    The `delisted` field is the "flag in the objects that publishes the
    listing": the listing publisher reads this durable flag so its
    advertisement reflects delisted=true.
    """
    new = dict(state or {})
    new["ts"] = ts
    new["snapshot"] = snapshot_preds(preds)
    new["delisted"] = bool(delisted)
    if last_reason is not None:
        new["last_reason"] = last_reason
    return new


# ── Decision assembly ─────────────────────────────────────────────────────
def evaluate_triggers(preds_now, prev_state, *, override=False,
                      move_hours=EXHAUST_MOVE_HOURS,
                      variance_threshold=VARIANCE_THRESHOLD):
    """Assemble a single delist decision from all three trigger sources.

    Args:
        preds_now:  current burn predictor predictions (list of dicts).
        prev_state: prior run's state dict ({"snapshot": {...}} or None on
                    first run / no baseline).
        override:   whether a manual override is active.
        move_hours / variance_threshold: trigger thresholds.

    Returns a decision dict:
        {"fire": bool, "reasons": [str, ...], "triggers": {...}}
    A first run with no baseline NEVER fires on acceleration (it would be a
    false positive); it can still fire on variance or manual override.
    """
    prev_snapshot = {}
    if isinstance(prev_state, dict):
        s = prev_state.get("snapshot")
        if isinstance(s, dict):
            prev_snapshot = s
    # Rebuild a list of previous pred dicts from the snapshot so the pure
    # comparison function needs no special casing.
    preds_prev = [{"key": k.split("|")[0], "window": k.split("|")[1]
                   if "|" in k else "unknown", "exhausts_in_hours": v}
                  for k, v in prev_snapshot.items()]

    accel = exhaustion_accelerated(preds_now, preds_prev, move_hours=move_hours)
    var = variance_jumped(preds_now, threshold=variance_threshold)

    reasons = []
    if accel:
        reasons.append(f"exhaustion accelerated ({len(accel)} window(s): "
                       + ", ".join(f"{a['key']}/{a['window']} {a['now_hours']:.1f}h"
                                   for a in accel) + ")")
    if var:
        reasons.append(f"kalman variance jumped ({len(var)} window(s), max "
                       + f"{max(v['uncertainty'] for v in var):.2f})")
    if override:
        reasons.append("manual override")

    return {
        "fire": bool(accel or var or override),
        "reasons": reasons,
        "triggers": {"accel": accel, "variance": var, "manual": override},
    }


# ── Listing action (backend-injected; provider stays in ladder) ───────────
def pull_listing(backend, *, dry_run=False):
    """Pull the routstr SALE listing on the sell node.

    Delegates to `backend` so the remote/SSH path is swappable in tests.
    `backend` must expose pull(**kw) and (for dry-run) dry_run_pull(**kw).

    Returns backend result dict.
    """
    if dry_run and hasattr(backend, "dry_run_pull"):
        return backend.dry_run_pull(dry_run=True)
    return backend.pull(dry_run=dry_run)


def relist(backend, *, dry_run=False):
    """Restore the routstr SALE listing on the sell node."""
    if dry_run and hasattr(backend, "relist"):
        return backend.relist(dry_run=True)
    return backend.relist(dry_run=dry_run)


# ── SQL / command shaping (hermetic — no network) ─────────────────────────
def pull_listing_sql():
    """SQL that pulls the advertised listing: disable every advertised model.

    Scoped STRICTLY to the `models` table's `enabled` flag. It deliberately
    does NOT touch upstream_providers or any router provider row — the
    provider stays in the ladder (task body).
    """
    return "UPDATE models SET enabled=0 WHERE enabled=1;"


def relist_sql():
    """SQL that restores the advertised listing."""
    return "UPDATE models SET enabled=1;"


def build_ssh_docker_cmd(ssh_target, container, sql):
    """Build the shell command that runs `sql` inside the sell node container.

    Returns a list suitable for subprocess. Quoting is defensive: the SQL is
    passed as a docker exec python3 -c argument (single-quoted shell segment).
    """
    python = ("import sqlite3;db=sqlite3.connect('/app/data/keys.db');"
              f"db.execute({sql!r});db.commit();print('OK')")
    return [
        "ssh", ssh_target,
        "docker", "exec", container, "python3", "-c", python,
    ]


# ── Event-based delist signal (kind-30315 via nak) ────────────────────────
def build_delist_event(sec_file, *, nsec_env="NOSTR_SECRET_KEY",
                       nak=_DEFAULT_NAK, relays=None, delisted=True):
    """Return the argv for a `nak` kind-30315 `d=routstr-delist` publish.

    Uses the env-var secret path (never --sec — see routstr-node-ops skill:
    --sec exposes the key in ps aux). Returns None when the nsec file is
    missing. Hermetic — only builds argv, does not run it.
    """
    if not sec_file or not os.path.isfile(sec_file):
        return None
    rel = relays or ["wss://relay.primal.net", "wss://nos.lol", "wss://relay.damus.io"]
    content = json.dumps({"delisted": delisted, "ts": time.time(),
                          "tag": DELIST_EVENT_TAG}, separators=(",", ":"))
    argv = [nak, "event", "--kind", str(DELIST_EVENT_KIND),
            "--tag", f"d:{DELIST_EVENT_TAG}",
            "--tag", "t:routstr",
            "--content", content,
            "--env-sec"]
    argv += rel
    return argv


# ── CLI ───────────────────────────────────────────────────────────────────
def _read_preds(pred_input):
    """Parse current predictions: either a JSON list from a file path, or []
    (caller may also pre-load via --json for testing). Never raises."""
    if not pred_input:
        return []
    try:
        with open(pred_input, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="routstr_delist.py",
        description="Pull the routstr SALE listing within 5 min of a trigger "
                    "(burn acceleration / kalman variance jump / manual override). "
                    "The routstr provider stays in the ladder.",
    )
    ap.add_argument("--state", default=_DEFAULT_STATE_PATH,
                    help="durable trigger-baseline state JSON")
    ap.add_argument("--override", default=_DEFAULT_OVERRIDE_PATH,
                    help="manual override marker path")
    ap.add_argument("--preds", default=None,
                    help="path to a JSON list of current predictions "
                         "(omit to run with no live baseline)")
    ap.add_argument("--move-hours", type=float, default=EXHAUST_MOVE_HOURS,
                    help="exhaustion must move forward by this many hours to fire")
    ap.add_argument("--variance", type=float, default=VARIANCE_THRESHOLD,
                    help="kalman uncertainty above this fires")
    ap.add_argument("--manual", action="store_true",
                    help="force a manual delist trigger")
    ap.add_argument("--delist", action="store_true",
                    help="force-pull the listing now (ignore triggers)")
    ap.add_argument("--relist", action="store_true",
                    help="restore the listing")
    ap.add_argument("--status", action="store_true",
                    help="print current delist state and exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="do not execute the remote delist (print intent)")
    ap.add_argument("--ssh-target", default=_DEFAULT_SSH_TARGET)
    ap.add_argument("--container", default=_DEFAULT_CONTAINER)
    ap.add_argument("--nsec", default=None,
                    help="path to a Nostr nsec file for the event-based delist "
                         "signal (optional)")
    args = ap.parse_args(argv)

    state = load_state(args.state)

    if args.status:
        print(json.dumps({"delisted": state.get("delisted", False),
                          "ts": state.get("ts"),
                          "last_reason": state.get("last_reason"),
                          "snapshot_windows": len(state.get("snapshot", {}))},
                         indent=2, sort_keys=True))
        return 0

    override = args.manual or manual_override(args.override)
    preds = _read_preds(args.preds)

    # Re-enable: --relist overrides everything.
    if args.relist:
        res = relist(_SshBackend(args.ssh_target, args.container),
                     dry_run=args.dry_run)
        state = update_state(state, ts=time.time(), preds=preds,
                             delisted=False, last_reason="relist")
        save_state(args.state, state)
        print(json.dumps({"action": "relist", **res}, indent=2, sort_keys=True))
        return 0

    dec = evaluate_triggers(preds, state, override=override,
                            move_hours=args.move_hours,
                            variance_threshold=args.variance)
    fire = args.delist or dec["fire"]

    if not fire:
        state = update_state(state, ts=time.time(), preds=preds,
                             delisted=state.get("delisted", False))
        save_state(args.state, state)
        print(json.dumps({"action": "none", "fire": False,
                          "reasons": [], "ts": time.time()}, indent=2, sort_keys=True))
        return 0

    # Fire: pull the listing.
    res = pull_listing(_SshBackend(args.ssh_target, args.container),
                       dry_run=args.dry_run)
    state = update_state(state, ts=time.time(), preds=preds, delisted=True,
                         last_reason="; ".join(dec["reasons"] or ["--delist"]))
    save_state(args.state, state)

    # Event-based delist signal (best-effort, never blocks the pull result).
    if args.nsec:
        ev = build_delist_event(args.nsec)
        if ev:
            try:
                with open(args.nsec) as _fh:
                    _nsec_val = _fh.read().strip()
                subprocess.run(ev, timeout=30,
                               env=dict(os.environ, NOSTR_SECRET_KEY=_nsec_val),
                               capture_output=True)
            except Exception:
                pass

    print(json.dumps({"action": "delist", "fire": True, **res,
                      "reasons": dec["reasons"]}, indent=2, sort_keys=True))
    return 0


class _SshBackend:
    """Production backend: docker exec python3 against the sell node over ssh.

    Only ever mutates the `models` table (the advertised listing). Never
    touches upstream_providers — the provider stays in the ladder.
    """

    def __init__(self, ssh_target, container):
        self.ssh_target = ssh_target
        self.container = container

    def pull(self, dry_run=False):
        return self._run(pull_listing_sql(), dry_run=dry_run, action="pull")

    def relist(self, dry_run=False):
        return self._run(relist_sql(), dry_run=dry_run, action="relist")

    def _run(self, sql, *, dry_run, action):
        cmd = build_ssh_docker_cmd(self.ssh_target, self.container, sql)
        if dry_run:
            return {"ok": True, "dry_run": True, "action": action,
                    "cmd": " ".join(cmd[:3]) + " … " + cmd[-1]}
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            ok = proc.returncode == 0 and "OK" in proc.stdout
            return {"ok": ok, "action": action, "rc": proc.returncode,
                    "stdout": proc.stdout.strip()[:200],
                    "stderr": proc.stderr.strip()[:200]}
        except Exception as exc:  # ssh/docker unavailable or slow
            return {"ok": False, "action": action, "error": str(exc)}


if __name__ == "__main__":
    raise SystemExit(main())
