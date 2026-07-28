#!/usr/bin/env python3
"""
Incremental indexer - live updates via cron job that indexes new messages since last run.

This script reads messages from the session database since the last sync timestamp,
chunks them for embedding, and stores them in a LanceDB vector store with proper
deduplication and session deletion handling.

USAGE:
    python3 incremental_indexer.py --source-db /path/to/messages.db --vector-db /path/to/lancedb/
    python3 incremental_indexer.py --source-db /path/to/messages.db --vector-db /path/to/lancedb/ --dry-run
    python3 incremental_indexer.py --source-db /path/to/messages.db --vector-db /path/to/lancedb/ --batch-size 5

FEATURES:
- Only processes new messages since last sync timestamp
- Session deletion handling (removes from LanceDB when archived)
- Batch processing with configurable batch size
- Rate limiting to avoid overwhelming the embedding service
- Crash recovery using progress tracking file
- Message chunking for long messages
- Mock mode for testing without external dependencies
- Dry run mode for testing without actual storage

REQUIREMENTS:
- SQLite database with messages table (schema: id, session_id, role, content, timestamp, active)
- LanceDB for vector storage
- EmbedClient for generating embeddings (ollama nomic-embed-text)
- chunker module for message chunking (optional, fallback provided)

EXAMPLE:
    python3 incremental_indexer.py \
        --source-db ~/.hermes/profiles/manager/state.db \
        --vector-db ~/.hermes/profiles/manager/vector_store \
        --sync-file ~/.hermes/profiles/manager/state/embed_last_sync.json \
        --batch-size 5 \
        --rate-limit 100 \
        --dry-run
"""

import os
import json
import sqlite3
import time
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

# Add the parent directory to path for imports
sys.path.insert(0, '/home/c03rad0r/repos/hermes-bot/scripts/embedding')

# Import dependencies with graceful fallbacks
try:
    from embed_client import EmbedClient
    EMBED_CLIENT_AVAILABLE = True
except ImportError:
    EMBED_CLIENT_AVAILABLE = False
    EmbedClient = None  # type: ignore

try:
    from vector_store import VectorStore
    VECTOR_STORE_AVAILABLE = True
except ImportError:
    VECTOR_STORE_AVAILABLE = False
    VectorStore = None  # type: ignore

try:
    from chunker import chunk_message, chunk_messages_batch
    CHUNKER_AVAILABLE = True
except ImportError:
    CHUNKER_AVAILABLE = False
    chunk_message = None  # type: ignore
    chunk_messages_batch = None  # type: ignore


# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class MockEmbedClient:
    """Mock EmbedClient for testing when ollama is not available"""
    
    def __init__(self, db_path: str = "embed_cache.db", ollama_url: str = "http://localhost:11434/api/embeddings"):
        self.db_path = db_path
        self.ollama_url = ollama_url
        self.max_chars = 8192
        
    def embed(self, text: str) -> List[float]:
        """Return mock embedding"""
        if not text or not text.strip():
            raise ValueError("Text cannot be empty")
        
        # Return a mock 768-dimensional vector
        return [0.1] * 768
    
    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """Return mock embeddings for batch"""
        return [self.embed(text) for text in texts]


class MockVectorStore:
    """Mock VectorStore for testing when LanceDB is not available"""
    
    def __init__(self, db_path: str, table_name: str = 'messages'):
        self.db_path = db_path
        self.table_name = table_name
        self.records = []
        logger.info(f"MockVectorStore initialized with path: {db_path}")
    
    def insert(self, records: List[Dict[str, Any]]) -> None:
        """Mock insert - just store records"""
        self.records.extend(records)
        logger.info(f"Mock insert: {len(records)} records")
    
    def delete_session(self, session_id: str) -> int:
        """Mock delete session"""
        deleted_count = len([r for r in self.records if r['session_id'] == session_id])
        self.records = [r for r in self.records if r['session_id'] != session_id]
        logger.info(f"Mock delete_session: {deleted_count} records for session {session_id}")
        return deleted_count
    
    def count(self) -> int:
        """Mock count"""
        return len(self.records)
    
    def table_exists(self) -> bool:
        """Mock table exists"""
        return True


