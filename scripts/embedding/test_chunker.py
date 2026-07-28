#!/usr/bin/env python3
"""
Test suite for chunker.py
Following TDD: write failing tests first, then implement
"""

import unittest
import sys
import os

# Add the parent directory to sys.path to import chunker
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from chunker import chunk_message, chunk_messages_batch, get_chunk_stats


class TestChunker(unittest.TestCase):
    def test_short_message_single_chunk(self):
        """Test that short message returns single chunk"""
        msg_id = "msg123"
        session_id = "sess456"
        role = "user"
        content = "This is a short message"
        timestamp = "2024-01-01T12:00:00Z"
        
        result = chunk_message(msg_id, session_id, role, content, timestamp)
        
        # Should return single chunk
        self.assertEqual(len(result), 1)
        
        # Check chunk structure
        chunk = result[0]
        self.assertEqual(chunk['id'], msg_id)
        self.assertEqual(chunk['session_id'], session_id)
        self.assertEqual(chunk['role'], role)
        self.assertEqual(chunk['content'], content)
        self.assertEqual(chunk['timestamp'], timestamp)

    def test_long_message_multiple_chunks(self):
        """Test that long message is split into multiple chunks"""
        msg_id = "msg123"
        session_id = "sess456"
        role = "assistant"
        # Create long content (> 8000 chars)
        content = "word " * 5000  # ~25000 chars
        timestamp = "2024-01-01T12:00:00Z"
        
        result = chunk_message(msg_id, session_id, role, content, timestamp, max_chars=8000)
        
        # Should return multiple chunks
        self.assertGreater(len(result), 1)
        
        # Check first chunk
        chunk1 = result[0]
        self.assertEqual(chunk1['id'], "msg123_chunk_0")
        self.assertEqual(chunk1['session_id'], session_id)
        self.assertEqual(chunk1['role'], role)
        self.assertLessEqual(len(chunk1['content']), 8000)  # Word boundary may reduce length
        self.assertEqual(chunk1['timestamp'], timestamp)
        
        # Check second chunk exists
        chunk2 = result[1]
        self.assertEqual(chunk2['id'], "msg123_chunk_1")
        self.assertEqual(chunk2['session_id'], session_id)
        self.assertEqual(chunk2['role'], role)
        self.assertLessEqual(len(chunk2['content']), 8000)

    def test_empty_message_returns_empty_list(self):
        """Test that empty message returns empty list"""
        msg_id = "msg123"
        session_id = "sess456"
        role = "user"
        content = ""
        timestamp = "2024-01-01T12:00:00Z"
        
        result = chunk_message(msg_id, session_id, role, content, timestamp)
        
        # Should return empty list
        self.assertEqual(result, [])

    def test_tool_role_message_returns_empty_list(self):
        """Test that tool role messages return empty list"""
        msg_id = "msg123"
        session_id = "sess456"
        role = "tool"
        content = "Some tool output"
        timestamp = "2024-01-01T12:00:00Z"
        
        result = chunk_message(msg_id, session_id, role, content, timestamp)
        
        # Should return empty list (tool output is noise)
        self.assertEqual(result, [])

    def test_chunk_contains_all_required_fields(self):
        """Test that each chunk contains all required fields"""
        msg_id = "msg123"
        session_id = "sess456"
        role = "user"
        content = "This is a test message with content that should be chunked properly."
        timestamp = "2024-01-01T12:00:00Z"
        
        result = chunk_message(msg_id, session_id, role, content, timestamp)
        
        # Single chunk should have all required fields
        self.assertEqual(len(result), 1)
        chunk = result[0]
        required_fields = ['id', 'session_id', 'role', 'content', 'timestamp']
        for field in required_fields:
            self.assertIn(field, chunk)

    def test_max_chars_parameter(self):
        """Test that max_chars parameter works correctly"""
        msg_id = "msg123"
        session_id = "sess456"
        role = "user"
        content = "word " * 1000  # ~5000 chars
        timestamp = "2024-01-01T12:00:00Z"
        
        # Test with smaller max_chars
        result = chunk_message(msg_id, session_id, role, content, timestamp, max_chars=100)
        
        # Should create multiple chunks with max 100 chars each
        self.assertGreater(len(result), 1)
        for chunk in result:
            self.assertLessEqual(len(chunk['content']), 100)

    def test_exactly_at_max_chars(self):
        """Test message exactly at max_chars boundary"""
        msg_id = "msg123"
        session_id = "sess456"
        role = "user"
        content = "x" * 8000  # Exactly max_chars
        timestamp = "2024-01-01T12:00:00Z"
        
        result = chunk_message(msg_id, session_id, role, content, timestamp, max_chars=8000)
        
        # Should return single chunk
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0]['content']), 8000)


    def test_chunk_messages_batch(self):
        """Test batch chunking of multiple messages"""
        messages = [
            {
                'id': 'msg1',
                'session_id': 'sess1',
                'role': 'user',
                'content': 'Short message',
                'timestamp': '2024-01-01T12:00:00Z'
            },
            {
                'id': 'msg2',
                'session_id': 'sess1',
                'role': 'assistant',
                'content': 'long content ' * 1000,  # > 8000 chars
                'timestamp': '2024-01-01T12:01:00Z'
            },
            {
                'id': 'msg3',
                'session_id': 'sess1',
                'role': 'tool',
                'content': 'tool output',
                'timestamp': '2024-01-01T12:02:00Z'
            }
        ]
        
        result = chunk_messages_batch(messages, max_chars=100)
        
        # Should have chunks from msg1 and msg2, but not msg3 (tool)
        self.assertGreater(len(result), 1)
        
        # Check that msg3 (tool) was skipped
        msg3_chunks = [chunk for chunk in result if chunk['id'] == 'msg3']
        self.assertEqual(len(msg3_chunks), 0)

    def test_get_chunk_stats(self):
        """Test chunk statistics function"""
        chunks = [
            {'id': 'msg1_chunk_0', 'content': 'short ' * 100},
            {'id': 'msg1_chunk_1', 'content': 'content ' * 100},
            {'id': 'msg2_chunk_0', 'content': 'another message'}
        ]
        
        stats = get_chunk_stats(chunks)
        
        self.assertEqual(stats['total_chunks'], 3)
        self.assertEqual(stats['original_messages'], 2)  # msg1 and msg2
        self.assertGreater(stats['total_chars'], 0)
        self.assertGreater(stats['avg_chunk_size'], 0)

    def test_get_chunk_stats_empty(self):
        """Test statistics with empty chunks list"""
        stats = get_chunk_stats([])
        
        self.assertEqual(stats['total_chunks'], 0)
        self.assertEqual(stats['total_chars'], 0)
        self.assertEqual(stats['avg_chunk_size'], 0)
        self.assertEqual(stats['original_messages'], 0)


if __name__ == '__main__':
    unittest.main()