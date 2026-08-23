#!/usr/bin/env python3
"""
Key coordinator utilities for shell script integration
"""

import sys
import os
import json
from pathlib import Path

# Add the bot directory to Python path
sys.path.insert(0, os.path.expanduser("~/.hermes/bot"))

from intelligent_key_selector import IntelligentKeySelector, KeyType, KeyStatus

def main():
    database_path = os.path.expanduser("~/.hermes/bot/key_selector.db")
    
    if len(sys.argv) < 2:
        print("Usage: key_coordinator_utils.py <command> [args...]", file=sys.stderr)
        sys.exit(1)
    
    command = sys.argv[1]
    selector = IntelligentKeySelector(database_path)
    
    try:
        if command == "get-key":
            key_id, key_info = selector.select_key()
            print(f"{key_id} {key_info.key_type.value}")
            
        elif command == "status":
            status = selector.get_key_status()
            for key_id, info in status.items():
                backoff_str = info['backoff_until'] if info['backoff_until'] else "none"
                print(f"{key_id}: {info['status'].value} (backoff: {backoff_str})")
                
        elif command == "report-rate-limit":
            if len(sys.argv) < 3:
                print("Error: key_id required", file=sys.stderr)
                sys.exit(1)
            key_id = sys.argv[2]
            selector.report_429(key_id)
            print(f"Rate limit reported for key: {key_id}")
            
        elif command == "report-success":
            if len(sys.argv) < 3:
                print("Error: key_id required", file=sys.stderr)
                sys.exit(1)
            key_id = sys.argv[2]
            selector.report_success(key_id)
            print(f"Success reported for key: {key_id}")
            
        elif command == "stats":
            stats = selector.get_statistics()
            for key_id, stat in stats.items():
                success_rate = stat['success_rate'] * 100
                print(f"{key_id}: {stat['selections']} selections, {stat['rate_limits']} rate limits, {success_rate:.1f}% success rate")
                
        elif command == "reset-key":
            if len(sys.argv) < 3:
                print("Error: key_id required", file=sys.stderr)
                sys.exit(1)
            key_id = sys.argv[2]
            selector.reset_key_state(key_id)
            print(f"Key {key_id} reset to available state")
            
        elif command == "update-api-key":
            if len(sys.argv) < 4:
                print("Error: key_id and api_key required", file=sys.stderr)
                sys.exit(1)
            key_id = sys.argv[2]
            api_key = sys.argv[3]
            selector.update_api_key(key_id, api_key)
            print(f"API key updated for: {key_id}")
            
        else:
            print(f"Error: Unknown command: {command}", file=sys.stderr)
            sys.exit(1)
            
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()