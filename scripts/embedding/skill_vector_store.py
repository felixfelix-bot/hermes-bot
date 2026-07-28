#!/usr/bin/env python3
"""
LanceDB vector store for managing skill embeddings with skill-specific schema
"""

import os
import json
from typing import List, Dict, Optional, Any
import numpy as np
import lancedb
import pyarrow as pa
from pathlib import Path


class SkillVectorStore:
    """
    LanceDB table management for skill embeddings with skill-specific operations.
    
    Schema:
    - id: string, name: string, description: string, tags: list<string>
    - category: string, content: string, file_path: string, vector: list<float32>[768], timestamp: string
    """
    
    def __init__(self, db_path: str, table_name: str = 'skills'):
        """
        Initialize SkillVectorStore, creating table if it doesn't exist.
        
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
        """Create the LanceDB table with the skill-specific schema."""
        schema = pa.schema([
            ("id", pa.string()),
            ("name", pa.string()),
            ("description", pa.string()),
            ("tags", pa.list_(pa.string())),
            ("category", pa.string()),
            ("content", pa.string()),
            ("file_path", pa.string()),
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
        required_fields = {"id", "name", "description", "tags", "category", 
                          "content", "file_path", "vector", "timestamp"}
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
    
    def search(self, query_vector: List[float], limit: int = 10, category: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Search for similar skill vectors.
        
        Args:
            query_vector: Query vector (768 dimensions)
            limit: Maximum number of results to return
            category: Optional category to filter results
            
        Returns:
            List of dictionaries with original fields plus distance score
        """
        if len(query_vector) != 768:
            raise ValueError(f"Query vector must be length 768, got {len(query_vector)}")
        
        table = self.db.open_table(self.table_name)
        
        # Build search query
        query = table.search(query_vector).limit(limit)
        
        # Apply category filter if specified
        if category is not None:
            query = query.where(f"category = '{category}'")
        
        # Execute search
        results = query.to_arrow()
        
        # Format results with distance scores
        output_results = []
        if len(results) > 0:
            for i in range(len(results["id"])):
                result = {
                    "id": results["id"][i].as_py(),
                    "name": results["name"][i].as_py(),
                    "description": results["description"][i].as_py(),
                    "tags": [str(x) for x in results["tags"][i].as_py()],
                    "category": results["category"][i].as_py(),
                    "content": results["content"][i].as_py(),
                    "file_path": results["file_path"][i].as_py(),
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
    
    def delete_category(self, category: str) -> int:
        """
        Delete all records for a specific category.
        
        Args:
            category: Category to delete
            
        Returns:
            Number of records deleted
        """
        table = self.db.open_table(self.table_name)
        
        # Count records before deletion
        count_before = table.count_rows()
        
        # Delete category records
        table.delete(f"category = '{category}'")
        
        # Return number of deleted records
        count_after = table.count_rows()
        return count_before - count_after
    
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
    
    def get_categories(self) -> List[str]:
        """
        Get all available categories.
        
        Returns:
            List of category names
        """
        try:
            table = self.db.open_table(self.table_name)
            # Execute SQL query to get unique categories
            result = table.to_lance().to_table(columns=["category"])
            categories = list(set(result["category"].to_pylist()))
            categories.sort()
            return categories
        except Exception:
            return []