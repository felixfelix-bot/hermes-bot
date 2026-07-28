#!/usr/bin/env python3
"""
Session message chunker for embedding index preparation

Splits long messages into chunks suitable for embedding, skipping tool output
and handling edge cases appropriately.
"""

def chunk_message(msg_id, session_id, role, content, timestamp, max_chars=8000):
    """
    Split a message into chunks suitable for embedding.
    
    Args:
        msg_id (str): Original message ID
        session_id (str): Session ID
        role (str): Message role ('user', 'assistant', 'tool')
        content (str): Message content
        timestamp (str): Message timestamp
        max_chars (int): Maximum characters per chunk (default: 8000)
    
    Returns:
        list: List of chunk dictionaries, or empty list for skipped messages
    """
    # Skip tool messages - they're noise for semantic search
    if role == "tool":
        return []
    
    # Skip empty messages
    if not content or not content.strip():
        return []
    
    # Short message - return as single chunk
    if len(content) <= max_chars:
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
    
    # Split content into chunks
    start_pos = 0
    chunk_index = 0
    
    while start_pos < content_length:
        # Calculate end position for this chunk
        end_pos = start_pos + max_chars
        
        # If we're at the end, take remaining content
        if end_pos >= content_length:
            chunk_content = content[start_pos:]
        else:
            # Try to split at word boundaries for better readability
            chunk_content = content[start_pos:end_pos]
            
            # Find last word boundary
            last_space = chunk_content.rfind(' ')
            if last_space > max_chars * 0.8:  # If we can split within 20% of max length
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


def chunk_messages_batch(messages, max_chars=8000):
    """
    Process multiple messages through the chunker.
    
    Args:
        messages (list): List of message dictionaries with keys:
            - 'id': message ID
            - 'session_id': session ID
            - 'role': message role
            - 'content': message content
            - 'timestamp': message timestamp
        max_chars (int): Maximum characters per chunk (default: 8000)
    
    Returns:
        list: List of all chunks from all messages
    """
    all_chunks = []
    
    for message in messages:
        chunk = chunk_message(
            message['id'],
            message['session_id'],
            message['role'],
            message['content'],
            message['timestamp'],
            max_chars
        )
        all_chunks.extend(chunk)
    
    return all_chunks


def get_chunk_stats(chunks):
    """
    Get statistics about a list of chunks.
    
    Args:
        chunks (list): List of chunk dictionaries
    
    Returns:
        dict: Statistics including total chunks, total characters,
              average chunk size, original message count
    """
    if not chunks:
        return {
            'total_chunks': 0,
            'total_chars': 0,
            'avg_chunk_size': 0,
            'original_messages': 0
        }
    
    total_chars = sum(len(chunk['content']) for chunk in chunks)
    avg_chunk_size = total_chars / len(chunks)
    
    # Estimate original messages by counting chunk IDs with different base IDs
    unique_msg_ids = set()
    for chunk in chunks:
        # Extract base message ID (before _chunk_N)
        base_id = chunk['id'].split('_chunk_')[0]
        unique_msg_ids.add(base_id)
    
    return {
        'total_chunks': len(chunks),
        'total_chars': total_chars,
        'avg_chunk_size': avg_chunk_size,
        'original_messages': len(unique_msg_ids)
    }