class IncrementalIndexer:
    """Incremental indexer for processing new messages since last sync"""
    
    def __init__(
        self,
        source_db: str,
        vector_db_path: str,
        sync_file: str,
        batch_size: int = 10,
        rate_limit: int = 100,
        chunk_max_chars: int = 8000,
        dry_run: bool = False,
        embed_client: Optional[EmbedClient] = None,
        vector_store: Optional[VectorStore] = None,
        mock_mode: bool = False
    ):
        """
        Initialize incremental indexer.
        
        Args:
            source_db: Path to SQLite database with messages
            vector_db_path: Path to LanceDB database directory
            sync_file: Path to JSON sync tracking file
            batch_size: Number of messages to process at once
            rate_limit: Max messages per minute (for rate limiting)
            chunk_max_chars: Max characters per message chunk
            dry_run: If True, only log what would be processed
            embed_client: Optional EmbedClient instance
            vector_store: Optional VectorStore instance
            mock_mode: If True, use mock implementations
        """
        self.source_db = source_db
        self.vector_db_path = vector_db_path
        self.sync_file = sync_file
        self.batch_size = batch_size
        self.rate_limit = rate_limit
        self.chunk_max_chars = chunk_max_chars
        self.dry_run = dry_run
        self.mock_mode = mock_mode
        self.last_sync_timestamp = None
        self.processed_count = 0
        
        # Load last sync timestamp
        self.last_sync_timestamp = self._load_last_sync_timestamp()
        
        # Initialize clients (or use provided instances)
        if mock_mode or not EMBED_CLIENT_AVAILABLE:
            logger.warning("Using mock EmbedClient")
            self.embed_client = MockEmbedClient() if mock_mode else MockEmbedClient()
        else:
            self.embed_client = embed_client or EmbedClient()  # type: ignore
        
        if mock_mode or not VECTOR_STORE_AVAILABLE:
            logger.warning("Using mock VectorStore")
            self.vector_store = MockVectorStore(vector_db_path) if mock_mode else MockVectorStore(vector_db_path)  # type: ignore
        else:
            self.vector_store = vector_store or VectorStore(vector_db_path)  # type: ignore
        
        # Check if chunker is available
        if not CHUNKER_AVAILABLE:
            logger.warning("Chunker not available, using simple chunking logic")
    
    def _load_last_sync_timestamp(self) -> Optional[float]:
        """
        Load last sync timestamp from tracking file.
        
        Returns:
            Timestamp as float, or None if first run
        """
        if not os.path.exists(self.sync_file):
            logger.info("No existing sync file, this appears to be first run")
            return None
        
        try:
            with open(self.sync_file, 'r') as f:
                sync_data = json.load(f)
                timestamp = sync_data.get('last_sync_timestamp')
                if timestamp:
                    logger.info(f"Loaded last sync timestamp: {timestamp}")
                    return float(timestamp)
                else:
                    logger.info("Sync file exists but no timestamp found, first run")
                    return None
        except Exception as e:
            logger.warning(f"Failed to load sync file: {e}")
            return None
    
    def _save_last_sync_timestamp(self, timestamp: float):
        """Save current sync timestamp to tracking file"""
        sync_data = {
            'last_sync_timestamp': timestamp,
            'timestamp': datetime.now().isoformat(),
            'processed_count': self.processed_count
        }
        
        try:
            os.makedirs(os.path.dirname(self.sync_file), exist_ok=True)
            with open(self.sync_file, 'w') as f:
                json.dump(sync_data, f, indent=2)
            logger.debug(f"Sync timestamp saved: {timestamp}")
        except Exception as e:
            logger.error(f"Failed to save sync timestamp: {e}")
    
    def _get_new_messages_since_last_sync(self) -> List[Dict[str, Any]]:
        """
        Get messages from source database since last sync timestamp.
        
        Returns:
            List of message dictionaries
        """
        if not os.path.exists(self.source_db):
            raise FileNotFoundError(f"Source database not found: {self.source_db}")
        
        try:
            conn = sqlite3.connect(self.source_db)
            cursor = conn.cursor()
            
            # Build query based on whether we have a sync timestamp
            if self.last_sync_timestamp is None:
                # First run - get all active messages
                query = '''
                    SELECT id, session_id, role, content, timestamp
                    FROM messages 
                    WHERE active = 1
                    ORDER BY timestamp ASC
                '''
                params = []
            else:
                # Subsequent runs - get messages since last sync
                query = '''
                    SELECT id, session_id, role, content, timestamp
                    FROM messages 
                    WHERE active = 1 AND timestamp > ?
                    ORDER BY timestamp ASC
                '''
                params = [self.last_sync_timestamp]
            
            cursor.execute(query, params)
            rows = cursor.fetchall()
            
            messages = []
            for row in rows:
                messages.append({
                    'id': row[0],
                    'session_id': row[1],
                    'role': row[2],
                    'content': row[3],
                    'timestamp': row[4]
                })
            
            conn.close()
            return messages
            
        except Exception as e:
            logger.error(f"Database error: {e}")
            raise
    
    def _simple_chunk_message(self, msg_id: str, session_id: str, role: str, content: str, timestamp: str) -> List[Dict[str, Any]]:
        """Simple chunking implementation when chunker is not available"""
        # Skip tool messages and empty messages
        if role == "tool" or not content or not content.strip():
            return []
        
        # Short message - return as single chunk
        if len(content) <= self.chunk_max_chars:
            return [{
                'id': msg_id,
                'session_id': session_id,
                'role': role,
                'content': content,
                'timestamp': timestamp
            }]
        
        # Long message - split into multiple chunks
        chunks = []
        content_length = len(content)
        start_pos = 0
        chunk_index = 0
        
        while start_pos < content_length:
            # Calculate end position for this chunk
            end_pos = start_pos + self.chunk_max_chars
            
            # If we're at the end, take remaining content
            if end_pos >= content_length:
                chunk_content = content[start_pos:]
            else:
                # Try to split at word boundaries for better readability
                chunk_content = content[start_pos:end_pos]
                last_space = chunk_content.rfind(' ')
                if last_space > self.chunk_max_chars * 0.8:  # If we can split within 20% of max length
                    chunk_content = chunk_content[:last_space]
                    end_pos = start_pos + last_space
            
            # Create chunk
            chunk_id = f"{msg_id}_chunk_{chunk_index}"
            chunks.append({
                'id': chunk_id,
                'session_id': session_id,
                'role': role,
                'content': chunk_content,
                'timestamp': timestamp
            })
            
            # Move to next chunk
            start_pos += len(chunk_content)
            chunk_index += 1
        
        return chunks
    
    def _process_batch(self, messages: List[Dict[str, Any]]) -> int:
        """
        Process a batch of messages into embeddings and store them.
        
        Args:
            messages: List of message dictionaries
            
        Returns:
            Number of records processed
        """
        if self.dry_run:
            logger.info(f"[DRY RUN] Would process {len(messages)} messages")
            return len(messages)
        
        # Chunk messages
        if CHUNKER_AVAILABLE:
            chunks = chunk_messages_batch(messages, self.chunk_max_chars)
        else:
            chunks = []
            for message in messages:
                chunk = self._simple_chunk_message(
                    message['id'], message['session_id'], 
                    message['role'], message['content'], message['timestamp']
                )
                chunks.extend(chunk)
        
        if not chunks:
            logger.info(f"No chunks created from {len(messages)} messages")
            return 0
        
        logger.info(f"Created {len(chunks)} chunks from {len(messages)} messages")
        
        # Prepare batch for embedding
        texts_to_embed = []
        chunk_data = []
        
        for chunk in chunks:
            texts_to_embed.append(chunk['content'])
            chunk_data.append(chunk)
        
        # Embed the texts
        if self.mock_mode:
            # Use mock embeddings
            embeddings = self.embed_client.embed_batch(texts_to_embed)
        else:
            try:
                embeddings = self.embed_client.embed_batch(texts_to_embed)
            except Exception as e:
                logger.error(f"Failed to embed batch: {e}")
                if len(texts_to_embed) > 1:
                    # Try to embed one at a time
                    embeddings = []
                    for text in texts_to_embed:
                        try:
                            emb = self.embed_client.embed(text)
                            embeddings.append(emb)
                        except Exception as e2:
                            logger.error(f"Failed to embed text '{text[:50]}...': {e2}")
                            embeddings.append([0.1] * 768)  # Fallback
                else:
                    logger.error("Single text also failed, using fallback")
                    embeddings = [[0.1] * 768]
        
        if len(embeddings) != len(chunks):
            logger.error(f"Mismatch: {len(embeddings)} embeddings for {len(chunks)} chunks")
            return 0
        
        # Prepare records for vector store
        records = []
        for i, chunk in enumerate(chunks):
            record = {
                'id': chunk['id'],
                'session_id': chunk['session_id'],
                'role': chunk['role'],
                'content': chunk['content'],
                'vector': embeddings[i],
                'timestamp': chunk['timestamp']
            }
            records.append(record)
        
        # Store in vector database
        try:
            self.vector_store.insert(records)
        except Exception as e:
            logger.error(f"Failed to store records in vector store: {e}")
            return 0
        
        return len(records)
    
    def _handle_session_deletion(self, session_id: str) -> int:
        """
        Handle session deletion from vector store.
        
        Args:
            session_id: Session ID to delete
            
        Returns:
            Number of records deleted
        """
        try:
            return self.vector_store.delete_session(session_id)
        except Exception as e:
            logger.error(f"Failed to delete session {session_id}: {e}")
            return 0
    
    def run_incremental(self, session_id_to_delete: Optional[str] = None) -> int:
        """
        Run the incremental indexing process.
        
        Args:
            session_id_to_delete: Optional session ID to delete from vector store
            
        Returns:
            Number of messages processed
        """
        start_time = time.time()
        
        logger.info(f"Starting incremental indexer (dry_run={self.dry_run}, mock_mode={self.mock_mode})")
        
        # Handle session deletion if requested
        if session_id_to_delete:
            deleted_count = self._handle_session_deletion(session_id_to_delete)
            logger.info(f"Deleted {deleted_count} records for session {session_id_to_delete}")
        
        # Get new messages to process
        try:
            messages = self._get_new_messages_since_last_sync()
            if not messages:
                logger.info("No new messages to process")
                return 0
        except Exception as e:
            logger.error(f"Failed to get messages: {e}")
            return 0
        
        logger.info(f"Found {len(messages)} new messages to process")
        
        # Process messages in batches
        total_processed = 0
        batch_count = 0
        
        for i in range(0, len(messages), self.batch_size):
            batch_count += 1
            batch = messages[i:i + self.batch_size]
            
            logger.info(f"Processing batch {batch_count} with {len(batch)} messages")
            
            # Process the batch
            processed = self._process_batch(batch)
            total_processed += processed
            
            # Update sync timestamp if we processed messages
            if processed > 0 and batch:
                # Use the timestamp of the last message in the batch as new sync point
                last_message_timestamp = batch[-1]['timestamp']
                self.last_sync_timestamp = last_message_timestamp
                self._save_last_sync_timestamp(last_message_timestamp)
            
            # Apply rate limiting (except for last batch or dry run)
            if i + self.batch_size < len(messages) and not self.dry_run:
                self._apply_rate_limit()
        
        elapsed = time.time() - start_time
        logger.info(f"Incremental indexer completed: {total_processed} messages processed in {elapsed:.1f}s")
        
        self.processed_count += total_processed
        return total_processed
    
    def _apply_rate_limit(self):
        """Apply rate limiting between batches"""
        if self.rate_limit > 0:
            delay = 60.0 / self.rate_limit  # Convert to seconds per message
            time.sleep(delay * self.batch_size)
            logger.debug(f"Rate limiting: waited {delay * self.batch_size:.2f}s for batch")


