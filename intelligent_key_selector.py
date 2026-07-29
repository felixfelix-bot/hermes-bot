#!/usr/bin/env python3
"""
Intelligent Key Selector for API Key Management

Replaces binary exponential backoff with intelligent key rotation,
tracking rate limits per key and selecting optimal keys based on current status.
"""

import time
import json
import threading
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, Optional, Tuple
from dataclasses import dataclass, asdict
import sqlite3
import os
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class KeyType(Enum):
    OURS = "ours"
    FRIEND = "friend"
    PPQ = "ppq"
    OLLAMA = "ollama"

class KeyStatus(Enum):
    AVAILABLE = "available"
    LIMITED = "limited"
    COOLDOWN = "cooldown"

@dataclass
class KeyInfo:
    key_type: KeyType
    key_id: str
    api_key: str
    status: KeyStatus = KeyStatus.AVAILABLE
    last_used: Optional[datetime] = None
    last_429: Optional[datetime] = None
    request_count: int = 0
    backoff_until: Optional[datetime] = None
    backoff_duration: int = 30  # seconds

class IntelligentKeySelector:
    """
    Intelligent Key Selection system that tracks rate limits and selects optimal keys.
    """
    
    def __init__(self, db_path: str = None):
        self.db_path = db_path or os.path.expanduser("~/.hermes/bot/key_selector.db")
        self.keys: Dict[str, KeyInfo] = {}
        self.lock = threading.Lock()
        self.init_database()
        self.load_keys()
        
    def init_database(self):
        """Initialize SQLite database for persistent key state tracking."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS key_states (
                key_id TEXT PRIMARY KEY,
                key_type TEXT NOT NULL,
                status TEXT NOT NULL,
                last_used TIMESTAMP,
                last_429 TIMESTAMP,
                request_count INTEGER DEFAULT 0,
                backoff_until TIMESTAMP,
                backoff_duration INTEGER DEFAULT 30,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS rate_limit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                details TEXT
            )
        ''')
        
        conn.commit()
        conn.close()
        logger.info(f"Database initialized at {self.db_path}")
        
    def load_keys(self):
        """Load keys from database and create default ones if none exist."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM key_states")
        rows = cursor.fetchall()
        
        if not rows:
            # Create default keys
            default_keys = [
                ("main_ours", KeyType.OURS, os.environ.get("ZAI_OUR_KEY", "")),
                ("friend_key", KeyType.FRIEND, os.environ.get("ZAI_API_KEY", "")),
                ("ppq_key", KeyType.PPQ, os.environ.get("PPQ_API_KEY", "")),
                ("ollama_key", KeyType.OLLAMA, "ollama_local_key")
            ]
            
            for key_id, key_type, api_key in default_keys:
                key_info = KeyInfo(
                    key_type=key_type,
                    key_id=key_id,
                    api_key=api_key,
                    backoff_duration=self._get_default_backoff(key_type)
                )
                self.keys[key_id] = key_info
                self._save_key_state(key_info)
                logger.info(f"Created default key: {key_id}")
        else:
            # Load existing keys
            for row in rows:
                if len(row) >= 8:
                    key_id, key_type, status, last_used, last_429, request_count, backoff_until, backoff_duration = row[:8]
                else:
                    # Handle old schema or missing columns
                    key_id, key_type, status = row[:3]
                    last_used = row[3] if len(row) > 3 else None
                    last_429 = row[4] if len(row) > 4 else None
                    request_count = row[5] if len(row) > 5 else 0
                    backoff_until = row[6] if len(row) > 6 else None
                    backoff_duration = row[7] if len(row) > 7 else 30
                key_info = KeyInfo(
                    key_type=KeyType(key_type),
                    key_id=key_id,
                    api_key="loaded_from_db",  # Will be updated from secure storage
                    status=KeyStatus(status),
                    last_used=datetime.fromisoformat(last_used) if last_used else None,
                    last_429=datetime.fromisoformat(last_429) if last_429 else None,
                    request_count=request_count,
                    backoff_until=datetime.fromisoformat(backoff_until) if backoff_until else None,
                    backoff_duration=backoff_duration
                )
                self.keys[key_id] = key_info
                logger.info(f"Loaded key from database: {key_id}")
        
        conn.close()
        
    def _get_default_backoff(self, key_type: KeyType) -> int:
        """Get default backoff duration for key type."""
        backoff_map = {
            KeyType.OURS: 30,
            KeyType.FRIEND: 45,
            KeyType.PPQ: 60,
            KeyType.OLLAMA: 15
        }
        return backoff_map.get(key_type, 30)
        
    def _save_key_state(self, key_info: KeyInfo):
        """Save key state to database."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT OR REPLACE INTO key_states 
            (key_id, key_type, status, last_used, last_429, request_count, backoff_until, backoff_duration)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            key_info.key_id,
            key_info.key_type.value,
            key_info.status.value,
            key_info.last_used.isoformat() if key_info.last_used else None,
            key_info.last_429.isoformat() if key_info.last_429 else None,
            key_info.request_count,
            key_info.backoff_until.isoformat() if key_info.backoff_until else None,
            key_info.backoff_duration
        ))
        
        conn.commit()
        conn.close()
        
    def _log_rate_limit_event(self, key_id: str, event_type: str, details: str = None):
        """Log rate limit events to database."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO rate_limit_events (key_id, event_type, details)
            VALUES (?, ?, ?)
        ''', (key_id, event_type, details))
        
        conn.commit()
        conn.close()
        
    def select_key(self) -> Tuple[str, KeyInfo]:
        """
        Select the optimal key based on current status and priorities.
        Returns tuple of (key_id, key_info)
        """
        with self.lock:
            now = datetime.now()
            available_keys = []
            
            # Filter available keys
            for key_id, key_info in self.keys.items():
                # Check if backoff period has expired
                if key_info.backoff_until and key_info.backoff_until <= now:
                    key_info.status = KeyStatus.AVAILABLE
                    key_info.backoff_until = None
                    self._save_key_state(key_info)
                    
                if key_info.status == KeyStatus.AVAILABLE:
                    available_keys.append((key_id, key_info))
            
            if not available_keys:
                logger.warning("No available keys - all keys are in cooldown")
                # Return the key with earliest backoff expiry
                earliest_key = min(self.keys.items(), 
                                 key=lambda x: x[1].backoff_until or datetime.max)
                return earliest_key
                
            # Prioritize keys: free keys (ollama) first, then our key, then friend, then ppq
            priority_order = [KeyType.OLLAMA, KeyType.OURS, KeyType.FRIEND, KeyType.PPQ]
            
            for key_type in priority_order:
                for key_id, key_info in available_keys:
                    if key_info.key_type == key_type:
                        # Update usage tracking
                        key_info.last_used = now
                        key_info.request_count += 1
                        self._save_key_state(key_info)
                        self._log_rate_limit_event(key_id, "selected", f"Request #{key_info.request_count}")
                        return key_id, key_info
                        
            # Fallback - return first available
            key_id, key_info = available_keys[0]
            key_info.last_used = now
            key_info.request_count += 1
            self._save_key_state(key_info)
            self._log_rate_limit_event(key_id, "selected", f"Request #{key_info.request_count}")
            return key_id, key_info
            
    def report_429(self, key_id: str, response_headers: Dict = None):
        """
        Report a 429 rate limit response and apply backoff.
        """
        with self.lock:
            if key_id not in self.keys:
                logger.error(f"Unknown key_id: {key_id}")
                return
                
            key_info = self.keys[key_id]
            now = datetime.now()
            
            # Update key state
            key_info.status = KeyStatus.LIMITED
            key_info.last_429 = now
            
            # Apply graduated backoff
            backoff_duration = key_info.backoff_duration
            key_info.backoff_until = now + timedelta(seconds=backoff_duration)
            key_info.status = KeyStatus.COOLDOWN
            
            # Save state
            self._save_key_state(key_info)
            
            # Log event
            details = f"Backoff: {backoff_duration}s"
            if response_headers:
                details += f", Headers: {json.dumps(response_headers)}"
            self._log_rate_limit_event(key_id, "429_received", details)
            
            logger.info(f"Applied {backoff_duration}s backoff to key {key_id}")
            
    def report_success(self, key_id: str):
        """Report a successful request."""
        with self.lock:
            if key_id not in self.keys:
                logger.error(f"Unknown key_id: {key_id}")
                return
                
            key_info = self.keys[key_id]
            self._log_rate_limit_event(key_id, "success")
            logger.debug(f"Success reported for key {key_id}")
            
    def report_error(self, key_id: str, error_type: str, details: str = None):
        """Report a non-429 error."""
        with self.lock:
            if key_id not in self.keys:
                logger.error(f"Unknown key_id: {key_id}")
                return
                
            self._log_rate_limit_event(key_id, f"error_{error_type}", details)
            logger.debug(f"Error {error_type} reported for key {key_id}")
            
    def get_key_status(self, key_id: str = None) -> Dict:
        """Get current status of keys."""
        with self.lock:
            if key_id:
                if key_id in self.keys:
                    return asdict(self.keys[key_id])
                else:
                    return {"error": f"Key {key_id} not found"}
            else:
                # Return status of all keys
                return {key_id: asdict(key_info) for key_id, key_info in self.keys.items()}
                
    def get_statistics(self) -> Dict:
        """Get usage statistics."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        stats = {}
        
        # Get key statistics
        cursor.execute('''
            SELECT key_id, 
                   COUNT(*) as total_events,
                   SUM(CASE WHEN event_type = 'selected' THEN 1 ELSE 0 END) as selections,
                   SUM(CASE WHEN event_type = '429_received' THEN 1 ELSE 0 END) as rate_limits,
                   MAX(timestamp) as last_event
            FROM rate_limit_events 
            GROUP BY key_id
        ''')
        
        key_stats = cursor.fetchall()
        for row in key_stats:
            key_id, total_events, selections, rate_limits, last_event = row
            stats[key_id] = {
                'total_events': total_events,
                'selections': selections,
                'rate_limits': rate_limits,
                'last_event': last_event,
                'success_rate': (selections - rate_limits) / selections if selections > 0 else 0
            }
            
        conn.close()
        return stats
        
    def reset_key_state(self, key_id: str):
        """Reset a key to available state."""
        with self.lock:
            if key_id in self.keys:
                key_info = self.keys[key_id]
                key_info.status = KeyStatus.AVAILABLE
                key_info.backoff_until = None
                self._save_key_state(key_info)
                self._log_rate_limit_event(key_id, "reset")
                logger.info(f"Reset key {key_id} to available state")
                
    def update_api_key(self, key_id: str, new_api_key: str):
        """Update the API key for a key ID."""
        with self.lock:
            if key_id in self.keys:
                self.keys[key_id].api_key = new_api_key
                self._save_key_state(self.keys[key_id])
                logger.info(f"Updated API key for {key_id}")
            else:
                logger.error(f"Key {key_id} not found")


def main():
    """Main function for testing and CLI usage."""
    selector = IntelligentKeySelector()
    
    print("Intelligent Key Selector")
    print("=" * 40)
    
    # Test key selection
    key_id, key_info = selector.select_key()
    print(f"Selected key: {key_id} (type: {key_info.key_type.value})")
    
    # Test status reporting
    print("\nCurrent key status:")
    status = selector.get_key_status()
    for kid, info in status.items():
        print(f"  {kid}: {info['status'].value}")
    
    # Test statistics
    print("\nStatistics:")
    stats = selector.get_statistics()
    for kid, stat in stats.items():
        print(f"  {kid}: {stat}")


if __name__ == "__main__":
    main()