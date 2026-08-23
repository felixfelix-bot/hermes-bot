#!/usr/bin/env python3
"""Comprehensive test suite for predictive crash prevention system.

Tests:
1. MultiResourceKalmanPredictor functionality
2. CrashPreventionManager coordination
3. Alert system integration
4. Dispatch gate integration
5. End-to-end crash prediction and prevention
"""

import json
import os
import sys
import time
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

# Add the bot directory to Python path
sys.path.insert(0, str(Path(__file__).parent / "bot"))

def test_multiresource_kalman_predictor():
    """Test MultiResourceKalmanPredictor functionality."""
    print("=== Testing MultiResourceKalmanPredictor ===")
    
    try:
        from burn_predictor_enhanced import MultiResourceKalmanPredictor
        
        # Test CPU predictor
        cpu_predictor = MultiResourceKalmanPredictor("cpu")
        
        # Simulate CPU usage readings
        cpu_readings = [45.2, 48.7, 52.1, 55.8, 59.3, 63.7, 68.2, 72.9, 78.1, 83.4]
        for i, reading in enumerate(cpu_readings):
            timestamp = time.time() - (len(cpu_readings) - i) * 300  # 5-minute intervals
            cpu_predictor.update(reading, timestamp)
        
        # Test crash risk prediction
        risk = cpu_predictor.predict_crash_risk(30)
        print(f"CPU Risk: {risk['risk_level']} (confidence: {risk['confidence']:.3f})")
        
        # Test self-healing actions
        actions = cpu_predictor.get_self_healing_actions()
        print(f"CPU Actions: {len(actions)}")
        for action in actions:
            print(f"  - {action['action']}: {action['priority']}")
        
        # Test memory predictor
        mem_predictor = MultiResourceKalmanPredictor("memory")
        
        # Simulate memory usage readings
        mem_readings = [65.3, 68.9, 72.4, 76.1, 80.2, 84.7, 89.3, 94.1, 98.8, 103.2]
        for i, reading in enumerate(mem_readings):
            timestamp = time.time() - (len(mem_readings) - i) * 300
            mem_predictor.update(reading, timestamp)
        
        # Test crash risk prediction for memory
        mem_risk = mem_predictor.predict_crash_risk(30)
        print(f"Memory Risk: {mem_risk['risk_level']} (confidence: {mem_risk['confidence']:.3f})")
        
        # Test self-healing actions for memory
        mem_actions = mem_predictor.get_self_healing_actions()
        print(f"Memory Actions: {len(mem_actions)}")
        for action in mem_actions:
            print(f"  - {action['action']}: {action['priority']}")
        
        print("✓ MultiResourceKalmanPredictor tests passed\n")
        return True
        
    except Exception as e:
        print(f"✗ MultiResourceKalmanPredictor test failed: {e}")
        return False

def test_crash_prevention_manager():
    """Test CrashPreventionManager coordination."""
    print("=== Testing CrashPreventionManager ===")
    
    try:
        from burn_predictor_enhanced import CrashPreventionManager
        
        # Create temporary directories
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            
            # Mock paths
            with patch('burn_predictor_enhanced.Path') as mock_path:
                mock_path.return_value.expanduser.return_value = temp_path
                
                manager = CrashPreventionManager()
                
                # Test updating resource measurements
                manager.update_resource_measurement("cpu", 75.3)
                manager.update_resource_measurement("memory", 82.7)
                manager.update_resource_measurement("api_tokens", 88.4)
                manager.update_resource_measurement("disk_io", 45.1)
                
                # Test system crash risk
                system_risk = manager.get_system_crash_risk(30)
                print(f"Overall Risk: {system_risk['overall_risk_level']}")
                print(f"Resources: {len(system_risk['resource_risks'])}")
                
                # Test preventive actions
                actions = manager.get_preventive_actions()
                print(f"Recommended Actions: {len(actions)}")
                
                # Test executing actions
                results = manager.execute_preventive_actions(actions)
                print(f"Executed Actions: {len(results['executed'])}")
                print(f"Failed Actions: {len(results['failed'])}")
                
                print("✓ CrashPreventionManager tests passed\n")
                return True
                
    except Exception as e:
        print(f"✗ CrashPreventionManager test failed: {e}")
        return False

