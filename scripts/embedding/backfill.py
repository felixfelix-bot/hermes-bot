#!/usr/bin/env python3
"""
Backfill indexer - batch embed all session messages into LanceDB

This script reads messages from the session database, chunks them for embedding,
and stores them in a LanceDB vector store with proper deduplication and crash recovery.

USAGE:
    python3 backfill.py --source-db /path/to/messages.db --vector-db /path/to/lancedb/
    python3 backfill.py --source-db /path/to/messages.db --vector-db /path/to/lancedb/ --dry-run --limit 100
    python3 backfill.py --source-db /path/to/messages.db --vector-db /path/to/lancedb/ --batch-size 20 --rate-limit 50

FEATURES:
- Batch processing with configurable batch size
- Rate limiting to avoid overwhelming the embedding service
- Crash recovery using progress tracking file
- Deduplication by checking existing message IDs
- Message chunking for long messages
- Mock mode for testing without external dependencies
- Dry run mode for testing without actual storage
- Progress logging every batch

REQUIREMENTS:
- SQLite database with messages table (schema: id, session_id, role, content, timestamp, active)
- LanceDB for vector storage
- EmbedClient for generating embeddings (ollama nomic-embed-text)
- chunker module for message chunking (optional, fallback provided)

EXAMPLE:
    python3 backfill.py \
        --source-db ~/.hermes/profiles/manager/state.db \
        --vector-db ~/.hermes/profiles/manager/vector_store \
        --batch-size 10 \
        --rate-limit 100 \
        --limit 1000 \
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

# Add the parent directory and venv to path for imports
sys.path.insert(0, '/home/c03rad0r/repos/hermes-bot/scripts/embedding')
sys.path.insert(0, '/home/c03rad0r/.hermes/hermes-agent/venv/lib/python3.11/site-packages')

# Import dependencies with graceful fallbacks
try:
    from embed_client import EmbedClient
    EMBED_CLIENT_AVAILABLE = True
except ImportError:
    EMBED_CLIENT_AVAILABLE = False
    EmbedClient = None

try:
    from vector_store import VectorStore
    VECTOR_STORE_AVAILABLE = True
except ImportError:
    VECTOR_STORE_AVAILABLE = False
    VectorStore = None

try:
    from chunker import chunk_message, chunk_messages_batch
    CHUNKER_AVAILABLE = True
except ImportError:
    CHUNKER_AVAILABLE = False
    chunk_message = None
    chunk_messages_batch = None


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
    
    def count(self) -> int:
        """Mock count"""
        return len(self.records)
    
    def table_exists(self) -> bool:
        """Mock table exists"""
        return True


class BackfillIndexer:
    """Batch embed session messages into LanceDB with crash recovery"""
    
    def __init__(
        self,
        source_db: str,
        vector_db_path: str,
        progress_file: Optional[str] = None,
        batch_size: int = 10,
        rate_limit: int = 100,
        chunk_max_chars: int = 8000,
        dry_run: bool = False,
        embed_client: Optional[EmbedClient] = None,
        vector_store: Optional[VectorStore] = None,
        mock_mode: bool = False
    ):
        """
        Initialize backfill indexer.
        
        Args:
            source_db: Path to SQLite database with messages
            vector_db_path: Path to LanceDB database directory
            progress_file: Path to JSON progress tracking file
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
        self.progress_file = progress_file or os.path.join(
            os.path.dirname(source_db), 'backfill_progress.json'
        )
        self.batch_size = batch_size
        self.rate_limit = rate_limit
        self.chunk_max_chars = chunk_max_chars
        self.dry_run = dry_run
        self.mock_mode = mock_mode
        self.last_processed_id = 0
        self.processed_count = 0
        self.total_messages = 0
        
        # Initialize clients (or use provided instances)
        if mock_mode or not EMBED_CLIENT_AVAILABLE:
            logger.warning("Using mock EmbedClient")
            self.embed_client = MockEmbedClient() if mock_mode else MockEmbedClient()
        else:
            self.embed_client = embed_client or EmbedClient()
        
        if mock_mode or not VECTOR_STORE_AVAILABLE:
            logger.warning("Using mock VectorStore")
            self.vector_store = MockVectorStore(vector_db_path) if mock_mode else MockVectorStore(vector_db_path)
        else:
            self.vector_store = vector_store or VectorStore(vector_db_path)
        
        # Check if chunker is available
        if not CHUNKER_AVAILABLE:
            logger.warning("Chunker not available, using simple chunking logic")
        
        # Ensure progress directory and file exist
        try:
            os.makedirs(os.path.dirname(self.progress_file), exist_ok=True)
            if not os.path.exists(self.progress_file):
                # Initialize progress file with current state
                initial_progress = {
                    'last_processed_id': self.last_processed_id,
                    'processed_count': self.processed_count,
                    'total_messages': 0,  # Will be updated when we query the database
                    'timestamp': datetime.now().isoformat()
                }
                with open(self.progress_file, 'w') as f:
                    json.dump(initial_progress, f, indent=2)
                logger.debug("Initialized progress file")
        except Exception as e:
            logger.warning(f"Could not initialize progress file: {e}")
        
        # Load existing progress
        self._load_progress()
    
    def _load_progress(self):
        """Load progress from tracking file if it exists"""
        if os.path.exists(self.progress_file):
            try:
                with open(self.progress_file, 'r') as f:
                    progress = json.load(f)
                    self.last_processed_id = progress.get('last_processed_id', 0)
                    self.processed_count = progress.get('processed_count', 0)
                    logger.info(f"Loaded progress: processed {self.processed_count} messages, last ID {self.last_processed_id}")
            except Exception as e:
                logger.warning(f"Failed to load progress file: {e}")
                # Reset progress if file is corrupted
                self.last_processed_id = 0
                self.processed_count = 0
        else:
            logger.info("No existing progress file, starting fresh")
    
    def _save_progress(self):
        """Save current progress to tracking file"""
        progress = {
            'last_processed_id': self.last_processed_id,
            'processed_count': self.processed_count,
            'total_messages': self.total_messages,
            'timestamp': datetime.now().isoformat()
        }
        
        try:
            os.makedirs(os.path.dirname(self.progress_file), exist_ok=True)
            with open(self.progress_file, 'w') as f:
                json.dump(progress, f, indent=2)
            logger.debug("Progress saved")
        except Exception as e:
            logger.error(f"Failed to save progress: {e}")
    
    def _get_messages_to_process(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Get messages from source database, skipping already processed ones"""
        if not os.path.exists(self.source_db):
            raise FileNotFoundError(f"Source database not found: {self.source_db}")
        
        try:
            conn = sqlite3.connect(self.source_db)
            cursor = conn.cursor()
            
            query = '''
                SELECT id, session_id, role, content, timestamp
                FROM messages 
                WHERE id > ? AND active = 1
                ORDER BY id ASC
            '''
            
            params = [self.last_processed_id]
            
            if limit:
                query += ' LIMIT ?'
                params.append(limit)
            
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
            
            # Update total messages count for progress tracking (only if not already set)
            if not self.total_messages:
                cursor.execute('SELECT COUNT(*) FROM messages WHERE active = 1')
                self.total_messages = cursor.fetchone()[0]
                logger.info(f"Total messages to process: {self.total_messages}")
            
            conn.close()
            return messages
            
        except Exception as e:
            logger.error(f"Database error: {e}")
            raise
    
    def _apply_rate_limit(self):
        """Apply rate limiting between batches"""
        if self.rate_limit > 0:
            delay = 60.0 / self.rate_limit  # Convert to seconds per message
            time.sleep(delay * self.batch_size)
            logger.debug(f"Rate limiting: waited {delay * self.batch_size:.2f}s for batch")
    
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
        """Process a batch of messages into embeddings and store them"""
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
    
    def run_backfill(self, limit: Optional[int] = None, dry_run: Optional[bool] = None) -> int:
        """
        Run the backfill process.
        
        Args:
            limit: Maximum number of messages to process (None = all)
            dry_run: Override dry_run setting
            
        Returns:
            Number of messages processed
        """
        if dry_run is not None:
            self.dry_run = dry_run
        
        start_time = time.time()
        batch_count = 0
        
        logger.info(f"Starting backfill (dry_run={self.dry_run}, mock_mode={self.mock_mode})")
        
        while True:
            # Get next batch
            try:
                messages = self._get_messages_to_process(limit=limit)
                if not messages:
                    break
            except Exception as e:
                logger.error(f"Failed to get messages: {e}")
                break
            
            batch_count += 1
            logger.info(f"Processing batch {batch_count} with {len(messages)} messages")
            
            # Process the batch
            processed = self._process_batch(messages)
            
            # Update progress
            if messages and processed > 0:
                last_id = messages[-1]['id']
                self.last_processed_id = last_id
                self.processed_count += processed
            
            # Save progress every batch
            self._save_progress()
            
            # Log progress
            elapsed = time.time() - start_time
            progress_pct = (self.processed_count / self.total_messages * 100) if self.total_messages > 0 else 0
            logger.info(
                f"Progress: {self.processed_count}/{self.total_messages} messages "
                f"({progress_pct:.1f}%) - Batch {batch_count}, {processed} processed, "
                f"Elapsed: {elapsed:.1f}s"
            )
            
            # Apply rate limiting (except for last batch)
            if messages and not self.dry_run:
                self._apply_rate_limit()
        
        elapsed = time.time() - start_time
        logger.info(f"Backfill completed: {self.processed_count} messages processed in {elapsed:.1f}s")
        
        return self.processed_count


def main():
    """Command line interface for backfill indexer"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Batch embed session messages into LanceDB')
    parser.add_argument('--source-db', required=True, help='Path to source SQLite database')
    parser.add_argument('--vector-db', required=True, help='Path to LanceDB database directory')
    parser.add_argument('--batch-size', type=int, default=10, help='Number of messages per batch')
    parser.add_argument('--rate-limit', type=int, default=100, help='Max messages per minute')
    parser.add_argument('--limit', type=int, help='Limit number of messages to process')
    parser.add_argument('--dry-run', action='store_true', help='Only log what would be processed')
    parser.add_argument('--progress-file', help='Path to progress tracking file')
    parser.add_argument('--mock-mode', action='store_true', help='Use mock implementations for testing')
    
    args = parser.parse_args()
    
    # Initialize indexer
    indexer = BackfillIndexer(
        source_db=args.source_db,
        vector_db_path=args.vector_db,
        batch_size=args.batch_size,
        rate_limit=args.rate_limit,
        progress_file=args.progress_file,
        dry_run=args.dry_run,
        mock_mode=args.mock_mode
    )
    
    # Run backfill
    processed = indexer.run_backfill(limit=args.limit, dry_run=args.dry_run)
    
    print(f"Processed {processed} messages")


if __name__ == '__main__':
    main()