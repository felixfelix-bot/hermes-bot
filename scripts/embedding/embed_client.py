#!/usr/bin/env python3
"""
Embed client - thin wrapper around ollama nomic-embed-text with batching, SQLite caching, error handling.

Usage:
    from embed_client import EmbedClient
    
    client = EmbedClient()
    vector = client.embed("Hello world")
    vectors = client.embed_batch(["Hello", "World"])
"""

import json
import sqlite3
import hashlib
import requests
from typing import List, Union
import re


class EmbedClient:
    """Ollama nomic-embed-text wrapper with SQLite caching and batching support"""
    
    def __init__(self, db_path: str = "embed_cache.db", ollama_url: str = "http://localhost:11434/api/embeddings"):
        """
        Initialize embed client.
        
        Args:
            db_path: Path to SQLite cache database
            ollama_url: URL for ollama embeddings API
        """
        self.db_path = db_path
        self.ollama_url = ollama_url
        self.max_chars = 8192  # ~2048 tokens
        
        # Initialize database
        self._init_db()
    
    def _init_db(self):
        """Initialize SQLite database and cache table"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS embeddings_cache (
                    text_hash TEXT PRIMARY KEY,
                    text TEXT,
                    embedding BLOB,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.commit()
    
    def _get_text_hash(self, text: str) -> str:
        """Generate SHA256 hash for text"""
        return hashlib.sha256(text.encode('utf-8')).hexdigest()
    
    def _truncate_text(self, text: str) -> str:
        """Truncate text to max_chars while preserving words"""
        if len(text) <= self.max_chars:
            return text
        
        # Try to truncate at word boundary
        truncated = text[:self.max_chars]
        last_space = truncated.rfind(' ')
        
        if last_space > self.max_chars * 0.8:  # If we can truncate at reasonable word boundary
            truncated = truncated[:last_space]
        else:
            # Otherwise just truncate
            truncated = truncated[:self.max_chars]
        
        return truncated
    
    def _check_cache(self, text_hash: str) -> Union[None, List[float]]:
        """Check if embedding exists in cache"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT embedding FROM embeddings_cache WHERE text_hash = ?",
                (text_hash,)
            )
            row = cursor.fetchone()
            if row:
                # Deserialize embedding from bytes
                return json.loads(row[0].decode('utf-8'))
            return None
    
    def _cache_embedding(self, text_hash: str, text: str, embedding: List[float]):
        """Store embedding in cache"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO embeddings_cache (text_hash, text, embedding) VALUES (?, ?, ?)",
                (text_hash, text, json.dumps(embedding).encode('utf-8'))
            )
            conn.commit()
    
    def _call_ollama(self, text: str) -> List[float]:
        """Call ollama embeddings API"""
        payload = {
            "model": "nomic-embed-text",
            "prompt": text
        }
        
        response = requests.post(self.ollama_url, json=payload)
        response.raise_for_status()
        
        result = response.json()
        # Extract the 768-dimensional embedding
        return result['embedding']
    
    def embed(self, text: str) -> List[float]:
        """
        Embed a single text string.
        
        Args:
            text: Input text to embed
            
        Returns:
            768-dimensional vector as list of floats
            
        Raises:
            ValueError: If text is empty
        """
        if not text or not text.strip():
            raise ValueError("Text cannot be empty")
        
        # Truncate text if necessary
        truncated_text = self._truncate_text(text)
        
        # Generate hash for cache lookup
        text_hash = self._get_text_hash(truncated_text)
        
        # Check cache first
        cached_embedding = self._check_cache(text_hash)
        if cached_embedding is not None:
            return cached_embedding
        
        # Call ollama API
        embedding = self._call_ollama(truncated_text)
        
        # Cache the result
        self._cache_embedding(text_hash, truncated_text, embedding)
        
        return embedding
    
    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """
        Embed multiple text strings efficiently.
        
        Args:
            texts: List of input texts to embed
            
        Returns:
            List of 768-dimensional vectors
            
        Raises:
            ValueError: If any text is empty
        """
        if not texts:
            return []
        
        # Check for empty texts
        for text in texts:
            if not text or not text.strip():
                raise ValueError("Text cannot be empty")
        
        embeddings = []
        uncached_texts = []
        uncached_indices = []
        processed_hashes = []
        
        # Process each text and check cache
        for i, text in enumerate(texts):
            truncated_text = self._truncate_text(text)
            text_hash = self._get_text_hash(truncated_text)
            
            # Check cache
            cached_embedding = self._check_cache(text_hash)
            if cached_embedding is not None:
                embeddings.append(cached_embedding)
            else:
                embeddings.append(None)  # Placeholder
                uncached_texts.append(truncated_text)
                uncached_indices.append(i)
                processed_hashes.append(text_hash)
        
        # Call ollama for uncached texts
        if uncached_texts:
            for idx, text, text_hash in zip(uncached_indices, uncached_texts, processed_hashes):
                embedding = self._call_ollama(text)
                # Cache the result
                self._cache_embedding(text_hash, text, embedding)
                # Replace placeholder in embeddings list
                embeddings[idx] = embedding
        
        # Remove None placeholders (shouldn't be any at this point)
        return [emb for emb in embeddings if emb is not None]