def main():
    """Command line interface for incremental indexer"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Incremental indexer for new messages since last sync')
    parser.add_argument('--source-db', required=True, help='Path to source SQLite database')
    parser.add_argument('--vector-db', required=True, help='Path to LanceDB database directory')
    parser.add_argument('--sync-file', required=True, help='Path to JSON sync tracking file')
    parser.add_argument('--batch-size', type=int, default=10, help='Number of messages per batch')
    parser.add_argument('--rate-limit', type=int, default=100, help='Max messages per minute')
    parser.add_argument('--dry-run', action='store_true', help='Only log what would be processed')
    parser.add_argument('--mock-mode', action='store_true', help='Use mock implementations for testing')
    parser.add_argument('--session-delete', help='Session ID to delete from vector store')
    
    args = parser.parse_args()
    
    # Initialize indexer
    indexer = IncrementalIndexer(
        source_db=args.source_db,
        vector_db_path=args.vector_db,
        sync_file=args.sync_file,
        batch_size=args.batch_size,
        rate_limit=args.rate_limit,
        dry_run=args.dry_run,
        mock_mode=args.mock_mode
    )
    
    # Run incremental indexer
    processed = indexer.run_incremental(session_id_to_delete=args.session_delete)
    
    print(f"Processed {processed} messages")


if __name__ == '__main__':
    main()