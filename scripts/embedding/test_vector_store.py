#!/usr/bin/env python3
"""
Test suite for VectorStore class - TDD implementation
Tests should fail initially, then pass after implementation
"""

import pytest
import tempfile
import os
from pathlib import Path
import numpy as np

# Import the VectorStore class (will fail initially)
from vector_store import VectorStore


class TestVectorStore:
    """Test VectorStore class functionality"""
    
    def setup_method(self):
        """Setup temporary database for each test"""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_lancedb")
        self.table_name = "test_messages"
        
    def teardown_method(self):
        """Cleanup temporary database"""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_create_table(self):
        """Test creating a table if not exists"""
        # This should create the table successfully
        vs = VectorStore(self.db_path, self.table_name)
        assert vs.table_exists() == True
        
    def test_insert_single(self):
        """Test inserting a single record"""
        vs = VectorStore(self.db_path, self.table_name)
        
        record = {
            "id": "test_id_1",
            "session_id": "session_123", 
            "role": "user",
            "content": "Hello world",
            "vector": np.random.random(768).astype(np.float32).tolist(),
            "timestamp": "2024-01-01T00:00:00"
        }
        
        # Should insert successfully
        vs.insert([record])
        assert vs.count() == 1
        
    def test_search_returns_similar(self):
        """Test search returns similar items with distance scores"""
        vs = VectorStore(self.db_path, self.table_name)
        
        # Insert test records
        records = [
            {
                "id": "test_id_1",
                "session_id": "session_123",
                "role": "user", 
                "content": "Hello world",
                "vector": np.random.random(768).astype(np.float32).tolist(),
                "timestamp": "2024-01-01T00:00:00"
            },
            {
                "id": "test_id_2",
                "session_id": "session_123",
                "role": "assistant",
                "content": "Hello there!",
                "vector": np.random.random(768).astype(np.float32).tolist(), 
                "timestamp": "2024-01-01T00:01:00"
            }
        ]
        
        vs.insert(records)
        
        # Search with query vector - should return results with distance scores
        query_vector = np.random.random(768).astype(np.float32).tolist()
        results = vs.search(query_vector, limit=5)
        
        assert len(results) > 0
        assert "distance" in results[0]  # Should include distance score
        assert "id" in results[0]  # Should include original fields
        
    def test_search_with_session_filter(self):
        """Test search with session_id filter"""
        vs = VectorStore(self.db_path, self.table_name)
        
        # Insert records for different sessions
        session1_records = [
            {
                "id": "test_id_1",
                "session_id": "session_1",
                "role": "user",
                "content": "Hello session 1",
                "vector": np.random.random(768).astype(np.float32).tolist(),
                "timestamp": "2024-01-01T00:00:00"
            }
        ]
        
        session2_records = [
            {
                "id": "test_id_2", 
                "session_id": "session_2",
                "role": "user",
                "content": "Hello session 2",
                "vector": np.random.random(768).astype(np.float32).tolist(),
                "timestamp": "2024-01-01T00:01:00"
            }
        ]
        
        vs.insert(session1_records + session2_records)
        assert vs.count() == 2
        
        # Search filtered to session_1 should return only session_1 results
        query_vector = np.random.random(768).astype(np.float32).tolist()
        session1_results = vs.search(query_vector, session_id="session_1")
        
        assert len(session1_results) == 1
        assert session1_results[0]["session_id"] == "session_1"
        
    def test_delete_session(self):
        """Test deleting all records for a session"""
        vs = VectorStore(self.db_path, self.table_name)
        
        # Insert records for two sessions
        session1_records = [
            {
                "id": "test_id_1",
                "session_id": "session_1", 
                "role": "user",
                "content": "Hello session 1",
                "vector": np.random.random(768).astype(np.float32).tolist(),
                "timestamp": "2024-01-01T00:00:00"
            }
        ]
        
        session2_records = [
            {
                "id": "test_id_2",
                "session_id": "session_2",
                "role": "user",
                "content": "Hello session 2", 
                "vector": np.random.random(768).astype(np.float32).tolist(),
                "timestamp": "2024-01-01T00:01:00"
            }
        ]
        
        vs.insert(session1_records + session2_records)
        assert vs.count() == 2
        
        # Delete session_1
        vs.delete_session("session_1")
        assert vs.count() == 1
        
        # Verify session_2 still exists
        query_vector = np.random.random(768).astype(np.float32).tolist()
        results = vs.search(query_vector)
        assert results[0]["session_id"] == "session_2"
        
    def test_count_empty(self):
        """Test count returns 0 for empty table"""
        vs = VectorStore(self.db_path, self.table_name)
        assert vs.count() == 0
        assert vs.table_exists() == True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])