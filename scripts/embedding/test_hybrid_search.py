#!/usr/bin/env python3
"""
Test suite for hybrid search RRF module
"""

import pytest
import sys
import os

# Add the current directory to Python path to import modules
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from hybrid_search import ReciprocalRankFusion, HybridSearch


class TestReciprocalRankFusion:
    """Test cases for ReciprocalRankFusion class"""
    
    def test_empty_fusion(self):
        """Test fusing empty rankings returns empty list"""
        rrf = ReciprocalRankFusion()
        result = rrf.fuse(k=60)
        assert result == []
    
    def test_single_ranking(self):
        """Test fusing single ranking returns it as-is"""
        rrf = ReciprocalRankFusion()
        rrf.add_ranking("vector_search", ["doc1", "doc2", "doc3"])
        result = rrf.fuse(k=60)
        
        # Should return the same ranking with scores
        assert len(result) == 3
        assert result[0]["id"] == "doc1"
        assert result[1]["id"] == "doc2"
        assert result[2]["id"] == "doc3"
        assert all("score" in item for item in result)
    
    def test_fusion_with_two_sources(self):
        """Test fusing rankings from two sources"""
        rrf = ReciprocalRankFusion()
        
        # Vector search ranks doc1 first, doc2 second
        rrf.add_ranking("vector_search", ["doc1", "doc2"])
        
        # Keyword search ranks doc2 first, doc1 second
        rrf.add_ranking("keyword_search", ["doc2", "doc1"])
        
        result = rrf.fuse(k=60)
        
        # RRF score for doc1: 1/(60+1) + 1/(60+2) = 0.01639 + 0.01613 = 0.03252
        # RRF score for doc2: 1/(60+2) + 1/(60+1) = 0.01613 + 0.01639 = 0.03252
        # They should be tied, but let's check structure
        assert len(result) == 2
        assert {item["id"] for item in result} == {"doc1", "doc2"}
        assert all("score" in item for item in result)
    
    def test_fusion_with_three_sources(self):
        """Test fusing rankings from three sources"""
        rrf = ReciprocalRankFusion()
        
        # Add three different rankings
        rrf.add_ranking("source1", ["doc1", "doc2", "doc3"])
        rrf.add_ranking("source2", ["doc2", "doc1", "doc3"])
        rrf.add_ranking("source3", ["doc3", "doc1", "doc2"])
        
        result = rrf.fuse(k=60)
        
        assert len(result) == 3
        assert {item["id"] for item in result} == {"doc1", "doc2", "doc3"}
        assert all("score" in item for item in result)
    
    def test_fusion_with_duplicate_ids(self):
        """Test that duplicate IDs across sources are handled correctly"""
        rrf = ReciprocalRankFusion()
        
        # Same document ranked in multiple sources
        rrf.add_ranking("source1", ["doc1", "doc2"])
        rrf.add_ranking("source2", ["doc1", "doc3"])
        
        result = rrf.fuse(k=60)
        
        # Should have 3 unique documents
        assert len(result) == 3
        assert {item["id"] for item in result} == {"doc1", "doc2", "doc3"}
    
    def test_different_k_values(self):
        """Test different k values in RRF formula"""
        rrf = ReciprocalRankFusion()
        
        rrf.add_ranking("source1", ["doc1", "doc2"])
        rrf.add_ranking("source2", ["doc2", "doc1"])
        
        # Test with different k values
        result_k10 = rrf.fuse(k=10)
        result_k60 = rrf.fuse(k=60)
        result_k100 = rrf.fuse(k=100)
        
        # Results should be different due to different k values
        assert len(result_k10) == 2
        assert len(result_k60) == 2
        assert len(result_k100) == 2
    
    def test_add_ranking_after_fusion(self):
        """Test adding ranking after fusion works"""
        rrf = ReciprocalRankFusion()
        
        rrf.add_ranking("source1", ["doc1", "doc2"])
        rrf.fuse(k=60)  # First fusion
        
        # Add another ranking and fuse again
        rrf.add_ranking("source2", ["doc2", "doc1"])
        result = rrf.fuse(k=60)
        
        assert len(result) == 2
        assert {item["id"] for item in result} == {"doc1", "doc2"}


