#!/usr/bin/env python3
"""
Test suite for skill_indexer.py - TDD implementation
"""

import unittest
import tempfile
import shutil
import os
import sys
from pathlib import Path

# Add the repo scripts to path so we can import the skill indexer
repo_path = Path.home() / "repos" / "hermes-bot"
scripts_path = repo_path / "scripts"
sys.path.insert(0, str(scripts_path))

from embedding.skill_indexer import SkillIndexer


class TestSkillIndexer(unittest.TestCase):
    
    def setUp(self):
        """Set up test environment with temporary directory"""
        self.temp_dir = tempfile.mkdtemp()
        self.skills_dir = Path(self.temp_dir) / "skills"
        self.skills_dir.mkdir()
        self.db_path = str(Path(self.temp_dir) / "test_skills.db")
        
        # Create fixture skills
        self._create_fixture_skills()
        
        # Initialize indexer
        self.indexer = SkillIndexer(
            skills_dir=str(self.skills_dir),
            db_path=self.db_path
        )
    
    def tearDown(self):
        """Clean up temporary directory"""
        shutil.rmtree(self.temp_dir)
    
    def _create_fixture_skills(self):
        """Create 5 fake SKILL.md files for testing"""
        skills_data = [
            {
                "name": "test-skill-1",
                "description": "A test skill for firmware flashing",
                "tags": ["hardware", "firmware", "esp32"],
                "category": "hardware",
                "content": "# Test Skill 1\nThis skill handles ESP32 firmware flashing."
            },
            {
                "name": "test-skill-2", 
                "description": "RP2040 microcontroller programming",
                "tags": ["hardware", "rp2040", "programming"],
                "category": "hardware",
                "content": "# Test Skill 2\nRP2040 programming utilities."
            },
            {
                "name": "social-media-posting",
                "description": "Automated social media content posting",
                "tags": ["social-media", "automation", "marketing"],
                "category": "social-media",
                "content": "# Social Media Posting\nAutomated posting to social platforms."
            },
            {
                "name": "web-scraping",
                "description": "Web scraping tools and utilities",
                "tags": ["web", "scraping", "data-extraction"],
                "category": "web",
                "content": "# Web Scraping\nExtract data from websites."
            },
            {
                "name": "data-analysis",
                "description": "Statistical data analysis and visualization",
                "tags": ["data", "analysis", "visualization", "statistics"],
                "category": "data-science",
                "content": "# Data Analysis\nStatistical analysis tools."
            }
        ]
        
        # Create the skill files
        for i, skill_data in enumerate(skills_data):
            skill_dir = self.skills_dir / skill_data["category"] / f"skill-{i+1}"
            skill_dir.mkdir(parents=True)
            
            skill_file = skill_dir / "SKILL.md"
            frontmatter = f"""---
name: {skill_data['name']}
description: {skill_data['description']}
tags: {skill_data['tags']}
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: {skill_data['tags']}
---

# {skill_data['name']}

{skill_data['content']}
"""
            skill_file.write_text(frontmatter)
    
    def test_indexer_initialization(self):
        """Test that indexer initializes correctly"""
        self.assertIsNotNone(self.indexer)
        self.assertEqual(self.indexer.skills_dir, str(self.skills_dir))
        self.assertEqual(self.indexer.db_path, self.db_path)
    
    def test_scan_skills_directory(self):
        """Test that skills directory is scanned correctly"""
        skill_files = self.indexer._scan_skills_directory()
        self.assertEqual(len(skill_files), 5)
        
        # Check that all skills have required fields when metadata is extracted
        for skill_file in skill_files:
            metadata = self.indexer._extract_skill_metadata(str(skill_file))
            self.assertIn('name', metadata)
            self.assertIn('description', metadata)
            self.assertIn('tags', metadata)
            self.assertIn('category', metadata)
            self.assertIn('content', metadata)
    
    def test_extract_skill_metadata(self):
        """Test extraction of skill metadata from SKILL.md files"""
        skill_files = list(self.skills_dir.rglob("SKILL.md"))
        self.assertEqual(len(skill_files), 5)
        
        for skill_file in skill_files:
            metadata = self.indexer._extract_skill_metadata(str(skill_file))
            self.assertIsInstance(metadata, dict)
            self.assertIn('name', metadata)
            self.assertIn('description', metadata)
            self.assertIn('tags', metadata)
            self.assertIn('category', metadata)
            self.assertIn('content', metadata)
    
    def test_build_concatenated_text(self):
        """Test building concatenated text for embedding"""
        skill_data = {
            'name': 'test-skill',
            'description': 'Test description',
            'tags': ['tag1', 'tag2'],
            'category': 'test'
        }
        
        concatenated = self.indexer._build_concatenated_text(skill_data)
        expected = 'test-skill: Test description Tags: ["tag1", "tag2"]'
        self.assertEqual(concatenated, expected)
    
    def test_index_skills(self):
        """Test indexing all skills"""
        # Index skills
        count = self.indexer.index_skills()
        self.assertEqual(count, 5)
        
        # Check that table exists and has data
        info = self.indexer.vector_store.get_table_info()
        self.assertTrue(info['exists'])
        self.assertEqual(info['count'], 5)
    
    def test_skill_search(self):
        """Test searching for skills with specific query"""
        # Index skills first
        self.indexer.index_skills()
        
        # Search for "firmware flashing" - should return hardware skills
        results = self.indexer.search("firmware flashing", limit=3)
        self.assertGreater(len(results), 0)
        
        # Check that results contain relevant skills
        found_hardware = any('hardware' in result['name'].lower() or 
                          'hardware' in result['content'].lower() 
                          for result in results)
        self.assertTrue(found_hardware)
        
        # Search for social media - should return social media skills
        results_social = self.indexer.search("social media", limit=3)
        found_social = any('social-media' in result['name'].lower() or
                         'social' in result['content'].lower()
                         for result in results_social)
        self.assertTrue(found_social)
    
    def test_skill_search_with_limit(self):
        """Test search with result limit"""
        self.indexer.index_skills()
        
        # Search with limit of 2
        results = self.indexer.search("test", limit=2)
        self.assertLessEqual(len(results), 2)
    
    def test_get_skills_count(self):
        """Test getting total count of indexed skills"""
        count = self.indexer.get_skills_count()
        self.assertEqual(count, 0)  # Should be 0 before indexing
        
        # Index skills
        self.indexer.index_skills()
        
        # Count should be 5 after indexing
        count = self.indexer.get_skills_count()
        self.assertEqual(count, 5)


if __name__ == '__main__':
    unittest.main()