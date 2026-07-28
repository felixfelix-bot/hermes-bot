#!/usr/bin/env python3
"""
LanceDB vector store for managing message embeddings with session-based filtering
"""

import os
import json
from typing import List, Dict, Optional, Any
import numpy as np
import lancedb
import pyarrow as pa
from pathlib import Path


class VectorStore:
    """
    LanceDB table management for message embeddings with session-based operations.
    
    Schema:
    - id: string, session_id: string, role: string, content: string
    - vector: list<float32>[768], timestamp: string
    """
    
    def __init__(self, db_path: str, table_name: str = 'messages'):
        """
        Initialize VectorStore, creating table if it doesn't exist.
        
        Args:
            db_path: Path to LanceDB database directory
            table_name: Name of the table to use/create
        """
        self.db_path = db_path
        self.table_name = table_name
        
        # Ensure database directory exists
        os.makedirs(db_path, exist_ok=True)
        
        # Connect to LanceDB
        self.db = lancedb.connect(db_path)
        
        # Create table if it doesn't exist
        if not self.table_exists():
            self._create_table()
    
    def _create_table(self):
        """Create the LanceDB table with the defined schema."""
        schema = pa.schema([
            ("id", pa.string()),
            ("session_id", pa.string()), 
            ("role", pa.string()),
            ("content", pa.string()),
            ("vector", pa.list_(pa.float32(), 768)),
            ("timestamp", pa.string())
        ])
        
        self.db.create_table(
            self.table_name,
            schema=schema,
            mode="overwrite"
        )
    
    def table_exists(self) -> bool:
        """Check if table exists in the database."""
        try:
            # Try to open the table, if it exists it will work
            self.db.open_table(self.table_name)
            return True
        except Exception:
            return False
    
    def insert(self, records: List[Dict[str, Any]]) -> None:
        """
        Insert a list of records into the table.
        
        Args:
            records: List of dictionaries matching schema
        """
        if not records:
            return
            
        # Validate records have required fields
        required_fields = {"id", "session_id", "role", "content", "vector", "timestamp"}
        for record in records:
            if not required_fields.issubset(record.keys()):
                missing = required_fields - set(record.keys())
                raise ValueError(f"Record missing required fields: {missing}")
            
            # Validate vector is correct shape
            if len(record["vector"]) != 768:
                raise ValueError(f"Vector must be length 768, got {len(record['vector'])}")
        
        # Convert to Arrow table
        table = pa.Table.from_pylist(records)
        
        # Insert into table
        lance_table = self.db.open_table(self.table_name)
        lance_table.add(table)
    
    def search(self, query_vector: List[float], limit: int = 10, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Search for similar vectors.
        
        Args:
            query_vector: Query vector (768 dimensions)
            limit: Maximum number of results to return
            session_id: Optional session ID to filter results
            
        Returns:
            List of dictionaries with original fields plus distance score
        """
        if len(query_vector) != 768:
            raise ValueError(f"Query vector must be length 768, got {len(query_vector)}")
        
        table = self.db.open_table(self.table_name)
        
        # Build search query
        query = table.search(query_vector).limit(limit)
        
        # Apply session filter if specified
        if session_id is not None:
            query = query.where(f"session_id = '{session_id}'")
        
        # Execute search
        results = query.to_arrow()
        
        # Format results with distance scores
        output_results = []
        if len(results) > 0:
            for i in range(len(results["id"])):
                result = {
                    "id": results["id"][i].as_py(),
                    "session_id": results["session_id"][i].as_py(),
                    "role": results["role"][i].as_py(),
                    "content": results["content"][i].as_py(),
                    "vector": [float(x) for x in results["vector"][i].as_py()],
                    "timestamp": results["timestamp"][i].as_py(),
                    "distance": float(results["distance"][i].as_py()) if "distance" in results.schema.names else float('inf')
                }
                output_results.append(result)
        
        return output_results
    
    def count(self) -> int:
        """Get total number of records in the table."""
        try:
            table = self.db.open_table(self.table_name)
            return table.count_rows()
        except Exception:
            return 0
    
    def delete_session(self, session_id: str) -> int:
        """
        Delete all records for a specific session.
        
        Args:
            session_id: Session ID to delete
            
        Returns:
            Number of records deleted
        """
        try:
            table = self.db.open_table(self.table_name)
            
            # Count records before deletion
            count_before = table.count_rows()
            
            # Delete session records
            table.delete(f"session_id = '{session_id}'")
            
            # Return number of deleted records
            count_after = table.count_rows()
            return count_before - count_after
        except Exception:
            # If deletion fails (table doesn't exist, session doesn't exist, etc.)
            return 0
    
    def get_table_info(self) -> Dict[str, Any]:
        """
        Get information about the table.
        
        Returns:
            Dictionary with table information
        """
        if not self.table_exists():
            return {"exists": False}
        
        table = self.db.open_table(self.table_name)
        return {
            "exists": True,
            "name": self.table_name,
            "count": table.count_rows(),
            "schema": str(table.schema)
        }