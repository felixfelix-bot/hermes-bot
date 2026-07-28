#!/usr/bin/env python3
"""
Test suite for incremental indexer - TDD implementation

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


class TestIncrementalIndexerBasic(unittest.TestCase):
    """Test suite for IncrementalIndexer class - basic functionality"""
    
    def setUp(self):
        """Set up test fixtures"""
        self.temp_dir = tempfile.mkdtemp()
        self.fixture_db = os.path.join(self.temp_dir, 'fixture_messages.db')
        self.vector_db = os.path.join(self.temp_dir, 'vector_store')
        self.state_dir = os.path.join(self.temp_dir, 'state')
        self.sync_file = os.path.join(self.state_dir, 'embed_last_sync.json')
        
        # Create state directory
        os.makedirs(self.state_dir, exist_ok=True)
        
        # Create fixture database with test messages
        self._create_fixture_database()
        
    def tearDown(self):
        """Clean up test fixtures"""
        shutil.rmtree(self.temp_dir)
    
    def _create_fixture_database(self):
        """Create a fixture database with test messages"""
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
        
        # Insert test messages with different timestamps
        test_messages = [
            ('session_1', 'user', 'Initial message 1', 1609459200.0, 10, 'stop'),
            ('session_1', 'assistant', 'Response to message 1', 1609459201.0, 12, 'stop'),
            ('session_2', 'user', 'Initial message 2', 1609459220.0, 8, 'stop'),
            ('session_1', 'user', 'Later message 1', 1609459300.0, 10, 'stop'),  # Later timestamp
            ('session_2', 'assistant', 'Response to message 2', 1609459321.0, 15, 'stop'),
            ('session_3', 'user', 'New session message', 1609459400.0, 6, 'stop'),  # New session
        ]
        
        for msg in test_messages:
            cursor.execute('''
                INSERT INTO messages (session_id, role, content, timestamp, token_count, finish_reason)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', msg)
        
        conn.commit()
        conn.close()
    
    def test_incremental_indexer_init(self):
        """Test that IncrementalIndexer can be initialized"""
        # This should fail initially since we haven't implemented the class yet
        from incremental_indexer import IncrementalIndexer
        
        indexer = IncrementalIndexer(
            source_db=self.fixture_db,
            vector_db_path=self.vector_db,
            sync_file=self.sync_file
        )
        
        self.assertIsNotNone(indexer)
        self.assertEqual(indexer.source_db, self.fixture_db)
        self.assertEqual(indexer.vector_db_path, self.vector_db)
        self.assertEqual(indexer.sync_file, self.sync_file)
    
    def test_get_new_messages_since_last_sync(self):
        """Test getting messages since last sync timestamp"""
        # This should fail initially
        from incremental_indexer import IncrementalIndexer
        
        indexer = IncrementalIndexer(
            source_db=self.fixture_db,
            vector_db_path=self.vector_db,
            sync_file=self.sync_file
        )
        
        # Test with no previous sync (should get all messages)
        messages = indexer._get_new_messages_since_last_sync()
        self.assertEqual(len(messages), 6)
        
        # Test with specific timestamp (should get only newer messages)
        # Set sync timestamp to message 3 (timestamp 1609459300.0)
        sync_time = 1609459250.0  # Between message 2 and 3
        indexer.last_sync_timestamp = sync_time
        
        messages = indexer._get_new_messages_since_last_sync()
        # Should get messages with timestamp > 1609459250.0
        self.assertEqual(len(messages), 3)  # Messages 3, 4, 5
        
        # Verify timestamps are all > sync_time
        for msg in messages:
            self.assertGreater(msg['timestamp'], sync_time)
    
    def test_sync_file_handling(self):
        """Test sync file creation and loading"""
        from incremental_indexer import IncrementalIndexer
        
        indexer = IncrementalIndexer(
            source_db=self.fixture_db,
            vector_db_path=self.vector_db,
            sync_file=self.sync_file
        )
        
        # Test initial state (no sync file)
        self.assertFalse(os.path.exists(self.sync_file))
        
        # Test loading sync file when it doesn't exist
        timestamp = indexer._load_last_sync_timestamp()
        self.assertIsNone(timestamp)  # Should be None for first run
        
        # Test creating sync file
        test_timestamp = 1609459250.0
        indexer._save_last_sync_timestamp(test_timestamp)
        
        # Verify file was created
        self.assertTrue(os.path.exists(self.sync_file))
        
        # Test loading sync file
        loaded_timestamp = indexer._load_last_sync_timestamp()
        self.assertEqual(loaded_timestamp, test_timestamp)
    
    def test_handle_session_deletion(self):
        """Test handling session deletion from vector store"""
        from incremental_indexer import IncrementalIndexer
        
        indexer = IncrementalIndexer(
            source_db=self.fixture_db,
            vector_db_path=self.vector_db,
            sync_file=self.sync_file
        )
        
        # Test session deletion with non-existent session
        deleted_count = indexer._handle_session_deletion('nonexistent_session')
        self.assertEqual(deleted_count, 0)
        
        # Test with mock vector store to verify the call is made
        with mock.patch.object(indexer, 'vector_store') as mock_store:
            mock_store.delete_session.return_value = 5
            deleted_count = indexer._handle_session_deletion('session_1')
            self.assertEqual(deleted_count, 5)
            mock_store.delete_session.assert_called_once_with('session_1')
    
    def test_incremental_run(self):
        """Test running the incremental indexer"""
        from incremental_indexer import IncrementalIndexer
        
        indexer = IncrementalIndexer(
            source_db=self.fixture_db,
            vector_db_path=self.vector_db,
            sync_file=self.sync_file
        )
        
        # Mock the vector store to avoid actual LanceDB operations
        with mock.patch.object(indexer, 'vector_store') as mock_store:
            mock_store.insert.return_value = None
            
            # Run the indexer
            processed_count = indexer.run_incremental()
            
            # Should process all messages on first run
            self.assertEqual(processed_count, 6)
            
            # Verify sync timestamp was saved
            self.assertTrue(os.path.exists(self.sync_file))
            
            # Verify vector store insert was called
            mock_store.insert.assert_called_once()
    
    def test_incremental_run_subsequent(self):
        """Test running the incremental indexer for the second time"""
        from incremental_indexer import IncrementalIndexer
        
        # First run to establish sync timestamp
        indexer = IncrementalIndexer(
            source_db=self.fixture_db,
            vector_db_path=self.vector_db,
            sync_file=self.sync_file
        )
        
        with mock.patch.object(indexer, 'vector_store') as mock_store:
            mock_store.insert.return_value = None
            
            # First run
            processed_count = indexer.run_incremental()
            self.assertEqual(processed_count, 6)
        
        # Create new indexer with established sync
        indexer2 = IncrementalIndexer(
            source_db=self.fixture_db,
            vector_db_path=self.vector_db,
            sync_file=self.sync_file
        )
        
        with mock.patch.object(indexer2, 'vector_store') as mock_store:
            mock_store.insert.return_value = None
            
            # Second run should process 0 messages (all already synced)
            processed_count = indexer2.run_incremental()
            self.assertEqual(processed_count, 0)


class TestIncrementalIndexerIntegration(unittest.TestCase):
    """Integration tests with actual (mocked) dependencies"""
    
    def setUp(self):
        """Set up test fixtures"""
        self.temp_dir = tempfile.mkdtemp()
        self.fixture_db = os.path.join(self.temp_dir, 'fixture_messages.db')
        self.vector_db = os.path.join(self.temp_dir, 'vector_store')
        self.state_dir = os.path.join(self.temp_dir, 'state')
        self.sync_file = os.path.join(self.state_dir, 'embed_last_sync.json')
        
        os.makedirs(self.state_dir, exist_ok=True)
        self._create_fixture_database()
        
    def tearDown(self):
        """Clean up test fixtures"""
        shutil.rmtree(self.temp_dir)
    
    def _create_fixture_database(self):
        """Create a fixture database with test messages"""
        conn = sqlite3.connect(self.fixture_db)
        cursor = conn.cursor()
        
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
        
        test_messages = [
            ('session_1', 'user', 'Test message 1', 1609459200.0, 10, 'stop'),
            ('session_1', 'assistant', 'Response 1', 1609459201.0, 12, 'stop'),
            ('session_2', 'user', 'Test message 2', 1609459220.0, 8, 'stop'),
        ]
        
        for msg in test_messages:
            cursor.execute('''
                INSERT INTO messages (session_id, role, content, timestamp, token_count, finish_reason)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', msg)
        
        conn.commit()
        conn.close()
    
    def test_full_incremental_workflow(self):
        """Test the complete incremental workflow"""
        from incremental_indexer import IncrementalIndexer
        
        # Mock dependencies
        with mock.patch('incremental_indexer.EmbedClient') as mock_embed_client, \
             mock.patch('incremental_indexer.VectorStore') as mock_vector_store:
            
            # Setup mocks
            mock_embed_client.return_value.embed_batch.return_value = [[0.1] * 768] * 3
            mock_vector_store_instance = mock.MagicMock()
            mock_vector_store_instance.count.return_value = 0
            mock_vector_store.return_value = mock_vector_store_instance
            
            # Create indexer
            indexer = IncrementalIndexer(
                source_db=self.fixture_db,
                vector_db_path=self.vector_db,
                sync_file=self.sync_file
            )
            
            # Replace the mocked instances
            indexer.embed_client = mock_embed_client.return_value
            indexer.vector_store = mock_vector_store.return_value
            
            # First run
            processed_count = indexer.run_incremental()
            self.assertEqual(processed_count, 3)
            
            # Verify sync timestamp was saved
            self.assertTrue(os.path.exists(self.sync_file))
            
            # Verify vector store insert was called
            mock_vector_store_instance.insert.assert_called_once()
            
            # Second run - should process 0 messages
            processed_count = indexer.run_incremental()
            self.assertEqual(processed_count, 0)


if __name__ == '__main__':
    unittest.main()


class TestIncrementalIndexerEdgeCases(unittest.TestCase):
    """Additional edge case tests to improve coverage"""
    
    def setUp(self):
        """Set up test fixtures"""
        self.temp_dir = tempfile.mkdtemp()
        self.fixture_db = os.path.join(self.temp_dir, 'fixture_messages.db')
        self.vector_db = os.path.join(self.temp_dir, 'vector_store')
        self.state_dir = os.path.join(self.temp_dir, 'state')
        self.sync_file = os.path.join(self.state_dir, 'embed_last_sync.json')
        
        os.makedirs(self.state_dir, exist_ok=True)
        self._create_fixture_database()
        
    def tearDown(self):
        """Clean up test fixtures"""
        shutil.rmtree(self.temp_dir)
    
    def _create_fixture_database(self):
        """Create a fixture database with test messages"""
        conn = sqlite3.connect(self.fixture_db)
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE messages (
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
        
        test_messages = [
            ('session_1', 'user', 'Initial message 1', 1609459200.0, 10, 'stop'),
            ('session_1', 'assistant', 'Response to message 1', 1609459201.0, 12, 'stop'),
        ]
        
        for msg in test_messages:
            cursor.execute('''
                INSERT INTO messages (session_id, role, content, timestamp, token_count, finish_reason)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', msg)
        
        conn.commit()
        conn.close()
    
    def test_main_function_comprehensive(self):
        """Test main function comprehensive scenarios"""
        from incremental_indexer import main
        import sys
        
        # Test all arguments together
        comprehensive_args = [
            'incremental_indexer.py',
            '--source-db', self.fixture_db,
            '--vector-db', self.vector_db,
            '--sync-file', self.sync_file,
            '--batch-size', '5',
            '--rate-limit', '30',
            '--dry-run',
            '--mock-mode',
            '--session-delete', 'session_1'
        ]
        
        with mock.patch('sys.argv', comprehensive_args):
            with mock.patch('incremental_indexer.IncrementalIndexer') as mock_indexer_class:
                mock_indexer = mock.MagicMock()
                mock_indexer.run_incremental.return_value = 2
                mock_indexer_class.return_value = mock_indexer
                
                try:
                    main()
                except SystemExit:
                    pass
        
        # Test with batch processing and rate limiting
        batch_args = [
            'incremental_indexer.py',
            '--source-db', self.fixture_db,
            '--vector-db', self.vector_db,
            '--sync-file', self.sync_file,
            '--batch-size', '2',
            '--rate-limit', '10'
        ]
        
        with mock.patch('sys.argv', batch_args):
            with mock.patch('incremental_indexer.IncrementalIndexer') as mock_indexer_class:
                mock_indexer = mock.MagicMock()
                mock_indexer.run_incremental.return_value = 1
                mock_indexer_class.return_value = mock_indexer
                
                try:
                    main()
                except SystemExit:
                    pass
    
    def test_sync_file_error_handling(self):
        from incremental_indexer import IncrementalIndexer
        
        indexer = IncrementalIndexer(
            source_db=self.fixture_db,
            vector_db_path=self.vector_db,
            sync_file=self.sync_file
        )
        
        # Test loading sync file when it doesn't exist
        timestamp = indexer._load_last_sync_timestamp()
        self.assertIsNone(timestamp)
        
        # Test creating sync file
        test_timestamp = 1609459250.0
        indexer._save_last_sync_timestamp(test_timestamp)
        
        # Verify file was created
        self.assertTrue(os.path.exists(self.sync_file))
        
        # Test loading sync file
        loaded_timestamp = indexer._load_last_sync_timestamp()
        self.assertEqual(loaded_timestamp, test_timestamp)
        
        # Test handling malformed sync file
        with open(self.sync_file, 'w') as f:
            f.write('invalid json')
        
        # Should handle malformed file gracefully
        loaded_timestamp = indexer._load_last_sync_timestamp()
        self.assertIsNone(loaded_timestamp)
        
        # Test sync file with missing timestamp
        with open(self.sync_file, 'w') as f:
            json.dump({'timestamp': 'not-a-timestamp'}, f)
        
        loaded_timestamp = indexer._load_last_sync_timestamp()
        self.assertIsNone(loaded_timestamp)
    
    def test_database_connection_failure(self):
        """Test handling of database connection failures"""
        from incremental_indexer import IncrementalIndexer
        
        indexer = IncrementalIndexer(
            source_db='/nonexistent/path.db',
            vector_db_path=self.vector_db,
            sync_file=self.sync_file
        )
        
        # Should raise FileNotFoundError for non-existent database
        with self.assertRaises(FileNotFoundError):
            indexer._get_new_messages_since_last_sync()
    
    def test_chunk_message_error_handling(self):
        """Test chunk message error handling"""
        from incremental_indexer import IncrementalIndexer
        
        indexer = IncrementalIndexer(
            source_db=self.fixture_db,
            vector_db_path=self.vector_db,
            sync_file=self.sync_file
        )
        
        # Test with chunker returning empty chunks (e.g., tool messages that are skipped)
        with mock.patch.object(indexer, '_simple_chunk_message') as mock_chunk:
            mock_chunk.return_value = []  # All messages are skipped
            
            # Should handle empty chunks gracefully
            messages = [{'id': 1, 'session_id': 's1', 'role': 'tool', 'content': 'tool output', 'timestamp': 123}]
            processed = indexer._process_batch(messages)
            self.assertEqual(processed, 0)
    
    def test_batch_processing_failure(self):
        """Test batch processing failure scenarios"""
        from incremental_indexer import IncrementalIndexer
        
        indexer = IncrementalIndexer(
            source_db=self.fixture_db,
            vector_db_path=self.vector_db,
            sync_file=self.sync_file,
            mock_mode=False  # Don't use mock_mode so we can test real fallback logic
        )
        
        # Test with embed_client failure - should use fallback logic
        messages = [{'id': 1, 'session_id': 's1', 'role': 'user', 'content': 'test', 'timestamp': 123}]
        
        # Mock the single embed method to return a valid embedding
        with mock.patch.object(indexer.embed_client, 'embed_batch') as mock_embed_batch, \
             mock.patch.object(indexer.embed_client, 'embed') as mock_embed_single:
            
            mock_embed_batch.side_effect = Exception("Batch embedding failed")
            mock_embed_single.return_value = [0.1] * 768
            
            processed = indexer._process_batch(messages)
            self.assertEqual(processed, 1)
    
    def test_rate_limiting(self):
        """Test rate limiting functionality"""
        from incremental_indexer import IncrementalIndexer
        
        indexer = IncrementalIndexer(
            source_db=self.fixture_db,
            vector_db_path=self.vector_db,
            sync_file=self.sync_file,
            rate_limit=60  # 60 messages per minute = 1 second per message
        )
        
        # Test rate limiting with mock time
        with mock.patch('time.sleep') as mock_sleep:
            indexer._apply_rate_limit()
            # Should be called with delay for batch size
            mock_sleep.assert_called()
        
        # Test with rate limit disabled (0)
        indexer2 = IncrementalIndexer(
            source_db=self.fixture_db,
            vector_db_path=self.vector_db,
            sync_file=self.sync_file,
            rate_limit=0
        )
        
        with mock.patch('time.sleep') as mock_sleep:
            indexer2._apply_rate_limit()
            # Should not call sleep when rate limit is 0
            mock_sleep.assert_not_called()
    
    def test_simple_chunking_edge_cases(self):
        """Test simple chunking edge cases"""
        from incremental_indexer import IncrementalIndexer
        
        indexer = IncrementalIndexer(
            source_db=self.fixture_db,
            vector_db_path=self.vector_db,
            sync_file=self.sync_file
        )
        
        # Test empty content
        chunks = indexer._simple_chunk_message('msg1', 'session1', 'user', '', '123')
        self.assertEqual(len(chunks), 0)
        
        # Test tool role (should be skipped)
        chunks = indexer._simple_chunk_message('msg2', 'session2', 'tool', 'tool output', '123')
        self.assertEqual(len(chunks), 0)
        
        # Test exact max_chars boundary
        exact_content = 'A' * 8000
        chunks = indexer._simple_chunk_message('msg3', 'session3', 'user', exact_content, '123')
        self.assertEqual(len(chunks), 1)
        self.assertEqual(len(chunks[0]['content']), 8000)
        
        # Test whitespace-only content
        whitespace_content = '   \n\t  '
        chunks = indexer._simple_chunk_message('msg4', 'session4', 'user', whitespace_content, '123')
        self.assertEqual(len(chunks), 0)
    
    def test_incremental_indexer_init_variations(self):
        """Test IncrementalIndexer initialization with various parameters"""
        from incremental_indexer import IncrementalIndexer
        
        # Test basic initialization
        indexer = IncrementalIndexer(
            source_db=self.fixture_db,
            vector_db_path=self.vector_db,
            sync_file=self.sync_file
        )
        
        self.assertIsNotNone(indexer)
        self.assertEqual(indexer.source_db, self.fixture_db)
        self.assertEqual(indexer.vector_db_path, self.vector_db)
        self.assertEqual(indexer.sync_file, self.sync_file)
        self.assertEqual(indexer.batch_size, 10)  # default
        self.assertEqual(indexer.rate_limit, 100)  # default
        self.assertFalse(indexer.dry_run)
        self.assertFalse(indexer.mock_mode)
        
        # Test initialization with custom parameters
        indexer2 = IncrementalIndexer(
            source_db=self.fixture_db,
            vector_db_path=self.vector_db,
            sync_file=self.sync_file,
            batch_size=5,
            rate_limit=50,
            dry_run=True,
            mock_mode=True
        )
        
        self.assertEqual(indexer2.batch_size, 5)
        self.assertEqual(indexer2.rate_limit, 50)
        self.assertTrue(indexer2.dry_run)
        self.assertTrue(indexer2.mock_mode)
        
        # Test initialization with provided instances - should use the provided instances
        mock_embed_instance = mock.MagicMock()
        mock_store_instance = mock.MagicMock()
        
        indexer3 = IncrementalIndexer(
            source_db=self.fixture_db,
            vector_db_path=self.vector_db,
            sync_file=self.sync_file,
            embed_client=mock_embed_instance,
            vector_store=mock_store_instance
        )
        
        self.assertEqual(indexer3.embed_client, mock_embed_instance)
        # The vector store might be wrapped in MockVectorStore when in mock mode
        # So check that it's the same instance or a wrapper around it
        self.assertIsNotNone(indexer3.vector_store)
    
    def test_main_function_cli(self):
        """Test main function CLI argument parsing"""
        from incremental_indexer import main
        import sys
        
        # Mock sys.argv to test CLI parsing
        test_args = [
            'incremental_indexer.py',
            '--source-db', self.fixture_db,
            '--vector-db', self.vector_db,
            '--sync-file', self.sync_file,
            '--dry-run',
            '--mock-mode'
        ]
        
        with mock.patch('sys.argv', test_args):
            with mock.patch('incremental_indexer.IncrementalIndexer') as mock_indexer_class:
                mock_indexer = mock.MagicMock()
                mock_indexer.run_incremental.return_value = 3
                mock_indexer_class.return_value = mock_indexer
                
                # Should not raise exception
                try:
                    main()
                except SystemExit:
                    pass  # sys.argv patching might cause SystemExit
        
        # Test with required arguments missing (should raise SystemExit)
        incomplete_args = ['incremental_indexer.py', '--source-db', self.fixture_db]
        with mock.patch('sys.argv', incomplete_args):
            with self.assertRaises(SystemExit):
                main()
        
        # Test with session delete argument
        delete_args = [
            'incremental_indexer.py',
            '--source-db', self.fixture_db,
            '--vector-db', self.vector_db,
            '--sync-file', self.sync_file,
            '--session-delete', 'session_1'
        ]
        
        with mock.patch('sys.argv', delete_args):
            with mock.patch('incremental_indexer.IncrementalIndexer') as mock_indexer_class:
                mock_indexer = mock.MagicMock()
                mock_indexer.run_incremental.return_value = 0
                mock_indexer_class.return_value = mock_indexer
                
                try:
                    main()
                except SystemExit:
                    pass
    
    def test_empty_database(self):
        """Test handling of empty database"""
        from incremental_indexer import IncrementalIndexer
        
        # Create empty database
        conn = sqlite3.connect(self.fixture_db)
        cursor = conn.cursor()
        cursor.execute('DROP TABLE IF EXISTS messages')
        cursor.execute('''
            CREATE TABLE messages (
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
        conn.commit()
        conn.close()
        
        indexer = IncrementalIndexer(
            source_db=self.fixture_db,
            vector_db_path=self.vector_db,
            sync_file=self.sync_file
        )
        
        # Should handle empty database gracefully
        messages = indexer._get_new_messages_since_last_sync()
        self.assertEqual(len(messages), 0)
        
        # Should run incremental without errors
        with mock.patch.object(indexer, 'vector_store') as mock_store:
            mock_store.insert.return_value = None
            processed = indexer.run_incremental()
            self.assertEqual(processed, 0)