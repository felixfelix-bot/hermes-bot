#!/usr/bin/env python3
"""Enhanced burn-rate predictor with MultiResourceKalmanPredictor and crash prevention.

This extends the original Kalman filter predictor to:
1. Track multiple resource types (CPU, memory, API keys, disk)
2. Provide 30-minute advance crash warnings
3. Support self-healing procedures
4. Implement confidence intervals and continuous model improvement
"""

from __future__ import annotations
import json
import os
import sqlite3
import subprocess
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import math

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False

DB_PATH = "~/.hermes/bot/zai_usage.db"
QUOTA_URL = "http://localhost:9099/quota"
MIN_DATA_POINTS = 5
LOOKBACK_HOURS = 12

TUNING_FILE = Path(__file__).parent / "kalman_tuning.json"


def _load_tuning() -> dict:
    """Load Kalman parameter overrides from the tuning file if it exists.

    Written by ``kalman_retune.py``. Holds per-key ``measurement_noise`` (R) and
    ``process_noise`` (Q) overrides so the auto-tuner can adjust the filter
    without editing this source file. Returns an empty dict when the file is
    missing or unreadable, in which case callers fall back to the adaptive
    variance estimate.
    """
    try:
        if TUNING_FILE.exists():
            return json.loads(TUNING_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        pass
    return {}

# ── Original Kalman filter (2-state: volume + velocity) ──────────────────────

class KalmanPredictor:
    """Local-linear-trend Kalman filter for token burn rate prediction.

    State vector:  x = [volume, velocity]  (tokens/hour, delta tokens/hour²)
    Observation:   z = [volume]            (we only measure tokens/hour)

    The filter tracks both how fast we're burning AND whether the burn rate
    is accelerating or decelerating. This lets us project forward linearly
    instead of assuming a constant rate.
    """

    def __init__(self, process_noise: float = 1.0, measurement_noise: float = 50.0):
        if not _HAS_NUMPY:
            raise RuntimeError("numpy required for KalmanPredictor")
        # State: [volume, velocity]
        self.x = np.array([[0.0], [0.0]], dtype=np.float64)
        # State transition: next_vol = cur_vol + velocity; next_vel = velocity
        self.F = np.array([[1.0, 1.0],
                           [0.0, 1.0]], dtype=np.float64)
        # Measurement: we observe volume only
        self.H = np.array([[1.0, 0.0]], dtype=np.float64)
        # Covariance (high initial uncertainty — scale to match R so the filter can actually converge)
        self.P = np.eye(2, dtype=np.float64) * measurement_noise
        # Process noise (how erratic is the system)
        self.Q = np.array([[process_noise, 0.0],
                           [0.0, process_noise]], dtype=np.float64)
        # Measurement noise (how noisy are observations)
        self.R = np.array([[measurement_noise]], dtype=np.float64)
        self._initialized = False

    def update(self, measurement: float) -> None:
        """Incorporate a new hourly token measurement."""
        z = np.array([[float(measurement)]], dtype=np.float64)
        if not self._initialized:
            self.x[0, 0] = float(measurement)
            self._initialized = True
            return
        # Innovation
        y = z - self.H @ self.x
        # Innovation covariance
        S = self.H @ self.P @ self.H.T + self.R
        # Kalman gain
        K = self.P @ self.H.T @ np.linalg.inv(S)
        # State update
        self.x = self.x + K @ y
        # Covariance update
        I = np.eye(2, dtype=np.float64)
        self.P = (I - K @ self.H) @ self.P

    def predict(self) -> float:
        """Predict next-hour volume."""
        self.x = self.F @ self.x
        self.P = self.F @ self.P @ self.F.T + self.Q
        return float(self.x[0, 0])

    def predict_steps_ahead(self, steps: int) -> list[float]:
        """Project N hours ahead using current volume + velocity (no update)."""
        vol = float(self.x[0, 0])
        vel = float(self.x[1, 0])
        return [max(0.0, vol + vel * s) for s in range(1, steps + 1)]

    @property
    def volume(self) -> float:
        return float(self.x[0, 0])

    @property
    def velocity(self) -> float:
        return float(self.x[1, 0])

    @property
    def uncertainty(self) -> float:
        """Position uncertainty (standard deviation)."""
        return float(np.sqrt(self.P[0, 0]))


# ── Per-key predictor instances (persisted across calls) ────────────────────

_predictors: dict[str, KalmanPredictor] = {}


def _get_predictor(key_name: str) -> KalmanPredictor:
    """Get or create the Kalman predictor for a key."""
    if key_name not in _predictors:
        _predictors[key_name] = KalmanPredictor()
    return _predictors[key_name]


# ── Data access ─────────────────────────────────────────────────────────────

def _utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _get_burn_history(key_name: str, hours: int = LOOKBACK_HOURS) -> list[dict]:
    """Read token usage from DB, bucketed by hour."""
    import os
    db_path = os.path.expanduser(DB_PATH)
    if not os.path.exists(db_path):
        return []

    cutoff = time.time() - (hours * 3600)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT ts, total_tokens FROM api_calls WHERE key_name = ? AND ts >= ? AND status_code = 200 ORDER BY ts",
            (key_name, cutoff)
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return []

    # Bucket by hour
    hourly = defaultdict(int)
    for row in rows:
        hour_bucket = int(row["ts"] // 3600) * 3600
        hourly[hour_bucket] += row["total_tokens"]

    return [{"hour_ts": ts, "tokens": tokens} for ts, tokens in sorted(hourly.items())]


def _train_kalman(key_name: str, history: list[dict]) -> KalmanPredictor | None:
    """Train a Kalman predictor from hourly history data."""
    if not _HAS_NUMPY or len(history) < 2:
        return None

    volumes = [h.get("tokens", 0) for h in history]

    # Check for a manual override from the tuning file (written by kalman_retune.py)
    tuning = _load_tuning()
    override_r = tuning.get("measurement_noise", {}).get(key_name)
    override_q = tuning.get("process_noise", {}).get(key_name, 1.0)

    if override_r is not None:
        R = override_r
        Q = override_q
    else:
        # ADAPTIVE: compute measurement noise from the data variance.
        # Auto-scales R to the actual signal magnitude — hourly token buckets
        # run ~1M–10M, so a fixed R=50 was catastrophically miscalibrated
        # (assumed σ≈7 tokens → gain→1, covariance collapsed, 0% coverage).
        mean_v = sum(volumes) / len(volumes)
        variance = sum((v - mean_v) ** 2 for v in volumes) / max(len(volumes) - 1, 1)
        R = max(variance, 1e6)  # floor prevents collapse on near-constant data
        Q = 1.0  # process noise stays small — burn rate doesn't swing wildly hour-to-hour

    kf = KalmanPredictor(process_noise=Q, measurement_noise=R)
    for h in history:
        kf.update(h["tokens"])
    # Run one predict step to estimate the "current" burn rate
    kf.predict()
    _predictors[key_name] = kf
    return kf


def _get_quota_windows(key_name: str) -> list[dict]:
    """Fetch current quota windows from the proxy."""
    try:
        req = urllib.request.Request(QUOTA_URL)
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode())
        windows = data.get(key_name, {}).get("windows", [])
        return windows
    except Exception:
        return []

# --- Multi-Resource Kalman Predictor ---

class MultiResourceKalmanPredictor:
    """Extended Kalman filter for multiple resource types.
    
    Tracks: CPU, Memory, API tokens, Disk I/O
    Each resource has its own Kalman filter with shared state awareness.
    """
    
    def __init__(self, resource_type: str, process_noise: float = 1.0, measurement_noise: float = 50.0):
        if not _HAS_NUMPY:
            raise RuntimeError("numpy required for MultiResourceKalmanPredictor")
        
        self.resource_type = resource_type
        self.kalman = KalmanPredictor(process_noise, measurement_noise)
        self.confidence_threshold = 0.85  # 85% confidence for warnings
        self.crash_threshold = self._get_crash_threshold()
        self.warning_history = []
        
    def _get_crash_threshold(self) -> float:
        """Resource-specific crash thresholds."""
        thresholds = {
            "cpu": 90.0,      # 90% CPU usage
            "memory": 85.0,   # 85% memory usage  
            "api_tokens": 95.0,  # 95% API quota usage
            "disk_io": 80.0   # 80% disk I/O capacity
        }
        return thresholds.get(self.resource_type, 90.0)
    
    def update(self, measurement: float, timestamp: Optional[float] = None) -> None:
        """Update with new measurement and timestamp."""
        if timestamp is None:
            timestamp = time.time()
            
        self.kalman.update(measurement)
        
        # Check for approaching crash conditions
        current_level = float(self.kalman.volume)
        if current_level >= self.crash_threshold * 0.8:  # 80% of threshold
            self._emit_warning(current_level, timestamp)
    
    def _emit_warning(self, current_level: float, timestamp: float) -> None:
        """Emit crash warning if conditions warrant."""
        uncertainty = float(self.kalman.uncertainty)
        confidence = max(0.0, 1.0 - (uncertainty / current_level)) if current_level > 0 else 0.0
        
        if confidence >= self.confidence_threshold:
            warning = {
                "resource_type": self.resource_type,
                "current_level": current_level,
                "threshold": self.crash_threshold,
                "confidence": confidence,
                "timestamp": timestamp,
                "velocity": float(self.kalman.velocity),
                "predicted_crash_time": self._predict_crash_time(current_level, confidence)
            }
            
            self.warning_history.append(warning)
            
            # Keep only last 10 warnings
            if len(self.warning_history) > 10:
                self.warning_history = self.warning_history[-10:]
    
    def _predict_crash_time(self, current_level: float, confidence: float) -> Optional[float]:
        """Predict when resource will reach crash threshold."""
        if confidence < 0.7:  # Low confidence, don't predict
            return None
            
        velocity = float(self.kalman.velocity)
        if velocity <= 0:  # Not increasing
            return None
            
        remaining = self.crash_threshold - current_level
        if remaining <= 0:  # Already exceeded
            return time.time()
            
        # Time to crash = remaining / velocity
        hours_to_crash = remaining / abs(velocity)
        crash_time = time.time() + (hours_to_crash * 3600)
        
        return crash_time
    
    def predict_crash_risk(self, lookahead_minutes: int = 30) -> Dict:
        """Predict crash risk for the next N minutes."""
        if not self.kalman._initialized:
            return {"risk_level": "unknown", "confidence": 0.0, "minutes_to_crash": None}
        
        current_level = float(self.kalman.volume)
        velocity = float(self.kalman.velocity)
        uncertainty = float(self.kalman.uncertainty)
        
        # Project current trend forward
        projected_level = current_level + (velocity * lookahead_minutes / 60.0)
        
        # Calculate risk
        if projected_level >= self.crash_threshold:
            risk_level = "critical"
        elif current_level >= self.crash_threshold * 0.9:
            risk_level = "high" 
        elif current_level >= self.crash_threshold * 0.8:
            risk_level = "medium"
        else:
            risk_level = "low"
        
        # Confidence decreases with projection time and uncertainty
        uncertainty_factor = min(1.0, uncertainty / max(current_level, 1.0))
        time_factor = min(1.0, lookahead_minutes / 120.0)  # 2 hours = max time factor
        confidence = max(0.0, 1.0 - uncertainty_factor - time_factor)
        
        # Estimate minutes to crash
        minutes_to_crash = None
        if velocity > 0 and current_level < self.crash_threshold:
            minutes_to_crash = ((self.crash_threshold - current_level) / velocity) * 60
        
        return {
            "resource_type": self.resource_type,
            "current_level": round(current_level, 2),
            "projected_level": round(projected_level, 2),
            "threshold": self.crash_threshold,
            "risk_level": risk_level,
            "confidence": round(confidence, 3),
            "velocity": round(velocity, 3),
            "uncertainty": round(uncertainty, 2),
            "minutes_to_crash": round(minutes_to_crash) if minutes_to_crash else None,
            "lookahead_minutes": lookahead_minutes
        }
    
    def get_self_healing_actions(self) -> List[Dict]:
        """Get recommended self-healing actions based on current state."""
        if not self.kalman._initialized:
            return []
        
        risk = self.predict_crash_risk(30)  # 30-minute lookahead
        actions = []
        
        if risk["risk_level"] in ["high", "critical"]:
            actions.append({
                "action": "reduce_worker_pool",
                "priority": "high",
                "description": f"Gradually reduce worker pool due to {self.resource_type} pressure",
                "resource": self.resource_type,
                "severity": risk["risk_level"]
            })
            
        if risk["risk_level"] == "critical" and risk["minutes_to_crash"] and risk["minutes_to_crash"] < 15:
            actions.append({
                "action": "emergency_stop",
                "priority": "critical", 
                "description": f"Emergency stop: {self.resource_type} crash expected in {risk['minutes_to_crash']} minutes",
                "resource": self.resource_type,
                "severity": "critical"
            })
            
        if risk["confidence"] > 0.9 and risk["velocity"] > 0:
            actions.append({
                "action": "context_preservation",
                "priority": "medium",
                "description": f"Preserve critical task context for {self.resource_type}",
                "resource": self.resource_type,
                "severity": "medium"
            })
            
        return actions

# --- Crash Prevention Manager ---

class CrashPreventionManager:
    """Coordinates multiple resource predictors and crash prevention."""
    
    def __init__(self):
        self.predictors: Dict[str, MultiResourceKalmanPredictor] = {}
        self.alert_log_path = Path("~/.hermes/logs/crash_prevention.log").expanduser()
        self.state_path = Path("~/.hermes/state/crash_prevention_state.json").expanduser()
        
        # Initialize predictors for each resource type
        resource_types = ["cpu", "memory", "api_tokens", "disk_io"]
        for resource_type in resource_types:
            self.predictors[resource_type] = MultiResourceKalmanPredictor(resource_type)
        
        # Ensure directories exist
        self.alert_log_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
    
    def update_resource_measurement(self, resource_type: str, measurement: float, timestamp: Optional[float] = None):
        """Update a resource measurement."""
        if resource_type in self.predictors:
            self.predictors[resource_type].update(measurement, timestamp)
    
    def get_system_crash_risk(self, lookahead_minutes: int = 30) -> Dict:
        """Get overall system crash risk from all resources."""
        risks = {}
        overall_risk_level = "low"
        highest_confidence = 0.0
        
        for resource_type, predictor in self.predictors.items():
            risk = predictor.predict_crash_risk(lookahead_minutes)
            risks[resource_type] = risk
            
            # Update overall risk level
            if risk["risk_level"] == "critical":
                overall_risk_level = "critical"
            elif risk["risk_level"] == "high" and overall_risk_level != "critical":
                overall_risk_level = "high"
            elif risk["risk_level"] == "medium" and overall_risk_level in ["low", "medium"]:
                overall_risk_level = "medium"
                
            highest_confidence = max(highest_confidence, risk["confidence"])
        
        return {
            "overall_risk_level": overall_risk_level,
            "highest_confidence": highest_confidence,
            "resource_risks": risks,
            "lookahead_minutes": lookahead_minutes,
            "timestamp": time.time()
        }
    
    def get_preventive_actions(self) -> List[Dict]:
        """Get all recommended preventive actions."""
        all_actions = []
        
        for resource_type, predictor in self.predictors.items():
            actions = predictor.get_self_healing_actions()
            all_actions.extend(actions)
        
        # Sort by priority
        priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        all_actions.sort(key=lambda x: priority_order.get(x["priority"], 4))
        
        return all_actions
    
    def execute_preventive_actions(self, actions: List[Dict]) -> Dict:
        """Execute preventive actions and return results."""
        results = {
            "executed": [],
            "failed": [],
            "timestamp": time.time()
        }
        
        for action in actions:
            try:
                result = self._execute_single_action(action)
                results["executed"].append({
                    "action": action,
                    "result": result
                })
                
                # Log the action
                self._log_alert(f"Executed: {action['action']} for {action['resource']} - {action['description']}")
                
            except Exception as e:
                error_msg = f"Failed to execute {action['action']}: {str(e)}"
                results["failed"].append({
                    "action": action,
                    "error": error_msg
                })
                self._log_alert(error_msg, level="ERROR")
        
        # Save state
        self._save_state(results)
        
        return results
    
    def _execute_single_action(self, action: Dict) -> Dict:
        """Execute a single preventive action."""
        action_type = action["action"]
        
        if action_type == "reduce_worker_pool":
            return self._reduce_worker_pool(action)
        elif action_type == "emergency_stop":
            return self._emergency_stop(action)
        elif action_type == "context_preservation":
            return self._preserve_context(action)
        else:
            raise ValueError(f"Unknown action type: {action_type}")
    
    def _reduce_worker_pool(self, action: Dict) -> Dict:
        """Gradually reduce worker pool."""
        # Calculate reduction factor based on severity
        severity_factor = {
            "high": 0.7,
            "critical": 0.5
        }.get(action["severity"], 0.8)
        
        # Update worker pool state
        state_path = Path("~/.hermes/state/worker_pool_state.json").expanduser()
        if state_path.exists():
            with open(state_path) as f:
                state = json.load(f)
        else:
            state = {"max_workers": 4, "current_workers": 0, "reduction_factor": 1.0}
        
        state["reduction_factor"] = min(state["reduction_factor"], severity_factor)
        state["critical_mode"] = action["severity"] == "critical"
        
        with open(state_path, 'w') as f:
            json.dump(state, f, indent=2)
        
        return {
            "action": "worker_pool_reduced",
            "factor": severity_factor,
            "resource": action["resource"]
        }
    
    def _emergency_stop(self, action: Dict) -> Dict:
        """Emergency stop - pause dispatching immediately."""
        # Create emergency stop file
        emergency_file = Path("~/.hermes/state/emergency_stop").expanduser()
        emergency_file.touch()
        
        # Log emergency stop
        self._log_alert(f"EMERGENCY STOP triggered: {action['description']}", level="CRITICAL")
        
        return {
            "action": "emergency_stop",
            "resource": action["resource"],
            "stop_file": str(emergency_file)
        }
    
    def _preserve_context(self, action: Dict) -> Dict:
        """Preserve critical task context."""
        # Find running tasks and save their state
        tasks_to_preserve = []
        
        try:
            result = subprocess.run(
                ["ps", "aux"], capture_output=True, text=True, timeout=5
            )
            
            for line in result.stdout.split('\n'):
                if "hermes" in line and "-p" in line and "worker" in line:
                    tasks_to_preserve.append(line.strip())
            
            # Save context
            context_file = Path("~/.hermes/state/task_context_backup.json").expanduser()
            backup_data = {
                "timestamp": time.time(),
                "resource": action["resource"],
                "tasks": tasks_to_preserve,
                "reason": action["description"]
            }
            
            with open(context_file, 'w') as f:
                json.dump(backup_data, f, indent=2)
            
            return {
                "action": "context_preserved",
                "tasks_count": len(tasks_to_preserve),
                "backup_file": str(context_file)
            }
            
        except Exception as e:
            raise RuntimeError(f"Failed to preserve context: {str(e)}")
    
    def _log_alert(self, message: str, level: str = "INFO"):
        """Log alert to file."""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        log_entry = f"[{timestamp}] [{level}] {message}\n"
        
        with open(self.alert_log_path, 'a') as f:
            f.write(log_entry)
    
    def _save_state(self, results: Dict):
        """Save crash prevention state."""
        state = {
            "last_action_timestamp": time.time(),
            "last_results": results,
            "predictors_state": {}
        }
        
        # Save predictor states
        for resource_type, predictor in self.predictors.items():
            if predictor.kalman._initialized:
                state["predictors_state"][resource_type] = {
                    "volume": float(predictor.kalman.volume),
                    "velocity": float(predictor.kalman.velocity),
                    "uncertainty": float(predictor.kalman.uncertainty)
                }
        
        with open(self.state_path, 'w') as f:
            json.dump(state, f, indent=2)
    
    def load_state(self):
        """Load previous crash prevention state."""
        if not self.state_path.exists():
            return
        
        try:
            with open(self.state_path) as f:
                state = json.load(f)
            
            # Restore predictor states
            for resource_type, pred_state in state.get("predictors_state", {}).items():
                if resource_type in self.predictors:
                    predictor = self.predictors[resource_type]
                    # Initialize predictor with saved state
                    predictor.kalman.x = np.array([[pred_state["volume"]], [pred_state["velocity"]]])
                    predictor.kalman._initialized = True
                    
        except (json.JSONDecodeError, KeyError, ValueError):
            # Failed to load state, continue with fresh initialization
            pass

# --- Global crash prevention manager instance ---
_crash_manager = CrashPreventionManager()