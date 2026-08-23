#!/usr/bin/env python3
"""
Example: How to integrate Intelligent Key Selection with existing agent scripts

This demonstrates how to modify an existing agent script to use
intelligent key selection instead of exponential backoff.
"""

import sys
import os
import time
import requests
from datetime import datetime

# Add the bot directory to Python path
sys.path.insert(0, os.path.expanduser("~/.hermes/bot"))

from intelligent_key_selector import IntelligentKeySelector

class EnhancedAgentScript:
    """
    Example of an agent script enhanced with intelligent key selection.
    """
    
    def __init__(self):
        self.key_selector = IntelligentKeySelector()
        self.current_key = None
        self.current_key_info = None
        
    def _make_api_request(self, endpoint: str, method: str = "GET", data: dict = None) -> dict:
        """
        Make an API request with intelligent key selection and automatic retry logic.
        """
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # Select optimal key
                key_id, key_info = self.key_selector.select_key()
                self.current_key = key_id
                self.current_key_info = key_info
                
                print(f"Using key: {key_id} (type: {key_info.key_type.value})")
                
                # Prepare headers with API key
                headers = {
                    "Authorization": f"Bearer {key_info.api_key}",
                    "Content-Type": "application/json"
                }
                
                # Make the request
                url = f"https://api.example.com/{endpoint}"
                
                if method.upper() == "GET":
                    response = requests.get(url, headers=headers)
                elif method.upper() == "POST":
                    response = requests.post(url, json=data, headers=headers)
                else:
                    raise ValueError(f"Unsupported method: {method}")
                
                # Handle response
                if response.status_code == 200:
                    # Success!
                    self.key_selector.report_success(key_id)
                    return response.json()
                    
                elif response.status_code == 429:
                    # Rate limited - apply backoff
                    print(f"Rate limited on key {key_id}, applying backoff...")
                    self.key_selector.report_429(key_id, dict(response.headers))
                    
                    # Wait a bit before next attempt
                    time.sleep(1)
                    continue
                    
                else:
                    # Other error
                    error_msg = f"API error: {response.status_code}"
                    if response.text:
                        error_msg += f" - {response.text}"
                    
                    self.key_selector.report_error(key_id, "http_error", error_msg)
                    raise Exception(error_msg)
                    
            except Exception as e:
                self.key_selector.report_error(key_id, "exception", str(e))
                if attempt == max_retries - 1:
                    raise
                time.sleep(1)
                
        raise Exception(f"Failed after {max_retries} attempts")
        
    def process_data(self, data: list) -> dict:
        """
        Example function that processes data using API requests.
        """
        print("Processing data with intelligent key selection...")
        
        results = []
        for i, item in enumerate(data):
            print(f"Processing item {i+1}/{len(data)}")
            
            try:
                # This will automatically select the best key
                result = self._make_api_request(f"process/{item['id']}", "POST", item)
                results.append(result)
                
            except Exception as e:
                print(f"Failed to process item {item['id']}: {e}")
                results.append({"id": item['id'], "error": str(e)})
                
        return {
            "processed": len(results),
            "results": results,
            "key_usage": self.key_selector.get_statistics()
        }

def main():
    """Main function to demonstrate the enhanced agent script."""
    
    # Initialize the enhanced agent
    agent = EnhancedAgentScript()
    
    print("Enhanced Agent Script with Intelligent Key Selection")
    print("=" * 50)
    
    # Show current key status
    print("\nKey Status:")
    status = agent.key_selector.get_key_status()
    for key_id, info in status.items():
        print(f"  {key_id}: {info['status'].value}")
    
    # Sample data to process
    sample_data = [
        {"id": 1, "content": "Sample data 1"},
        {"id": 2, "content": "Sample data 2"},
        {"id": 3, "content": "Sample data 3"}
    ]
    
    # Process data
    print("\nProcessing sample data...")
    try:
        results = agent.process_data(sample_data)
        print(f"\nProcessed {results['processed']} items")
        
        # Show final statistics
        print("\nFinal Statistics:")
        stats = results['key_usage']
        for key_id, stat in stats.items():
            success_rate = stat['success_rate'] * 100
            print(f"  {key_id}: {stat['selections']} selections, {stat['rate_limits']} rate limits, {success_rate:.1f}% success rate")
            
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()