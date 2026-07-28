#!/usr/bin/env python3
"""
Hybrid search with Reciprocal Rank Fusion (RRF) - combining vector and keyword search
"""

import sqlite3
import logging
from typing import List, Dict, Any, Optional
from pathlib import Path

# Import the existing vector store
from vector_store import VectorStore


class ReciprocalRankFusion:
    """
    Reciprocal Rank Fusion implementation for combining ranked lists from multiple sources.
    
    The RRF formula: score(d) = sum(1/(k + rank_i(d))) for each source i
    where k is a constant (typically 60) and rank_i(d) is the rank of document d in source i.
    """
    
    def __init__(self, k: int = 60):
        """
        Initialize ReciprocalRankFusion.
        
        Args:
            k: The constant used in the RRF formula (default: 60)
        """
        self.k = k
        self.rankings = {}  # source_name -> list of ranked document IDs
    
    def add_ranking(self, source_name: str, ranked_ids: List[str]) -> None:
        """
        Add a ranked list from a search source.
        
        Args:
            source_name: Name of the search source
            ranked_ids: List of document IDs in ranked order (first is highest rank)
        """
        if not ranked_ids:
            logging.warning(f"Empty ranking provided for source: {source_name}")
        
        # Convert to 1-based indexing (RRF formula uses 1-based ranks)
        self.rankings[source_name] = ranked_ids
    
    def fuse(self, k: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Fuse rankings from all sources using RRF formula.
        
        Args:
            k: Override the default k value for fusion
            
        Returns:
            List of dictionaries with 'id' and 'score' keys, sorted by score descending
        """
        # Use provided k or default to instance k
        fusion_k = k if k is not None else self.k
        
        if not self.rankings:
            return []
        
        # Calculate RRF scores for each unique document
        document_scores = {}
        
        for source_name, ranked_ids in self.rankings.items():
            for rank, doc_id in enumerate(ranked_ids, start=1):
                # RRF formula: 1/(k + rank)
                score = 1.0 / (fusion_k + rank)
                
                if doc_id not in document_scores:
                    document_scores[doc_id] = 0.0
                
                document_scores[doc_id] += score
        
        # Handle single ranking case (return it as-is with appropriate scores)
        if len(self.rankings) == 1:
            ranked_ids = next(iter(self.rankings.values()))
            return [{"id": doc_id, "score": score} for doc_id, score in zip(ranked_ids, [1.0/(fusion_k + i) for i in range(1, len(ranked_ids) + 1)])]
        
        # Handle empty rankings case
        if not document_scores:
            return []
        
        # Sort by score descending and format results
        sorted_results = sorted(
            document_scores.items(),
            key=lambda x: x[1],
            reverse=True
        )
        
        return [{"id": doc_id, "score": score} for doc_id, score in sorted_results]


class HybridSearch:
    """
    Hybrid search that combines vector search and FTS5 keyword search using Reciprocal Rank Fusion.
    """
    
    def __init__(self, vector_store: VectorStore, db_path: str, table_name: str = "messages_fts"):
        """
        Initialize HybridSearch.
        
        Args:
            vector_store: VectorStore instance for vector similarity search
            db_path: Path to SQLite database with messages_fts table
            table_name: Name of the FTS5 table (default: "messages_fts")
        """
        self.vector_store = vector_store
        self.db_path = db_path
        self.table_name = table_name
    
    def _keyword_search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Perform keyword search using SQLite FTS5.
        
        Args:
            query: Search query string
            limit: Maximum number of results to return
            
        Returns:
            List of dictionaries with 'id' and 'rank' keys, sorted by rank
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Use FTS5 to search and rank by relevance
            query_sql = f"""
            SELECT id, rowid 
            FROM {self.table_name} 
            WHERE {self.table_name} MATCH ? 
            ORDER BY bm25({self.table_name}) DESC 
            LIMIT ?
            """
            
            cursor.execute(query_sql, (query, limit))
            results = cursor.fetchall()
            
            # Format results with rank (1-based)
            ranked_results = []
            for rank, (doc_id, rowid) in enumerate(results, start=1):
                ranked_results.append({
                    "id": doc_id,
                    "rank": rank
                })
            
            conn.close()
            return ranked_results
            
        except sqlite3.Error as e:
            logging.error(f"SQLite search error: {e}")
            return []
    
    def search(self, query_vector: List[float], query: str, limit: int = 10, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Perform hybrid search using vector similarity and keyword matching with RRF fusion.
        
        Args:
            query_vector: Query embedding vector for similarity search
            query: Text query for keyword search
            limit: Maximum number of results to return
            session_id: Optional session ID to filter results
            
        Returns:
            List of dictionaries with 'id', 'score', and optionally 'distance' keys, 
            sorted by RRF score descending
        """
        # Initialize RRF fusion
        rrf = ReciprocalRankFusion()
        
        # 1. Perform vector search
        try:
            vector_results = self.vector_store.search(query_vector, limit=limit, session_id=session_id)
            
            if vector_results:
                # Convert distance scores to ranks (lower distance = higher rank)
                vector_ranked_ids = []
                for result in vector_results:
                    vector_ranked_ids.append(result["id"])
                
                rrf.add_ranking("vector_search", vector_ranked_ids)
                
        except Exception as e:
            logging.error(f"Vector search error: {e}")
            vector_results = []
        
        # 2. Perform keyword search
        try:
            keyword_results = self._keyword_search(query, limit=limit)
            
            if keyword_results:
                keyword_ranked_ids = [result["id"] for result in keyword_results]
                rrf.add_ranking("keyword_search", keyword_ranked_ids)
                
        except Exception as e:
            logging.error(f"Keyword search error: {e}")
            keyword_results = []
        
        # 3. Fuse results using RRF
        fused_results = rrf.fuse()
        
        # Enhance results with additional metadata
        enhanced_results = []
        
        for result in fused_results[:limit]:  # Apply limit to final results
            doc_id = result["id"]
            enhanced_result = {
                "id": doc_id,
                "score": result["score"]
            }
            
            # Add distance if available from vector search
            if vector_results:
                for vec_result in vector_results:
                    if vec_result["id"] == doc_id:
                        enhanced_result["distance"] = vec_result.get("distance", float('inf'))
                        enhanced_result["vector_content"] = vec_result.get("content")
                        break
            
            # Add rank information if available from keyword search
            if keyword_results:
                for keyword_result in keyword_results:
                    if keyword_result["id"] == doc_id:
                        enhanced_result["keyword_rank"] = keyword_result["rank"]
                        break
            
            enhanced_results.append(enhanced_result)
        
        return enhanced_results