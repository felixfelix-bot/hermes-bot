#!/usr/bin/env python3
"""
skill_router.py - Hermes plugin tool for selecting relevant skills.

Given a user prompt, embed the prompt and query the LanceDB skills table
(populated by skill_indexer.py) to return the top-k most relevant skill names.
If the embedding client or LanceDB is unavailable, it falls back to an empty
recommendation list so the agent loop can continue scanning all skills.

Tool interface:
    route_skills(prompt: str, top_k: int = 5) -> dict

Returns:
    {
        "recommended_skills": ["skill1", "skill2", ...],
        "confidence": 0.82,
        "fallback": false
    }
"""

import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Reuse the shared indexing/embedding components from hermes-bot.
repo_path = Path.home() / "repos" / "hermes-bot"
scripts_path = repo_path / "scripts"
for _path in (scripts_path, scripts_path / "embedding"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from embedding.embed_client import EmbedClient
from embedding.skill_indexer import SkillIndexer
from embedding.skill_vector_store import SkillVectorStore

logger = logging.getLogger(__name__)

# Default locations; callers may override via environment variables.
DEFAULT_SKILLS_DIR = Path.home() / ".hermes" / "profiles" / "worker-admin" / "skills"
DEFAULT_DB_PATH = str(Path.home() / ".hermes" / "skill_index.db")
DEFAULT_CACHE_PATH = str(Path.home() / ".hermes" / "skill_embed_cache.db")
DEFAULT_OLLAMA_URL = "http://localhost:11434/api/embeddings"


def _build_skills_dir() -> Path:
    """Resolve the skills directory from the environment or default path."""
    skills_dir = os.environ.get("HERMES_SKILLS_DIR")
    if skills_dir:
        return Path(skills_dir)
    return DEFAULT_SKILLS_DIR


def _build_db_path() -> str:
    """Resolve the LanceDB skills DB path from the environment or default."""
    return os.environ.get("HERMES_SKILL_DB_PATH", DEFAULT_DB_PATH)


def _build_cache_path() -> str:
    """Resolve the SQLite embedding cache path from the environment or default."""
    return os.environ.get("HERMES_SKILL_EMBED_CACHE", DEFAULT_CACHE_PATH)


def _build_ollama_url() -> str:
    """Resolve the Ollama embeddings URL from the environment or default."""
    return os.environ.get("HERMES_OLLAMA_URL", DEFAULT_OLLAMA_URL)


def _ensure_indexed(indexer: SkillIndexer) -> bool:
    """Return True if the skills table already has indexed records."""
    try:
        return indexer.get_skills_count() > 0
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not check skills count: %s", exc)
        return False


def _compute_confidence(distances: List[float]) -> float:
    """
    Convert vector search distances into an overall confidence score.

    LanceDB cosine distances live in [0, 2] for normalized vectors. We map the
    best (minimum) distance to a confidence in [0, 1].
    """
    if not distances:
        return 0.0
    best_distance = min(distances)
    # Cosine distance of 0 -> perfect match (confidence 1.0).
    # Cosine distance of 2 -> opposite (confidence 0.0).
    confidence = max(0.0, 1.0 - (best_distance / 2.0))
    return round(float(confidence), 2)


def route_skills(
    prompt: str,
    top_k: int = 5,
    skills_dir: Optional[str] = None,
    db_path: Optional[str] = None,
    cache_path: Optional[str] = None,
    ollama_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Given a user prompt, return the most relevant skills to load.

    Args:
        prompt: The user message to route against indexed skills.
        top_k: Maximum number of skill recommendations to return.
        skills_dir: Optional override for the skills directory.
        db_path: Optional override for the LanceDB database path.
        cache_path: Optional override for the SQLite embedding cache.
        ollama_url: Optional override for the Ollama embeddings endpoint.

    Returns:
        A dict with keys ``recommended_skills`` (list[str]), ``confidence``
        (float), and ``fallback`` (bool). If any dependency is unavailable or
        the skills table is empty, ``fallback`` is True and the recommended list
        is empty so the caller can fall back to scanning all skills.
    """
    if not prompt or not str(prompt).strip():
        return {"recommended_skills": [], "confidence": 0.0, "fallback": True}

    resolved_skills_dir = str(skills_dir) if skills_dir else str(_build_skills_dir())
    resolved_db_path = db_path or _build_db_path()
    resolved_cache_path = cache_path or _build_cache_path()
    resolved_ollama_url = ollama_url or _build_ollama_url()

    try:
        embed_client = EmbedClient(
            db_path=resolved_cache_path,
            ollama_url=resolved_ollama_url,
        )
        vector_store = SkillVectorStore(resolved_db_path, table_name="skills")
        indexer = SkillIndexer(
            skills_dir=resolved_skills_dir,
            db_path=resolved_db_path,
            ollama_url=resolved_ollama_url,
        )
        # Replace the indexer's vector store and embed client with our own so we
        # share the same cache and table instance.
        indexer.embed_client = embed_client
        indexer.vector_store = vector_store

        if not _ensure_indexed(indexer):
            logger.info("Skills table empty; running index_skills().")
            try:
                indexer.index_skills()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to index skills: %s", exc)
                return {"recommended_skills": [], "confidence": 0.0, "fallback": True}

        if indexer.get_skills_count() == 0:
            return {"recommended_skills": [], "confidence": 0.0, "fallback": True}

        query_vector = embed_client.embed(str(prompt))
        results = vector_store.search(query_vector, limit=int(top_k))
        results = results[:int(top_k)]

        if not results:
            return {"recommended_skills": [], "confidence": 0.0, "fallback": True}

        recommended_skills = [result["name"] for result in results if "name" in result]
        distances = [
            float(result.get("distance", float("inf")))
            for result in results
            if result.get("distance") is not None
        ]
        confidence = _compute_confidence(distances)

        return {
            "recommended_skills": recommended_skills,
            "confidence": confidence,
            "fallback": False,
        }

    except Exception as exc:  # noqa: BLE001
        logger.warning("skill_router fallback: %s", exc)
        return {"recommended_skills": [], "confidence": 0.0, "fallback": True}


if __name__ == "__main__":
    # Tiny CLI smoke test.
    import argparse

    parser = argparse.ArgumentParser(description="Route a prompt to relevant skills")
    parser.add_argument("prompt", help="User prompt to route")
    parser.add_argument("--top-k", type=int, default=5, help="Number of recommendations")
    args = parser.parse_args()

    output = route_skills(args.prompt, top_k=args.top_k)
    print(output)
