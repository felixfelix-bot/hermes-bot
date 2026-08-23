#!/usr/bin/env python3
"""Crash prevention integration module.

This module provides the interface for the enhanced burn_predictor with
crash prevention capabilities.
"""

from burn_predictor_enhanced import CrashPreventionManager, MultiResourceKalmanPredictor

# Global instance
_crash_manager = None

def get_crash_manager():
    """Get or create the global crash prevention manager."""
    global _crash_manager
    if _crash_manager is None:
        _crash_manager = CrashPreventionManager()
        _crash_manager.load_state()
    return _crash_manager

def update_system_resources():
    """Update system resource measurements."""
    manager = get_crash_manager()
    
    # Get current system metrics
    try:
        import subprocess
        import time
        
        # CPU usage
        result = subprocess.run(
            ["top", "-bn1"], capture_output=True, text=True, timeout=5
        )
        cpu_usage = 0.0  # Parse from result if needed
        
        # Memory usage
        with open('/proc/meminfo') as f:
            mem_info = f.read()
        total_mem = 0
        avail_mem = 0
        for line in mem_info.split('\n'):
            if 'MemTotal:' in line:
                total_mem = int(line.split()[1])
            elif 'MemAvailable:' in line:
                avail_mem = int(line.split()[1])
        
        mem_usage = ((total_mem - avail_mem) / total_mem) * 100 if total_mem > 0 else 0
        
        # API token usage (from existing predictor)
        from burn_predictor import predict_exhaustion
        api_predictions = predict_exhaustion("ours")
        api_usage = api_predictions[0].get("used_pct", 0) if api_predictions else 0
        
        # Disk I/O (simplified)
        disk_usage = 0.0  # Could be enhanced with actual disk I/O metrics
        
        # Update all predictors
        manager.update_resource_measurement("cpu", cpu_usage, time.time())
        manager.update_resource_measurement("memory", mem_usage, time.time())
        manager.update_resource_measurement("api_tokens", api_usage, time.time())
        manager.update_resource_measurement("disk_io", disk_usage, time.time())
        
    except Exception:
        # Don't fail if we can't get metrics
        pass

def get_crash_predictions(lookahead_minutes=30):
    """Get crash predictions for all resources."""
    manager = get_crash_manager()
    return manager.get_system_crash_risk(lookahead_minutes)

def execute_preventive_actions():
    """Execute recommended preventive actions."""
    manager = get_crash_manager()
    actions = manager.get_preventive_actions()
    return manager.execute_preventive_actions(actions)

def should_block_dispatch():
    """Check if dispatch should be blocked due to crash risk."""
    risk = get_crash_predictions(30)
    
    # Block if critical risk detected
    if risk["overall_risk_level"] in ["high", "critical"]:
        return True, risk["overall_risk_level"]
    
    return False, risk["overall_risk_level"]

if __name__ == "__main__":
    # Test the crash prevention system
    update_system_resources()
    predictions = get_crash_predictions()
    print("System Crash Risk Predictions:")
    print(f"Overall Risk: {predictions['overall_risk_level']}")
    print(f"Confidence: {predictions['highest_confidence']:.3f}")
    
    for resource, risk in predictions['resource_risks'].items():
        print(f"  {resource}: {risk['risk_level']} ({risk['confidence']:.2f} confidence)")
        if risk['minutes_to_crash']:
            print(f"    Estimated crash in: {risk['minutes_to_crash']} minutes")
    
    # Check and execute preventive actions
    should_block, reason = should_block_dispatch()
    if should_block:
        print(f"\nWARNING: Should block dispatch - {reason}")
        
        actions = execute_preventive_actions()
        print(f"Executed {len(actions['executed'])} preventive actions")
        if actions['failed']:
            print(f"Failed to execute {len(actions['failed'])} actions")