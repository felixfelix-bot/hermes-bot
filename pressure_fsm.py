#!/usr/bin/env python3
"""pressure_fsm — GREEN/AMBER/RED pressure band FSM, shadow mode (S2b, t_4dfaf0d).

Implements the Layer-2 request-time half of
docs/DESIGN-two-layer-pressure-routing.md (~/merchant-routing-engine), Stage 2b:

  * Band computer (D3): friend-key 5h-window used_pct + Kalman
    exhausts_in_hours (uncertainty-adjusted, predictive) + monthly-window
    floor-raiser.  Asymmetric hysteresis — escalate 60/75, de-escalate
    45/60 — with a 10-minute dwell that gates DE-escalation only
    (escalation is immediate: pressure is a safety direction).
  * SHADOW ONLY: this module never reroutes a live request.  It computes
    and logs the decision it WOULD make into pressure_decisions
    (zai_usage.db) with stable reason codes (D8).  Enforce mode is a
    later stage (S2c+); `mode=enforce` is accepted but behaves like
    shadow here.
  * Interactive classifier (D4 v1 heuristic): a request is interactive
    iff its X-Hermes-Session had a prior api_calls row within 10 min.
    Interactive traffic is NEVER downgraded, and a downgrade is NEVER
    routed to a paid provider (only friend or flat-rate ollama_cloud).
  * Kill switch (D9): touch ~/.hermes/bot/.pressure_routing_disabled,
    or set pressure_policy.json mode=off — everything inert.
  * Ollama flat-rate capacity (D6): the tracker's regime
    (included/extra/exhausted/paywalled) is folded into a single
    flat_rate_capacity input: ok | ollama_extra | friend_only | none.

State persistence: pressure_state.json (NOT zai_proxy_state.json — that
file is wholesale-rewritten by the proxy's _refresh_loop every cycle, so
keys added there would be clobbered; deviation from design doc D3, noted
in the task report).

All DB/policy/state I/O is wrapped: a failure in this module must never
break request handling.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

GREEN, AMBER, RED = "GREEN", "AMBER", "RED"
_ORDER = {GREEN: 0, AMBER: 1, RED: 2}

# Ollama regimes that remove ollama as a downgrade target entirely.
_NO_OLLAMA_REGIMES = {"exhausted", "paywalled"}

DEFAULT_POLICY: dict = {
    "mode": "shadow",                 # shadow | enforce | off  (S2b: shadow only)
    "escalate_amber_pct": 60.0,       # GREEN -> AMBER
    "escalate_red_pct": 75.0,         # AMBER -> RED
    "deescalate_amber_pct": 60.0,     # RED -> AMBER (<=)
    "deescalate_green_pct": 45.0,     # AMBER -> GREEN (<=)
    "dwell_seconds": 600,             # 10-min anti-flap dwell on de-escalation
    "predictive_amber_hours": 3.0,    # will_exhaust && (exh-unc) <= 3.0 -> AMBER
    "predictive_red_hours": 1.0,      # will_exhaust && (exh-unc) <= 1.0 -> RED
    "monthly_floor_raiser_pct": 85.0, # monthly >= 85 shifts thresholds down...
    "threshold_shift_pp": 10.0,       # ...by this many percentage points
    "interactive_window_seconds": 600,# session seen within 10 min => interactive
}

BOT_DIR = Path.home() / ".hermes" / "bot"


@dataclass
class PressureInputs:
    """One observation of every pressure dimension (all read-only)."""
    used_pct_5h: float | None = None          # friend key, 5h window
    exhausts_in_hours: float | None = None    # Kalman prediction
    uncertainty: float | None = None          # Kalman uncertainty (hours)
    will_exhaust: bool = False                # Kalman will_exhaust flag
    used_pct_monthly: float | None = None     # friend key, monthly window
    ollama_regime: str | None = None          # included|extra|exhausted|paywalled
    friend_locked: bool = False               # proxy LOCK_THRESHOLDS verdict

    def snapshot(self) -> dict:
        return dict(
            used_pct_5h=self.used_pct_5h,
            exhausts_in_hours=self.exhausts_in_hours,
            uncertainty=self.uncertainty,
            will_exhaust=bool(self.will_exhaust),
            used_pct_monthly=self.used_pct_monthly,
            ollama_regime=self.ollama_regime,
            friend_locked=self.friend_locked,
        )


@dataclass
class Decision:
    """A would-be routing decision (shadow mode: never applied)."""
    requested_model: str
    would_serve_model: str
    would_provider: str | None
    state: str
    interactive: bool
    reason: str


class PressureTracker:
    """Thread-safe-ish pressure FSM: compute band, decide (shadow), log.

    Every public method swallows its own I/O errors — the tracker can
    never be the reason a proxied request fails.
    """

    def __init__(self, bot_dir: Path | str | None = None, *,
                 db_path: Path | str | None = None,
                 state_path: Path | str | None = None,
                 policy_path: Path | str | None = None,
                 flag_path: Path | str | None = None,
                 now: Callable[[], float] | None = None):
        root = Path(bot_dir) if bot_dir else BOT_DIR
        self.db_path = Path(db_path) if db_path else root / "zai_usage.db"
        self.state_path = Path(state_path) if state_path else root / "pressure_state.json"
        self.policy_path = Path(policy_path) if policy_path else root / "pressure_policy.json"
        self.flag_path = Path(flag_path) if flag_path else root / ".pressure_routing_disabled"
        self._now = now or time.time

    # ── policy / kill switch ────────────────────────────────────────────

    def _policy(self) -> dict:
        pol = dict(DEFAULT_POLICY)
        try:
            data = json.loads(self.policy_path.read_text())
            if isinstance(data, dict):
                pol.update({k: v for k, v in data.items() if k in pol})
        except Exception:
            pass  # missing/corrupt policy -> defaults (mode=shadow)
        return pol

    def mode(self) -> str:
        mode = self._policy().get("mode", "shadow")
        return mode if mode in ("shadow", "enforce", "off") else "shadow"

    def enabled(self) -> bool:
        """Kill switches: flag file present OR mode=off -> inert."""
        try:
            if self.flag_path.exists():
                return False
        except Exception:
            return False
        return self.mode() != "off"

    # ── thresholds ──────────────────────────────────────────────────────

    def _thresholds(self, inputs: PressureInputs) -> dict:
        pol = self._policy()
        shift = 0.0
        monthly = inputs.used_pct_monthly
        if monthly is not None and monthly >= pol["monthly_floor_raiser_pct"]:
            shift = float(pol["threshold_shift_pp"])
        return {
            "esc_amber": pol["escalate_amber_pct"] - shift,
            "esc_red": pol["escalate_red_pct"] - shift,
            "desc_amber": pol["deescalate_amber_pct"] - shift,
            "desc_green": pol["deescalate_green_pct"] - shift,
            "dwell": float(pol["dwell_seconds"]),
            "pred_amber": float(pol["predictive_amber_hours"]),
            "pred_red": float(pol["predictive_red_hours"]),
        }

    # ── band FSM ────────────────────────────────────────────────────────

    def _load_state(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text())
            if isinstance(data, dict) and data.get("state") in _ORDER:
                return data
        except Exception:
            pass
        return {"state": GREEN, "since": self._now()}

    def _save_state(self, state: dict) -> None:
        try:
            self.state_path.write_text(json.dumps(state, indent=2))
        except Exception:
            pass  # persistence failure must never bubble up

    def _next_state(self, cur: str, since: float, inputs: PressureInputs,
                    now: float, th: dict) -> str:
        used = inputs.used_pct_5h
        eff_hours: float | None = None
        if inputs.exhausts_in_hours is not None:
            unc = inputs.uncertainty if inputs.uncertainty is not None else 0.0
            eff_hours = inputs.exhausts_in_hours - max(0.0, unc)

        # Escalation — immediate, no dwell (safety direction). Loop so a
        # deep spike (GREEN at 85%) lands in RED within one update.
        state = cur
        while True:
            if state == GREEN:
                if used is not None and used >= th["esc_amber"]:
                    state = AMBER; continue
                if (inputs.will_exhaust and eff_hours is not None
                        and eff_hours <= th["pred_amber"]):
                    state = AMBER; continue
            elif state == AMBER:
                if used is not None and used >= th["esc_red"]:
                    state = RED; continue
                if (inputs.will_exhaust and eff_hours is not None
                        and eff_hours <= th["pred_red"]):
                    state = RED; continue
            break

        # De-escalation — single step, gated by dwell (anti-flap).
        # No-data (used is None) counts as low pressure (G2: "no data" is
        # green-with-caveat, never red) but still has to wait out dwell.
        if now - since >= th["dwell"]:
            low = 0.0 if used is None else used
            if state == RED and low <= th["desc_amber"]:
                state = AMBER
            elif state == AMBER and low <= th["desc_green"]:
                state = GREEN
        return state

    def update(self, inputs: PressureInputs) -> dict:
        """Advance the FSM one observation; persist; return snapshot."""
        now = self._now()
        th = self._thresholds(inputs)
        persisted = self._load_state()
        cur, since = persisted["state"], float(persisted.get("since", now))
        nxt = self._next_state(cur, since, inputs, now, th)
        if nxt != cur:
            since = now
        snap = {
            "state": nxt,
            "since": since,
            "updated_at": now,
            "inputs": inputs.snapshot(),
            "capacity": self.flat_rate_capacity(inputs),
        }
        self._save_state({
            "state": nxt,
            "since": since,
            "updated_at": now,
            "used_pct_5h": inputs.used_pct_5h,
            "used_pct_monthly": inputs.used_pct_monthly,
            "capacity": snap["capacity"],
        })
        return snap

    # ── flat-rate capacity (D6) ─────────────────────────────────────────

    def flat_rate_capacity(self, inputs: PressureInputs) -> str:
        regime = inputs.ollama_regime
        ollama_ok = regime == "included"
        ollama_extra = regime == "extra"
        ollama_dead = regime in _NO_OLLAMA_REGIMES or regime is None
        if ollama_ok:
            return "none" if inputs.friend_locked else "ok"
        if ollama_extra:
            return "none" if inputs.friend_locked else "ollama_extra"
        # exhausted / paywalled / unknown regime: no ollama evidence ->
        # conservative friend_only (never assume ollama capacity).
        return "none" if inputs.friend_locked else "friend_only"

    # ── interactive classifier (D4 v1 heuristic) ────────────────────────

    def classify_interactive(self, session_id: str | None) -> bool:
        """True iff session_id had an api_calls row within the window.

        The current (in-flight) request is not yet logged, so this sees
        only PRIOR calls — a session's first request classifies as
        background by design (D4 trade-off).
        """
        if not session_id:
            return False
        window = float(self._policy()["interactive_window_seconds"])
        try:
            conn = sqlite3.connect(self.db_path, timeout=5)
            try:
                row = conn.execute(
                    "SELECT 1 FROM api_calls WHERE session_id = ?"
                    " AND ts >= ? LIMIT 1",
                    (session_id, self._now() - window)).fetchone()
                return row is not None
            finally:
                conn.close()
        except Exception:
            return False  # no table / locked db -> background (safe default)

    # ── decision matrix (D5, shadow subset) ─────────────────────────────

    def decide(self, requested_model: str, session_id: str | None,
               snap: dict) -> Decision:
        """Compute the would-be decision. Pure w.r.t. snap; never reroutes."""
        state = snap.get("state", GREEN)
        capacity = snap.get("capacity", "friend_only")
        interactive = self.classify_interactive(session_id)

        if requested_model != "glm-5.3":
            return Decision(requested_model, requested_model, None, state,
                            interactive, "not_glm_53_passthrough")

        if interactive:
            # Invariant (D10): interactive is NEVER downgraded. Under RED
            # it is rationed (still 5.3 @ friend) — a mid-turn 429 is
            # worse than a weaker model, and the residual is caught by
            # the existing external failover.
            reason = "interactive_rationed" if state == RED else "interactive_protected"
            return Decision("glm-5.3", "glm-5.3", "friend", state, True, reason)

        # Background branch (default tier S per D2).
        if state == GREEN:
            return Decision("glm-5.3", "glm-5.3", "friend", state, False, "bg_kept")
        if capacity == "ok":
            return Decision("glm-5.3", "glm-5.2", "ollama_cloud", state, False,
                            "bg_downgraded_ollama")
        if capacity == "ollama_extra":
            return Decision("glm-5.3", "glm-5.2", "ollama_cloud", state, False,
                            "bg_downgraded_ollama_extra")
        if capacity == "friend_only":
            # Quota-neutral (G9): token quota is model-agnostic; the only
            # saving is fewer emitted tokens. True fix is Layer 1 deferral.
            return Decision("glm-5.3", "glm-5.2", "friend", state, False,
                            "bg_quota_neutral")
        return Decision("glm-5.3", "glm-5.3", "friend", state, False,
                        "bg_last_resort")

    # ── input gathering ─────────────────────────────────────────────────

    def gather_inputs(self, ollama_regime: str | None = None,
                      friend_locked: bool = False) -> PressureInputs:
        """Latest friend-key Kalman samples straight from zai_usage.db."""
        try:
            conn = sqlite3.connect(self.db_path, timeout=5)
            try:
                row5 = conn.execute(
                    "SELECT used_pct_observed, exhausts_in_hours, uncertainty,"
                    " will_exhaust FROM kalman_samples"
                    " WHERE key = 'friend' AND window = '5-hour'"
                    " ORDER BY ts DESC LIMIT 1").fetchone()
                rowmo = conn.execute(
                    "SELECT used_pct_observed FROM kalman_samples"
                    " WHERE key = 'friend' AND window = 'monthly'"
                    " ORDER BY ts DESC LIMIT 1").fetchone()
            finally:
                conn.close()
        except Exception:
            row5, rowmo = None, None
        return PressureInputs(
            used_pct_5h=row5[0] if row5 else None,
            exhausts_in_hours=row5[1] if row5 else None,
            uncertainty=row5[2] if row5 else None,
            will_exhaust=bool(row5[3]) if row5 else False,
            used_pct_monthly=rowmo[0] if rowmo else None,
            ollama_regime=ollama_regime,
            friend_locked=friend_locked,
        )

    # ── shadow pipeline + logging (D8) ──────────────────────────────────

    def shadow_decision(self, requested_model: str, session_id: str | None,
                        ollama_regime: str | None = None,
                        friend_locked: bool = False) -> Decision | None:
        """Full shadow pipeline: gather -> update band -> decide -> log.

        Returns None when kill-switched (nothing logged, nothing changed).
        """
        if not self.enabled():
            return None
        inputs = self.gather_inputs(ollama_regime=ollama_regime,
                                    friend_locked=friend_locked)
        snap = self.update(inputs)
        decision = self.decide(requested_model, session_id, snap)
        if requested_model == "glm-5.3":
            self.log_decision(decision)
        return decision

    def log_decision(self, decision: Decision) -> None:
        """Append to pressure_decisions. Never raises."""
        try:
            conn = sqlite3.connect(self.db_path, timeout=5)
            try:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS pressure_decisions ("
                    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    " ts REAL NOT NULL,"
                    " state TEXT,"
                    " requested_model TEXT,"
                    " would_serve_model TEXT,"
                    " would_provider TEXT,"
                    " interactive INTEGER,"
                    " reason TEXT)")
                conn.execute(
                    "INSERT INTO pressure_decisions"
                    " (ts, state, requested_model, would_serve_model,"
                    "  would_provider, interactive, reason)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (self._now(), decision.state, decision.requested_model,
                     decision.would_serve_model, decision.would_provider,
                     1 if decision.interactive else 0, decision.reason))
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass  # logging must never break request handling

    def snapshot(self, limit: int = 20) -> dict:
        """Observability payload for GET /pressure (D8)."""
        now = self._now()
        state = self._load_state()
        decisions: list[dict] = []
        try:
            conn = sqlite3.connect(self.db_path, timeout=5)
            try:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS pressure_decisions ("
                    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    " ts REAL NOT NULL,"
                    " state TEXT,"
                    " requested_model TEXT,"
                    " would_serve_model TEXT,"
                    " would_provider TEXT,"
                    " interactive INTEGER,"
                    " reason TEXT)")
                rows = conn.execute(
                    "SELECT ts, state, requested_model, would_serve_model,"
                    " would_provider, interactive, reason"
                    " FROM pressure_decisions ORDER BY ts DESC LIMIT ?",
                    (int(limit),)).fetchall()
                decisions = [
                    {"ts": r[0], "state": r[1], "requested_model": r[2],
                     "would_serve_model": r[3], "would_provider": r[4],
                     "interactive": bool(r[5]), "reason": r[6]}
                    for r in rows]
            finally:
                conn.close()
        except Exception:
            pass
        return {
            "enabled": self.enabled(),
            "mode": self.mode(),
            "state": state.get("state"),
            "since": state.get("since"),
            "state_age_s": max(0, int(now - float(state.get("since", now)))),
            "updated_at": state.get("updated_at"),
            "capacity": state.get("capacity"),
            "last_decisions": decisions,
        }