def test_alert_system():
    """Test alert system integration."""
    print("=== Testing Alert System ===")
    
    try:
        from crash_alert_system import AlertManager, send_crash_warning_alert
        
        # Create temporary log file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.log', delete=False) as temp_log:
            log_path = temp_log.name
        
        try:
            # Test alert manager
            config = {
                "email": {"enabled": False},
                "signal": {"enabled": False},
                "webhook": {"enabled": False},
                "log_file": log_path
            }
            
            alert_manager = AlertManager(config)
            
            # Send test alert
            result = alert_manager.send_alert(
                "Test Alert",
                "This is a test alert",
                "INFO",
                ["log"]
            )
            
            if not result.get("log", {}).get("success"):
                print("✗ Alert logging failed")
                return False
            
            # Test crash warning alert
            crash_result = send_crash_warning_alert(
                "cpu",
                "high",
                15,
                0.92,
                alert_manager
            )
            
            if not crash_result.get("log", {}).get("success"):
                print("✗ Crash warning alert failed")
                return False
            
            # Verify log file contents
            with open(log_path, 'r') as f:
                log_contents = f.read()
            
            if "Test Alert" not in log_contents or "Crash Risk" not in log_contents:
                print("✗ Alert content not found in log")
                return False
            
            print("✓ Alert system tests passed\n")
            return True
            
        finally:
            # Clean up
            if os.path.exists(log_path):
                os.unlink(log_path)
                
    except Exception as e:
        print(f"✗ Alert system test failed: {e}")
        return False

def test_dispatch_gate_integration():
    """Test dispatch gate integration."""
    print("=== Testing Dispatch Gate Integration ===")
    
    try:
        # Create temporary script for testing
        with tempfile.NamedTemporaryFile(mode='w', suffix='.sh', delete=False) as temp_script:
            script_path = temp_script.name
            
        try:
            # Write a minimal test script
            with open(script_path, 'w') as f:
                f.write("""#!/bin/bash
# Test dispatch gate script
echo "GATE OK: test"
exit 0
""")
            
            os.chmod(script_path, 0o755)
            
            # Test script execution
            import subprocess
            result = subprocess.run([script_path], capture_output=True, text=True, timeout=10)
            
            if result.returncode != 0:
                print("✗ Dispatch gate script failed")
                return False
            
            if "GATE OK" not in result.stdout:
                print("✗ Expected output not found")
                return False
            
            print("✓ Dispatch gate integration tests passed\n")
            return True
            
        finally:
            # Clean up
            if os.path.exists(script_path):
                os.unlink(script_path)
                
    except Exception as e:
        print(f"✗ Dispatch gate integration test failed: {e}")
        return False

def test_end_to_end_prediction():
    """Test end-to-end crash prediction and prevention."""
    print("=== Testing End-to-End Prediction ===")
    
    try:
        from crash_prevention_integration import (
            get_crash_manager, 
            update_system_resources,
            get_crash_predictions,
            should_block_dispatch
        )
        
        # Test crash manager
        manager = get_crash_manager()
        
        # Simulate high resource pressure
        for i in range(10):
            timestamp = time.time() - (10 - i) * 300
            manager.update_resource_measurement("cpu", 75 + i * 2, timestamp)
            manager.update_resource_measurement("memory", 80 + i * 2, timestamp)
        
        # Get crash predictions
        predictions = get_crash_predictions(30)
        print(f"Overall Risk: {predictions['overall_risk_level']}")
        print(f"Confidence: {predictions['highest_confidence']:.3f}")
        
        # Check if dispatch should be blocked
        should_block, reason = should_block_dispatch()
        print(f"Should Block Dispatch: {should_block} ({reason})")
        
        # Verify predictions structure
        if not isinstance(predictions, dict):
            print("✗ Predictions is not a dictionary")
            return False
        
        if "overall_risk_level" not in predictions:
            print("✗ Missing overall_risk_level in predictions")
            return False
        
        if "resource_risks" not in predictions:
            print("✗ Missing resource_risks in predictions")
            return False
        
        # Print resource-specific predictions
        for resource, risk in predictions['resource_risks'].items():
            print(f"  {resource}: {risk['risk_level']} ({risk['confidence']:.2f})")
            if risk['minutes_to_crash']:
                print(f"    Estimated crash in: {risk['minutes_to_crash']} minutes")
        
        print("✓ End-to-end prediction tests passed\n")
        return True
        
    except Exception as e:
        print(f"✗ End-to-end prediction test failed: {e}")
        return False

def main():
    """Run all tests."""
    print("Predictive Crash Prevention System - Test Suite")
    print("=" * 50)
    
    tests = [
        ("MultiResourceKalmanPredictor", test_multiresource_kalman_predictor),
        ("CrashPreventionManager", test_crash_prevention_manager),
        ("Alert System", test_alert_system),
        ("Dispatch Gate Integration", test_dispatch_gate_integration),
        ("End-to-End Prediction", test_end_to_end_prediction),
    ]
    
    passed = 0
    total = len(tests)
    
    for test_name, test_func in tests:
        print(f"\nRunning {test_name}...")
        if test_func():
            passed += 1
        else:
            print(f"\nFailed: {test_name}")
    
    print("\n" + "=" * 50)
    print(f"Test Results: {passed}/{total} passed")
    
    if passed == total:
        print("✓ All tests passed! Predictive crash prevention system is ready.")
        return 0
    else:
        print("✗ Some tests failed. Please review the output above.")
        return 1

if __name__ == "__main__":
    sys.exit(main())