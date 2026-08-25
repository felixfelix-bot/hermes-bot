#!/usr/bin/env python3
"""Multi-resource Kalman predictor for system resource monitoring.

Extends the burn-rate Kalman filter to track multiple system resources:
- tokens (existing API token burn rate)
- cpu_load (1-minute load average)
- memory_pct (memory used percentage)
- swap_used_pct (swap used percentage)
- disk_used_pct (disk used percentage)
- worker_count (active Hermes workers)

This enables:
- Early warning for resource exhaustion 30+ minutes ahead
- Multi-constraint resource prediction
- Confidence intervals for all resources
- Resource correlation modeling

Pure numpy + stdlib — no ML libraries beyond numpy.
"""

from __future__ import annotations
import json
import time
from datetime import datetime, timezone
from typing import Dict, List, Tuple, Any, Optional

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False


class MultiResourceKalmanPredictor:
    """6-state Kalman filter for multi-resource prediction.

    State vector:  x = [tokens, cpu_load, memory_pct, swap_used_pct, disk_used_pct, worker_count]
                   (tokens/hour, load avg, % used, % swap used, % disk used, count)
    
    Tracks system resources with uncertainty estimates and correlations.
    """

    # Resource names for indexing
    RESOURCES = ['tokens', 'cpu_load', 'memory_pct', 'swap_used_pct', 'disk_used_pct', 'worker_count']
    RESOURCE_IDX = {name: i for i, name in enumerate(RESOURCES)}
    
    # State indices
    TOKENS_IDX = 0
    CPU_IDX = 1
    MEMORY_IDX = 2
    SWAP_IDX = 3
    DISK_IDX = 4
    WORKER_IDX = 5

    def __init__(self, 
                 process_noise: float = 1.0,
                 measurement_noise: Dict[str, float] = None):
        """Initialize the 4-state Kalman filter.
        
        Args:
            process_noise: Base process noise (Q diagonal elements)
            measurement_noise: Per-resource measurement noise (R diagonal elements)
        """
        if not _HAS_NUMPY:
            raise RuntimeError("numpy required for MultiResourceKalmanPredictor")
        
        # Default measurement noise per resource (can be tuned per resource)
        default_noise = {
            'tokens': 1e6,          # High variance for token burn rates
            'cpu_load': 0.5,        # CPU load typically 0-10, noise ~0.5
            'memory_pct': 5.0,      # Memory % typically 0-100, noise ~5%
            'swap_used_pct': 10.0,  # Swap can jump significantly
            'disk_used_pct': 2.0,   # Disk changes slowly
            'worker_count': 2.0     # Worker count typically 0-50, noise ~2
        }
        
        # State vector: [tokens, cpu_load, memory_pct, swap_used_pct, disk_used_pct, worker_count]
        self.x = np.array([[0.0], [0.0], [0.0], [0.0], [0.0], [0.0]])
        
        # State transition matrix (assuming each resource persists with some velocity)
        # For simplicity, we assume each state persists to the next timestep
        # This is a simple model - could be enhanced with velocity terms
        self.F = np.eye(6)
        
        # Measurement matrix (we observe all 6 resources directly)
        self.H = np.eye(6)
        
        # Covariance matrix (high initial uncertainty)
        base_noise = measurement_noise or default_noise
        self.P = np.diag([base_noise[r] for r in self.RESOURCES])
        
        # Process noise matrix (how erratic is each resource)
        self.Q = np.eye(6) * process_noise
        
        # Measurement noise matrix (how noisy are observations)
        self.R = np.diag([base_noise[r] for r in self.RESOURCES])
        
        # Resource-specific constraints and thresholds
        self.thresholds = {
            'tokens': 5e7,           # 50M tokens = genuinely high (normal daily ~20M)
            'cpu_load': 8.0,         # 8.0 load = high CPU
            'memory_pct': 85.0,      # 85% = high memory usage (7G/7G)
            'swap_used_pct': 80.0,   # 80% swap = only alert if genuinely high (cold pages at 60% are normal)
            'disk_used_pct': 85.0,   # 85% disk used = warning
            'worker_count': 40       # 40 workers = high concurrent load
        }
        
        self._initialized = False
        self._update_count = 0

    def update(self, measurement: Dict[str, float]) -> None:
        """Incorporate new measurements for all resources.
        
        Args:
            measurement: Dict with keys from RESOURCES and float values
        """
        if not all(r in measurement for r in self.RESOURCES):
            raise ValueError(f"Measurement must contain all resources: {self.RESOURCES}")
        
        z = np.array([[measurement[r]] for r in self.RESOURCES])
        
        if not self._initialized:
            self.x = z
            self._initialized = True
            self._update_count += 1
            return
        
        # Kalman filter update equations
        # Innovation: y = z - H * x
        y = z - self.H @ self.x
        
        # Innovation covariance: S = H * P * H^T + R
        S = self.H @ self.P @ self.H.T + self.R
        
        # Kalman gain: K = P * H^T * S^-1
        try:
            K = self.P @ self.H.T @ np.linalg.inv(S)
        except np.linalg.LinAlgError:
            # If singular, use pseudo-inverse as fallback
            K = self.P @ self.H.T @ np.linalg.pinv(S)
        
        # State update: x = x + K * y
        self.x = self.x + K @ y
        
        # Covariance update: P = (I - K * H) * P
        I = np.eye(6)
        self.P = (I - K @ self.H) @ self.P
        
        # Ensure P stays positive semi-definite
        self.P = (self.P + self.P.T) / 2
        
        self._update_count += 1

    def predict(self) -> np.ndarray:
        """Predict next state for all resources.
        
        Returns:
            State vector [tokens, cpu_load, memory_pct, worker_count]
        """
        if not self._initialized:
            raise RuntimeError("Predictor not initialized - call update() first")
        
        # State prediction: x = F * x
        self.x = self.F @ self.x
        
        # Covariance prediction: P = F * P * F^T + Q
        self.P = self.F @ self.P @ self.F.T + self.Q
        
        return self.x.flatten()

    def predict_steps_ahead(self, steps: int, minutes_per_step: int = 5) -> List[Dict[str, float]]:
        """Project N steps ahead for all resources.
        
        Args:
            steps: Number of steps to predict
            minutes_per_step: Minutes per prediction step (default: 5)
            
        Returns:
            List of prediction dicts, one per step
        """
        if not self._initialized:
            raise RuntimeError("Predictor not initialized - call update() first")
        
        # Save current state
        x_orig = self.x.copy()
        P_orig = self.P.copy()
        
        predictions = []
        
        for step in range(1, steps + 1):
            # Predict next state
            self.predict()
            
            # Create prediction dict with confidence intervals
            pred_dict = {}
            for i, resource in enumerate(self.RESOURCES):
                value = float(self.x[i, 0])
                uncertainty = float(np.sqrt(self.P[i, i]))
                
                pred_dict[resource] = {
                    'value': value,
                    'uncertainty': uncertainty,
                    'lower_bound': max(0, value - 1.96 * uncertainty),  # 95% CI
                    'upper_bound': value + 1.96 * uncertainty,
                    'minutes_ahead': step * minutes_per_step
                }
            
            predictions.append(pred_dict)
        
        # Restore original state
        self.x = x_orig
        self.P = P_orig
        
        return predictions

    def get_resource_warnings(self, minutes_ahead: int = 30) -> List[Dict[str, Any]]:
        """Get early warnings for resource exhaustion.
        
        Args:
            minutes_ahead: Minutes ahead to predict (default: 30)
            
        Returns:
            List of warning dicts for resources approaching thresholds
        """
        steps = max(1, minutes_ahead // 5)  # 5-minute steps
        predictions = self.predict_steps_ahead(steps)
        
        if not predictions:
            return []
        
        warnings = []
        final_pred = predictions[-1]
        
        for resource in self.RESOURCES:
            if resource not in final_pred:
                continue
                
            pred_data = final_pred[resource]
            threshold = self.thresholds.get(resource)
            current_value = float(self.x[self.RESOURCE_IDX[resource], 0])
            
            if threshold is None:
                continue
            
            # Check if prediction exceeds threshold
            if pred_data['upper_bound'] > threshold:
                time_to_threshold = None
                
                # Estimate time until threshold based on current trend
                if current_value < threshold:
                    # Simple linear extrapolation
                    rate = (pred_data['value'] - current_value) / minutes_ahead
                    if rate > 0:
                        time_to_threshold = (threshold - current_value) / rate
                
                warnings.append({
                    'resource': resource,
                    'severity': 'critical' if pred_data['value'] > threshold else 'warning',
                    'current_value': current_value,
                    'predicted_value': pred_data['value'],
                    'predicted_upper': pred_data['upper_bound'],
                    'threshold': threshold,
                    'minutes_ahead': minutes_ahead,
                    'time_to_threshold_minutes': time_to_threshold,
                    'confidence': 1.0 - (pred_data['uncertainty'] / current_value) if current_value > 0 else 0.0
                })
        
        # Sort by severity and time to threshold
        warnings.sort(key=lambda w: (
            0 if w['severity'] == 'critical' else 1,
            w['time_to_threshold_minutes'] or float('inf')
        ))
        
        return warnings

    def get_correlation_matrix(self) -> np.ndarray:
        """Get the correlation matrix showing resource relationships.
        
        Returns:
            6x6 correlation matrix (-1 to 1)
        """
        if not self._initialized:
            return np.eye(6)
        
        # Convert covariance to correlation
        # corr(i,j) = cov(i,j) / sqrt(cov(i,i) * cov(j,j))
        std_devs = np.sqrt(np.diag(self.P))
        correlation = np.zeros((6, 6))
        
        for i in range(6):
            for j in range(6):
                if std_devs[i] > 0 and std_devs[j] > 0:
                    correlation[i, j] = self.P[i, j] / (std_devs[i] * std_devs[j])
                else:
                    correlation[i, j] = 0.0
        
        return correlation

    def get_system_health_score(self) -> Dict[str, float]:
        """Calculate overall system health score (0-100).
        
        Returns:
            Dict with health metrics and overall score
        """
        if not self._initialized:
            return {'overall': 0.0, 'details': 'Not initialized'}
        
        current_state = self.x.flatten()
        health_scores = {}
        
        for i, resource in enumerate(self.RESOURCES):
            value = float(current_state[i])
            threshold = self.thresholds.get(resource)
            
            if threshold is None:
                health_scores[resource] = 100.0
                continue
            
            # Health score: 100 at 0%, 0 at threshold or above
            health = max(0, min(100, 100 * (1 - value / threshold)))
            health_scores[resource] = health
        
        # Overall health: weighted average
        weights = {'tokens': 0.3, 'cpu_load': 0.15, 'memory_pct': 0.2, 'swap_used_pct': 0.15, 'disk_used_pct': 0.1, 'worker_count': 0.1}
        overall = sum(health_scores[r] * weights[r] for r in self.RESOURCES) / sum(weights.values())
        
        return {
            'overall': overall,
            'per_resource': health_scores,
            'update_count': self._update_count,
            'timestamp': time.time()
        }

    @property
    def state_vector(self) -> np.ndarray:
        """Get current state vector."""
        return self.x.flatten()

    @property
    def covariance_matrix(self) -> np.ndarray:
        """Get current covariance matrix."""
        return self.P.copy()

    @property
    def is_initialized(self) -> bool:
        """Check if predictor is initialized."""
        return self._initialized

    def to_dict(self) -> Dict[str, Any]:
        """Convert predictor state to serializable dict."""
        return {
            'state_vector': self.x.flatten().tolist(),
            'covariance_matrix': self.P.tolist(),
            'thresholds': self.thresholds,
            'update_count': self._update_count,
            'initialized': self._initialized,
            'timestamp': time.time()
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'MultiResourceKalmanPredictor':
        """Create predictor from serializable dict."""
        predictor = cls()
        predictor.x = np.array(data['state_vector']).reshape(6, 1)
        predictor.P = np.array(data['covariance_matrix'])
        predictor.thresholds = data.get('thresholds', predictor.thresholds)
        predictor._update_count = data.get('update_count', 0)
        predictor._initialized = data.get('initialized', False)
        return predictor


# ── Data access functions ─────────────────────────────────────────────────────

def get_resource_history(hours: int = 12, 
                        db_path: str = "~/.hermes/bot/zai_usage.db") -> List[Dict]:
    """Read resource metrics from database.
    
    Args:
        hours: Hours of history to retrieve
        db_path: Path to SQLite database
        
    Returns:
        List of resource measurement dicts
    """
    import os
    import sqlite3
    
    db_path = os.path.expanduser(db_path)
    if not os.path.exists(db_path):
        return []
    
    cutoff = time.time() - (hours * 3600)
    
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute(
            """SELECT ts, cpu_load_1m, memory_used_percent, swap_used_percent,
                      disk_used_percent, worker_count
               FROM resource_metrics 
               WHERE ts >= ? 
               ORDER BY ts""",
            (cutoff,)
        )
        
        rows = cursor.fetchall()
        conn.close()
        
        # Convert to dicts and calculate token burn rate
        history = []
        for row in rows:
            # Calculate token burn rate from api_calls table for this time window
            tokens_per_hour = _calculate_token_burn_rate(row['ts'], db_path)
            
            history.append({
                'timestamp': row['ts'],
                'tokens': tokens_per_hour,
                'cpu_load': row['cpu_load_1m'] or 0.0,
                'memory_pct': row['memory_used_percent'] or 0.0,
                'swap_used_pct': row['swap_used_percent'] or 0.0,
                'disk_used_pct': row['disk_used_percent'] or 0.0,
                'worker_count': row['worker_count'] or 0
            })
        
        return history
        
    except Exception as e:
        print(f"Error reading resource history: {e}")
        return []


def _calculate_token_burn_rate(timestamp: float, db_path: str) -> float:
    """Calculate tokens per hour around the given timestamp."""
    import os
    import sqlite3
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Get tokens for 1-hour window around the timestamp
        start_ts = timestamp - 1800  # 30 minutes before
        end_ts = timestamp + 1800    # 30 minutes after
        
        cursor.execute(
            """SELECT SUM(total_tokens) as total_tokens
               FROM api_calls
               WHERE ts >= ? AND ts < ? AND status_code = 200""",
            (start_ts, end_ts)
        )
        
        result = cursor.fetchone()
        conn.close()
        
        total_tokens = result[0] if result and result[0] else 0
        # Convert 1-hour total to per-hour rate
        return float(total_tokens)
        
    except Exception:
        return 0.0


# ── Example usage ─────────────────────────────────────────────────────────────

def demo_multi_resource_predictor():
    """Demonstrate the multi-resource Kalman predictor."""
    if not _HAS_NUMPY:
        print("Error: numpy not available")
        return
    
    print("Multi-Resource Kalman Predictor Demo")
    print("=" * 40)
    
    # Get some real data
    history = get_resource_history(hours=2)
    if not history:
        print("No resource history available - using synthetic data")
        # Create synthetic data for demo
        base_time = time.time() - 7200  # 2 hours ago
        history = []
        for i in range(24):  # 24 * 5 minutes = 2 hours
            ts = base_time + i * 300
            history.append({
                'timestamp': ts,
                'tokens': 500000 + i * 10000,  # Increasing burn rate
                'cpu_load': 2.0 + i * 0.1,    # Increasing CPU
                'memory_pct': 60 + i * 0.5,   # Increasing memory
                'swap_used_pct': 10 + i * 0.3, # Increasing swap
                'disk_used_pct': 70 + i * 0.1, # Slowly increasing disk
                'worker_count': 10 + (i // 4) # Workers every 20 minutes
            })
    
    # Initialize predictor
    predictor = MultiResourceKalmanPredictor(
        process_noise=0.5,
        measurement_noise={
            'tokens': 50000,
            'cpu_load': 0.3,
            'memory_pct': 3.0,
            'swap_used_pct': 10.0,
            'disk_used_pct': 2.0,
            'worker_count': 1.0
        }
    )
    
    print(f"Training on {len(history)} data points...")
    
    # Train on historical data
    for i, point in enumerate(history):
        measurement = {
            'tokens': point['tokens'],
            'cpu_load': point['cpu_load'],
            'memory_pct': point['memory_pct'],
            'swap_used_pct': point.get('swap_used_pct', 0),
            'disk_used_pct': point.get('disk_used_pct', 0),
            'worker_count': point['worker_count']
        }
        predictor.update(measurement)
    
    print(f"Predictor trained with {predictor._update_count} updates")
    
    # Get current state
    current_state = predictor.state_vector
    print(f"\nCurrent state:")
    for i, resource in enumerate(MultiResourceKalmanPredictor.RESOURCES):
        print(f"  {resource}: {current_state[i]:.2f}")
    
    # Predict 30 minutes ahead
    predictions = predictor.predict_steps_ahead(6)  # 6 * 5min = 30min
    
    print(f"\nPredictions (30 minutes ahead):")
    for i, pred in enumerate(predictions[-1:]):  # Show final prediction
        step_time = (i + 1) * 5
        print(f"  At +{step_time}min:")
        for resource in MultiResourceKalmanPredictor.RESOURCES:
            data = pred[resource]
            print(f"    {resource}: {data['value']:.2f} ±{data['uncertainty']:.2f}")
    
    # Get warnings
    warnings = predictor.get_resource_warnings(minutes_ahead=30)
    if warnings:
        print(f"\nResource Warnings:")
        for warning in warnings:
            print(f"  {warning['resource'].upper()}: {warning['severity']}")
            print(f"    Current: {warning['current_value']:.2f}")
            print(f"    Predicted: {warning['predicted_value']:.2f}")
            print(f"    Threshold: {warning['threshold']:.2f}")
            if warning['time_to_threshold_minutes']:
                print(f"    Time to threshold: {warning['time_to_threshold_minutes']:.1f} min")
    else:
        print(f"\nNo resource warnings for next 30 minutes")
    
    # Get health score
    health = predictor.get_system_health_score()
    print(f"\nSystem Health Score: {health['overall']:.1f}/100")
    for resource, score in health['per_resource'].items():
        print(f"  {resource}: {score:.1f}/100")
    
    # Get correlations
    correlations = predictor.get_correlation_matrix()
    print(f"\nResource Correlations:")
    for i, r1 in enumerate(MultiResourceKalmanPredictor.RESOURCES):
        for j, r2 in enumerate(MultiResourceKalmanPredictor.RESOURCES):
            if i <= j:  # Only show upper triangle
                corr = correlations[i, j]
                print(f"  {r1} <-> {r2}: {corr:.3f}")


if __name__ == "__main__":
    demo_multi_resource_predictor()