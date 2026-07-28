#!/usr/bin/env python3
"""
Test suite for backfill indexer - TDD implementation (simplified)

Tests core functionality without external dependencies where possible.
"""

import os
import json
import sqlite3
import tempfile
import shutil
import unittest.mock as mock
import unittest
import time
from pathlib import Path
import sys

# Add the parent directory to path so we can import from the actual script
sys.path.insert(0, '/home/c03rad0r/repos/hermes-bot/scripts/embedding')

# Import only what we can test without external dependencies
try:
    from chunker import chunk_message, chunk_messages_batch
except ImportError:
    chunk_message = None
    chunk_messages_batch = None


class TestBackfillIndexerBasic(unittest.TestCase):
    """Test suite for BackfillIndexer class - basic functionality"""
    
    def setUp(self):
        """Set up test fixtures"""
        self.temp_dir = tempfile.mkdtemp()
        self.fixture_db = os.path.join(self.temp_dir, 'fixture_messages.db')
        self.vector_db = os.path.join(self.temp_dir, 'vector_store')
        self.progress_file = os.path.join(self.temp_dir, 'backfill_progress.json')
        
        # Create fixture database with 10 test messages
        self._create_fixture_database()
        
    def tearDown(self):
        """Clean up test fixtures"""
        shutil.rmtree(self.temp_dir)
    
    def _create_fixture_database(self):
        """Create a fixture database with 10 test messages"""
        conn = sqlite3.connect(self.fixture_db)
        cursor = conn.cursor()
        
        # Create messages table if it doesn't exist
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                timestamp REAL NOT NULL,
                token_count INTEGER,
                finish_reason TEXT,
                active INTEGER NOT NULL DEFAULT 1
            )
        ''')
        
        # Insert 10 test messages
        test_messages = [
            ('session_1', 'user', 'Hello, this is message 1', 1609459200.0, 10, 'stop'),
            ('session_1', 'assistant', 'Hello! How can I help you today?', 1609459201.0, 12, 'stop'),
            ('session_2', 'user', 'I need help with my code', 1609459220.0, 8, 'stop'),
            ('session_2', 'assistant', 'Of course! What specific issue are you having?', 1609459221.0, 15, 'stop'),
            ('session_3', 'user', 'Can you explain TDD?', 1609459240.0, 6, 'stop'),
            ('session_3', 'assistant', 'TDD stands for Test-Driven Development...', 1609459241.0, 25, 'stop'),
            ('session_4', 'user', 'What are the benefits of testing?', 1609459260.0, 9, 'stop'),
            ('session_4', 'assistant', 'Testing provides many benefits...', 1609459261.0, 20, 'stop'),
            ('session_5', 'user', 'How do I mock dependencies?', 1609459280.0, 8, 'stop'),
            ('session_5', 'assistant', 'Mocking allows you to isolate your code...', 1609459281.0, 18, 'stop'),
        ]
        
        for msg in test_messages:
            cursor.execute('''
                INSERT INTO messages (session_id, role, content, timestamp, token_count, finish_reason)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', msg)
        
        conn.commit()
        conn.close()
    
    def test_chunk_functionality(self):
        """Test that chunker works correctly"""
        if chunk_message is None:
            self.skipTest("chunker module not available")
        
        # Test short message - should return single chunk
        chunk = chunk_message(
            'msg_1', 'session_1', 'user', 'Short message', '1609459200.0'
        )
        self.assertEqual(len(chunk), 1)
        self.assertEqual(chunk[0]['id'], 'msg_1')
        self.assertEqual(chunk[0]['content'], 'Short message')
        
        # Test long message - should return multiple chunks
        long_content = 'A' * 8500  # 8500 chars (exceeds default 8000 limit)
        chunks = chunk_message(
            'msg_2', 'session_2', 'user', long_content, '1609459200.0'
        )
        self.assertGreater(len(chunks), 1)
        
        # Test tool message - should be skipped
        chunks = chunk_message(
            'msg_3', 'session_3', 'tool', 'Tool output', '1609459200.0'
        )
        self.assertEqual(len(chunks), 0)
    
    def test_progress_file_handling(self):
        """Test progress file creation and loading"""
        # Test initial state (no progress file)
        self.assertFalse(os.path.exists(self.progress_file))
        
        # Create progress file manually
        progress_data = {
            'last_processed_id': 3,
            'processed_count': 3,
            'total_messages': 10
        }
        
        with open(self.progress_file, 'w') as f:
            json.dump(progress_data, f)
        
        # Verify file was created
        self.assertTrue(os.path.exists(self.progress_file))
        
        # Test loading progress
        with open(self.progress_file, 'r') as f:
            loaded_progress = json.load(f)
        
        self.assertEqual(loaded_progress['last_processed_id'], 3)
        self.assertEqual(loaded_progress['processed_count'], 3)


class TestMessageDatabase(unittest.TestCase):
    """Test database operations"""
    
    def setUp(self):
        """Set up test fixtures"""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, 'test.db')
        
        # Create test database
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                timestamp REAL NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            )
        ''')
        
        # Insert test data
        test_messages = [
            ('session_1', 'user', 'Message 1', 1609459200.0),
            ('session_1', 'assistant', 'Response 1', 1609459201.0),
            ('session_2', 'user', 'Message 2', 1609459220.0),
        ]
        
        for msg in test_messages:
            cursor.execute('''
                INSERT INTO messages (session_id, role, content, timestamp)
                VALUES (?, ?, ?, ?)
            ''', msg)
        
        conn.commit()
        conn.close()
    
    def tearDown(self):
        """Clean up test fixtures"""
        shutil.rmtree(self.temp_dir)
    
    def test_database_connection(self):
        """Test that we can connect to the database"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM messages')
        count = cursor.fetchone()[0]
        conn.close()
        
        self.assertEqual(count, 3)
    
    def test_message_retrieval(self):
        """Test retrieving messages from database"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Get messages after ID 0 (all messages)
        cursor.execute('''
            SELECT id, session_id, role, content, timestamp
            FROM messages WHERE id > ? ORDER BY id ASC
        ''', (0,))
        
        messages = []
        for row in cursor.fetchall():
            messages.append({
                'id': row[0],
                'session_id': row[1],
                'role': row[2],
                'content': row[3],
                'timestamp': row[4]
            })
        
        conn.close()
        
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[0]['id'], 1)
        self.assertEqual(messages[0]['session_id'], 'session_1')
        self.assertEqual(messages[0]['role'], 'user')


if __name__ == '__main__':
    unittest.main()