class TestHybridSearch:
    """Test cases for HybridSearch class"""
    
    def setup_method(self):
        """Setup test data"""
        # Create a mock vector store
        self.mock_vector_store = type('MockVectorStore', (), {})()
        self.mock_vector_store.search = lambda query, limit, session_id=None: [
            {"id": "doc1", "distance": 0.1, "content": "Vector result 1"},
            {"id": "doc2", "distance": 0.2, "content": "Vector result 2"}
        ]
        
        # Create a mock SQLite connection with messages_fts
        self.mock_db = type('MockDB', (), {})()
        self.mock_db.execute = lambda query: [
            {"id": "doc2", "rank": 1},
            {"id": "doc1", "rank": 2},
            {"id": "doc3", "rank": 3}
        ]
    
    def test_hybrid_search_initialization(self):
        """Test HybridSearch initialization"""
        hybrid = HybridSearch(
            vector_store=self.mock_vector_store,
            db_path=":memory:",
            table_name="messages_fts"
        )
        
        assert hybrid.vector_store == self.mock_vector_store
        assert hybrid.db_path == ":memory:"
        assert hybrid.table_name == "messages_fts"
    
    def test_hybrid_search_with_both_sources(self):
        """Test hybrid search using both vector and keyword sources"""
        hybrid = HybridSearch(
            vector_store=self.mock_vector_store,
            db_path=":memory:",
            table_name="messages_fts"
        )
        
        # Mock the search results
        def mock_vector_search(query_vector, limit, session_id=None):
            return [
                {"id": "doc1", "distance": 0.1, "content": "Vector result 1"},
                {"id": "doc2", "distance": 0.2, "content": "Vector result 2"}
            ]
        
        def mock_keyword_search(query, limit):
            return [
                {"id": "doc2", "rank": 1},
                {"id": "doc1", "rank": 2},
                {"id": "doc3", "rank": 3}
            ]
        
        hybrid.vector_store.search = mock_vector_search
        hybrid._keyword_search = mock_keyword_search
        
        # Mock query vector
        query_vector = [0.1] * 768
        
        results = hybrid.search(query_vector, query="test query", limit=10)
        
        # Should fuse results from both sources
        assert len(results) >= 2  # At least docs 1 and 2
        assert {item["id"] for item in results}.issuperset({"doc1", "doc2"})
        assert all("score" in item for item in results)
    
    def test_hybrid_search_with_keyword_only(self):
        """Test hybrid search with only keyword results"""
        hybrid = HybridSearch(
            vector_store=self.mock_vector_store,
            db_path=":memory:",
            table_name="messages_fts"
        )
        
        # Mock vector search returning no results
        def mock_vector_search(query_vector, limit, session_id=None):
            return []
        
        def mock_keyword_search(query, limit):
            return [
                {"id": "doc1", "rank": 1},
                {"id": "doc2", "rank": 2}
            ]
        
        hybrid.vector_store.search = mock_vector_search
        hybrid._keyword_search = mock_keyword_search
        
        query_vector = [0.1] * 768
        results = hybrid.search(query_vector, query="test query", limit=10)
        
        # Should return only keyword results
        assert len(results) == 2
        assert results[0]["id"] == "doc1"
        assert results[1]["id"] == "doc2"
    
    def test_hybrid_search_with_vector_only(self):
        """Test hybrid search with only vector results"""
        hybrid = HybridSearch(
            vector_store=self.mock_vector_store,
            db_path=":memory:",
            table_name="messages_fts"
        )
        
        # Mock keyword search returning no results
        def mock_vector_search(query_vector, limit, session_id=None):
            return [
                {"id": "doc1", "distance": 0.1},
                {"id": "doc2", "distance": 0.2}
            ]
        
        def mock_keyword_search(query, limit):
            return []
        
        hybrid.vector_store.search = mock_vector_search
        hybrid._keyword_search = mock_keyword_search
        
        query_vector = [0.1] * 768
        results = hybrid.search(query_vector, query="test query", limit=10)
        
        # Should return only vector results
        assert len(results) == 2
        assert results[0]["id"] == "doc1"
        assert results[1]["id"] == "doc2"
    
    def test_hybrid_search_empty_results(self):
        """Test hybrid search with no results from either source"""
        hybrid = HybridSearch(
            vector_store=self.mock_vector_store,
            db_path=":memory:",
            table_name="messages_fts"
        )
        
        # Mock both searches returning empty results
        def mock_vector_search(query_vector, limit, session_id=None):
            return []
        
        def mock_keyword_search(query, limit):
            return []
        
        hybrid.vector_store.search = mock_vector_search
        hybrid._keyword_search = mock_keyword_search
        
        query_vector = [0.1] * 768
        results = hybrid.search(query_vector, query="test query", limit=10)
        
        # Should return empty list
        assert results == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])