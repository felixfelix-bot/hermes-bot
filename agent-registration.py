#!/usr/bin/env python3
"""
Agent Registration and Coordination System
Coordinates multiple agents to prevent independent API hammering
"""

import os
import sys
import json
import time
import subprocess
import argparse
from pathlib import Path
from typing import Dict, List, Optional
import signal
import atexit

class AgentCoordinator:
    """Python interface to the agent coordination system"""
    
    def __init__(self, coordinator_script: Optional[str] = None):
        self.coordinator_script = coordinator_script or os.path.expanduser(
            "~/.hermes/profiles/manager/scripts/agent-coordinator.sh"
        )
        self.agent_id = None
        self.current_lock = None
        
        # Register cleanup on exit
        atexit.register(self.cleanup)
        
    def register_agent(self, agent_id: str, agent_type: str = "standard") -> bool:
        """Register this agent with the coordination system"""
        self.agent_id = agent_id
        
        try:
            result = subprocess.run([
                self.coordinator_script, "register", agent_id, agent_type
            ], capture_output=True, text=True, check=True)
            
            print(f"Registered agent {agent_id} ({agent_type})")
            return True
            
        except subprocess.CalledProcessError as e:
            print(f"Failed to register agent {agent_id}: {e}")
            return False
    
    def unregister_agent(self) -> bool:
        """Unregister this agent"""
        if not self.agent_id:
            return True
            
        try:
            subprocess.run([
                self.coordinator_script, "unregister", self.agent_id
            ], capture_output=True, text=True, check=True)
            
            print(f"Unregistered agent {self.agent_id}")
            self.agent_id = None
            return True
            
        except subprocess.CalledProcessError as e:
            print(f"Failed to unregister agent {self.agent_id}: {e}")
            return False
    
    def coordinate_request(self, request_type: str) -> Optional[str]:
        """
        Request permission to make an API call
        Returns lock file path if granted, None if denied
        """
        if not self.agent_id:
            print("Agent not registered")
            return None
            
        try:
            result = subprocess.run([
                self.coordinator_script, "request", self.agent_id, request_type
            ], capture_output=True, text=True, check=True)
            
            response = result.stdout.strip()
            
            if response.startswith("REQUEST_GRANTED:"):
                lock_file = response.split(":", 1)[1]
                self.current_lock = lock_file
                print(f"Request granted: {request_type}")
                return lock_file
            else:
                print(f"Request denied: {response}")
                return None
                
        except subprocess.CalledProcessError as e:
            print(f"Failed to coordinate request: {e}")
            return None
    
    def release_request(self) -> bool:
        """Release the current request lock"""
        if not self.current_lock:
            return True
            
        try:
            subprocess.run([
                self.coordinator_script, "release", self.current_lock
            ], capture_output=True, text=True, check=True)
            
            print(f"Released request lock: {self.current_lock}")
            self.current_lock = None
            return True
            
        except subprocess.CalledProcessError as e:
            print(f"Failed to release request lock: {e}")
            return False
    
    def suspend_non_critical(self) -> bool:
        """Suspend non-critical agents"""
        try:
            subprocess.run([
                self.coordinator_script, "suspend"
            ], capture_output=True, text=True, check=True)
            
            print("Suspended non-critical agents")
            return True
            
        except subprocess.CalledProcessError as e:
            print(f"Failed to suspend non-critical agents: {e}")
            return False
    
    def get_status(self) -> Dict:
        """Get current system status"""
        try:
            result = subprocess.run([
                self.coordinator_script, "status"
            ], capture_output=True, text=True, check=True)
            
            return {
                "status": "ok",
                "output": result.stdout
            }
            
        except subprocess.CalledProcessError as e:
            return {
                "status": "error",
                "error": str(e)
            }
    
    def cleanup(self):
        """Cleanup resources on exit"""
        if self.current_lock:
            self.release_request()
        if self.agent_id:
            self.unregister_agent()


class APICoordinator:
    """High-level API coordination wrapper"""
    
    def __init__(self, agent_id: str, agent_type: str = "standard"):
        self.coordinator = AgentCoordinator()
        self.agent_id = agent_id
        self.agent_type = agent_type
        self.registered = False
        
    def __enter__(self):
        """Context manager entry"""
        self.register()
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.cleanup()
        
    def register(self) -> bool:
        """Register the agent"""
        if self.registered:
            return True
            
        success = self.coordinator.register_agent(self.agent_id, self.agent_type)
        if success:
            self.registered = True
        return success
    
    def cleanup(self):
        """Cleanup and unregister"""
        if self.registered:
            self.coordinator.unregister_agent()
            self.registered = False
    
    def make_coordinated_request(self, request_func, *args, **kwargs):
        """
        Make a coordinated API request
        request_func: the function that makes the actual API call
        """
        if not self.registered:
            if not self.register():
                raise RuntimeError("Failed to register agent")
        
        # Request permission
        lock_file = self.coordinator.coordinate_request("api_call")
        if not lock_file:
            raise RuntimeError("Request denied by coordinator")
        
        try:
            # Make the actual API call
            result = request_func(*args, **kwargs)
            return result
        finally:
            # Always release the lock
            self.coordinator.release_request()


def setup_signal_handlers(coordinator: AgentCoordinator):
    """Setup signal handlers for graceful shutdown"""
    def signal_handler(signum, frame):
        print(f"Received signal {signum}, cleaning up...")
        coordinator.cleanup()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)


def main():
    """Command line interface"""
    parser = argparse.ArgumentParser(description="Agent Registration and Coordination")
    parser.add_argument("command", choices=["register", "unregister", "status", "suspend"])
    parser.add_argument("--agent-id", required=True, help="Agent ID")
    parser.add_argument("--agent-type", default="standard", 
                       help="Agent type (standard, critical, essential)")
    parser.add_argument("--coordinator-script", 
                       help="Path to coordinator script")
    
    args = parser.parse_args()
    
    coordinator = AgentCoordinator(args.coordinator_script)
    setup_signal_handlers(coordinator)
    
    if args.command == "register":
        success = coordinator.register_agent(args.agent_id, args.agent_type)
        sys.exit(0 if success else 1)
        
    elif args.command == "unregister":
        success = coordinator.unregister_agent()
        sys.exit(0 if success else 1)
        
    elif args.command == "status":
        status = coordinator.get_status()
        print(status.get("output", "No status available"))
        sys.exit(0 if status.get("status") == "ok" else 1)
        
    elif args.command == "suspend":
        success = coordinator.suspend_non_critical()
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()