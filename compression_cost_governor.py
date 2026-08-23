#!/usr/bin/env python3
"""Compression cost-ratio Kalman governor with PI control.

Tracks the ratio of compression-cost to total-cost from the api_calls DB
using a 1-D Kalman filter. Outputs a PI-controlled threshold AND a
compression model budget that the proxy's compression_model_router reads.

When the ratio is high, the budget is LOWERED (cheaper model selected) —
NOT the threshold raised (which would skip compression and worsen
input burn). The threshold lever stays near baseline; the model-selection
lever does the cost work.

Runs on the kalman-data-collect schedule (every 15 min).

State: ~/.hermes/bot/compression_governor_state.json
Output: ~/.hermes/bot/compression_threshold_override.json
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

BOT_DIR = Path.home() / ".hermes" / "bot"
DB_PATH = BOT_DIR / "zai_usage.db"
STATE_FILE = BOT_DIR / "compression_governor_state.json"
OUTPUT_FILE = BOT_DIR / "compression_threshold_override.json"

WINDOW_HOURS = 24
TARGET_RATIO = 0.20

COMPRESSION_THRESHOLD_BASE = 0.6
COMPRESSION_THRESHOLD_MIN = 0.4
COMPRESSION_THRESHOLD_MAX = 0.85

BUDGET_BASE = 0.20
BUDGET_MIN = 0.08
BUDGET_MAX = 0.50

PI_KP = 0.8
PI_KI = 0.15
PI_INTEGRAL_MAX = 0.5


class CostRatioKalman:
    def __init__(self, initial_ratio: float = 0.25):
        self.x = initial_ratio
        self.p = 0.5
        self.q = 0.01
        self.r = 0.05
        self.n = 0

    def update(self, measurement: float) -> float:
        self.n += 1
        k = self.p / (self.p + self.r)
        self.x = self.x + k * (measurement - self.x)
        self.p = (1 - k) * self.p + self.q
        return self.x

    def to_dict(self):
        return {"x": self.x, "p": self.p, "q": self.q, "r": self.r, "n": self.n}

    @classmethod
    def from_dict(cls, d):
        kf = cls(d.get("x", 0.25))
        kf.p = d.get("p", 0.5)
        kf.q = d.get("q", 0.01)
        kf.r = d.get("r", 0.05)
        kf.n = d.get("n", 0)
        return kf


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {
        "kalman": CostRatioKalman().to_dict(),
        "pi_integral": 0.0,
        "last_ratio": 0.0,
        "last_ts": 0,
        "threshold": COMPRESSION_THRESHOLD_BASE,
        "compression_budget": BUDGET_BASE,
    }


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def compute_ratio(db_path, hours=WINDOW_HOURS):
    if not db_path.exists():
        return 0.0, 0.0, 0.0
    cutoff = time.time() - hours * 3600
    try:
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("""
            SELECT
                SUM(CASE WHEN task_type = 'compression' THEN cost_usd ELSE 0 END) as comp_cost,
                SUM(CASE WHEN task_type = 'compression' THEN 0 ELSE cost_usd END) as noncomp_cost,
                SUM(cost_usd) as total_cost
            FROM api_calls WHERE ts >= ? AND status_code = 200
        """, (cutoff,)).fetchone()
        conn.close()
        comp = float(row[0] or 0)
        noncomp = float(row[1] or 0)
        total = float(row[2] or 0)
        ratio = comp / total if total > 0 else 0.0
        return comp, noncomp, ratio
    except Exception:
        try:
            conn = sqlite3.connect(str(db_path))
            row = conn.execute("""
                SELECT
                    SUM(CASE WHEN session_id IS NULL THEN cost_usd ELSE 0 END) as aux_cost,
                    SUM(CASE WHEN session_id IS NOT NULL THEN cost_usd ELSE 0 END) as prod_cost,
                    SUM(cost_usd) as total_cost
                FROM api_calls WHERE ts >= ? AND status_code = 200
            """, (cutoff,)).fetchone()
            conn.close()
            aux = float(row[0] or 0)
            prod = float(row[1] or 0)
            total = float(row[2] or 0)
            return aux, prod, (aux / total if total > 0 else 0.0)
        except Exception:
            return 0.0, 0.0, 0.0


def compute_pi_control(ratio_estimate, target, integral_state):
    error = ratio_estimate - target

    new_integral = integral_state + error
    new_integral = max(-PI_INTEGRAL_MAX, min(PI_INTEGRAL_MAX, new_integral))

    control_output = PI_KP * error + PI_KI * new_integral

    threshold = COMPRESSION_THRESHOLD_BASE + control_output
    threshold = max(COMPRESSION_THRESHOLD_MIN, min(COMPRESSION_THRESHOLD_MAX, threshold))

    budget = BUDGET_BASE - control_output
    budget = max(BUDGET_MIN, min(BUDGET_MAX, budget))

    return threshold, budget, new_integral


def main():
    state = load_state()
    kf = CostRatioKalman.from_dict(state.get("kalman", {}))
    integral = state.get("pi_integral", 0.0)

    comp, noncomp, measured_ratio = compute_ratio(DB_PATH)
    estimate = kf.update(measured_ratio)

    threshold, budget, integral = compute_pi_control(
        estimate, TARGET_RATIO, integral)

    state["kalman"] = kf.to_dict()
    state["pi_integral"] = integral
    state["last_ratio"] = measured_ratio
    state["last_estimate"] = estimate
    state["last_comp_cost"] = comp
    state["last_noncomp_cost"] = noncomp
    state["threshold"] = threshold
    state["compression_budget"] = budget
    state["last_ts"] = time.time()
    save_state(state)

    override = {
        "threshold": threshold,
        "compression_budget": budget,
        "ratio_estimate": estimate,
        "measured_ratio": measured_ratio,
        "target_ratio": TARGET_RATIO,
        "pi_integral": integral,
        "updated_at": time.time(),
    }
    OUTPUT_FILE.write_text(json.dumps(override, indent=2))

    tag = "OK" if estimate <= TARGET_RATIO else "HIGH"
    print(f"[compression-governor] ratio={measured_ratio:.1%} estimate={estimate:.1%} "
          f"target={TARGET_RATIO:.0%} threshold={threshold:.2f} "
          f"budget=${budget:.2f}/M comp=${comp:.2f} noncomp=${noncomp:.2f} "
          f"integral={integral:.4f} [{tag}] n={kf.n}")


if __name__ == "__main__":
    main()
