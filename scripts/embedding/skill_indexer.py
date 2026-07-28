#!/usr/bin/env python3
"""
Skill indexer - embed skill descriptions for routing.

This module scans SKILL.md files, extracts metadata, embeds the descriptions,
and stores them in LanceDB for efficient search and retrieval.
"""

import os
import re
import yaml
import json
from pathlib import Path
from typing import List, Dict, Any, Optional
import logging

# Import existing components
from embedding.embed_client import EmbedClient
from embedding.skill_vector_store import SkillVectorStore


class SkillIndexer:
    """
    Indexer for skill descriptions with embedding-based search.
    
    Scans SKILL.md files, extracts metadata, embeds descriptions,
    and stores in LanceDB for routing purposes.
    """
    
    def __init__(self, skills_dir: str, db_path: str, ollama_url: str = "http://localhost:11434/api/embeddings"):
        """
        Initialize skill indexer.
        
        Args:
            skills_dir: Directory containing SKILL.md files
            db_path: Path to LanceDB database
            ollama_url: URL for ollama embeddings API
        """
        # Store as string to maintain consistency
        self.skills_dir = str(skills_dir)
        self.db_path = db_path
        self.ollama_url = ollama_url
        
        # Initialize components with skill-specific vector store
        self.embed_client = EmbedClient(ollama_url=ollama_url)
        self.vector_store = SkillVectorStore(db_path, table_name='skills')
        
        # Setup logging
        logging.basicConfig(level=logging.INFO)
        self.logger = logging.getLogger(__name__)
        
        # Ensure table exists with correct schema
        if not self.vector_store.table_exists():
            self.vector_store._create_table()
    
    def _scan_skills_directory(self) -> List[Path]:
        """
        Scan skills directory for all SKILL.md files.
        
        Returns:
            List of paths to SKILL.md files
        """
        skills_path = Path(self.skills_dir)
        
        if not skills_path.exists():
            self.logger.warning(f"Skills directory does not exist: {skills_path}")
            return []
        
        skill_files = list(skills_path.rglob("SKILL.md"))
        self.logger.info(f"Found {len(skill_files)} SKILL.md files")
        return skill_files
    
    def _extract_skill_metadata(self, skill_file_path: str) -> Dict[str, Any]:
        """
        Extract metadata from a SKILL.md file.
        
        Args:
            skill_file_path: Path to SKILL.md file
            
        Returns:
            Dictionary with extracted metadata
        """
        skill_file = Path(skill_file_path)
        
        if not skill_file.exists():
            raise FileNotFoundError(f"Skill file not found: {skill_file_path}")
        
        # Read file content
        content = skill_file.read_text(encoding='utf-8')
        
        # Extract YAML frontmatter
        frontmatter_match = re.match(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
        if not frontmatter_match:
            raise ValueError(f"No YAML frontmatter found in {skill_file_path}")
        
        frontmatter_text = frontmatter_match.group(1)
        
        try:
            frontmatter = yaml.safe_load(frontmatter_text)
        except yaml.YAMLError as e:
            raise ValueError(f"Invalid YAML in {skill_file_path}: {e}")
        
        # Extract category from directory structure
        category = skill_file.parent.name
        
        # Extract tags from multiple possible locations
        tags = []
        
        # From tags field in frontmatter
        if 'tags' in frontmatter:
            tags.extend(frontmatter['tags'])
        
        # From hermes.metadata.tags
        if ('metadata' in frontmatter and 
            'hermes' in frontmatter['metadata'] and 
            'tags' in frontmatter['metadata']['hermes']):
            tags.extend(frontmatter['metadata']['hermes']['tags'])
        
        # Remove duplicates while preserving order
        seen = set()
        unique_tags = []
        for tag in tags:
            if tag not in seen:
                seen.add(tag)
                unique_tags.append(tag)
        
        # Build metadata dictionary
        metadata = {
            'name': frontmatter.get('name', 'unknown'),
            'description': frontmatter.get('description', ''),
            'tags': unique_tags,
            'category': category,
            'content': content,  # Full content for context
            'file_path': str(skill_file)
        }
        
        return metadata
    
    def _build_concatenated_text(self, skill_data: Dict[str, Any]) -> str:
        """
        Build concatenated text for embedding.
        
        Args:
            skill_data: Skill metadata dictionary
            
        Returns:
            Concatenated text for embedding
        """
        name = skill_data['name']
        description = skill_data['description']
        tags = skill_data['tags']
        
        # Format: "{name}: {description} Tags: {tags}"
        tags_str = json.dumps(tags)  # Properly format tags as JSON string (uses double quotes)
        concatenated = f"{name}: {description} Tags: {tags_str}"
        
        return concatenated
    
    def index_skills(self) -> int:
        """
        Index all skills from the skills directory.
        
        Returns:
            Number of skills indexed
        """
        self.logger.info("Starting skill indexing...")
        
        # Scan for skill files
        skill_files = self._scan_skills_directory()
        if not skill_files:
            self.logger.warning("No skill files found to index")
            return 0
        
        # Process each skill file
        indexed_count = 0
        records = []
        
        for skill_file in skill_files:
            try:
                # Extract metadata
                skill_data = self._extract_skill_metadata(str(skill_file))
                
                # Build text for embedding
                concatenated_text = self._build_concatenated_text(skill_data)
                
                # Generate embedding
                self.logger.info(f"Embedding skill: {skill_data['name']}")
                vector = self.embed_client.embed(concatenated_text)
                
                # Create record for vector store
                record = {
                    'id': f"skill_{skill_data['name']}",
                    'name': skill_data['name'],
                    'description': skill_data['description'],
                    'tags': skill_data['tags'],
                    'category': skill_data['category'],
                    'content': skill_data['content'],
                    'file_path': skill_data['file_path'],
                    'vector': vector,
                    'timestamp': skill_data.get('created_at', '')
                }
                
                records.append(record)
                indexed_count += 1
                
            except Exception as e:
                self.logger.error(f"Error processing {skill_file}: {e}")
                continue
        
        # Insert all records into vector store
        if records:
            self.vector_store.insert(records)
            self.logger.info(f"Successfully indexed {indexed_count} skills")
        else:
            self.logger.warning("No skills were successfully indexed")
        
        return indexed_count
    
    def search(self, query: str, limit: int = 10, category: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Search for skills using embedding similarity.
        
        Args:
            query: Search query text
            limit: Maximum number of results to return
            category: Optional category to filter results
            
        Returns:
            List of skill records with similarity scores
        """
        # Generate embedding for query
        query_vector = self.embed_client.embed(query)
        
        # Search in vector store
        results = self.vector_store.search(query_vector, limit=limit, category=category)
        
        # Format results
        formatted_results = []
        for result in results:
            formatted_result = {
                'id': result['id'],
                'name': result['name'],
                'description': result['description'],
                'tags': result['tags'],
                'category': result['category'],
                'distance': result.get('distance', float('inf')),
                'content': result['content'],
                'file_path': result['file_path']
            }
            formatted_results.append(formatted_result)
        
        return formatted_results
    
    def get_skills_count(self) -> int:
        """
        Get total count of indexed skills.
        
        Returns:
            Number of indexed skills
        """
        return self.vector_store.count()
    
    def get_skills_by_category(self, category: str) -> List[Dict[str, Any]]:
        """
        Get all skills in a specific category.
        
        Args:
            category: Category name
            
        Returns:
            List of skill records in the specified category
        """
        # Get all skills
        all_skills = self.search("", limit=1000)  # High limit to get all
        
        # Filter by category
        category_skills = [
            skill for skill in all_skills 
            if skill['category'] == category
        ]
        
        return category_skills
    
    def get_all_categories(self) -> List[str]:
        """
        Get all available categories.
        
        Returns:
            List of category names
        """
        return self.vector_store.get_categories()