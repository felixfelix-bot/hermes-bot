# Skill Router Plugin

This Hermes plugin provides `route_skills()`, a fast skill-selection tool for
the agent loop. Instead of scanning all 200+ installed skills every turn, it
embeds the user prompt, queries the LanceDB `skills` table built by
`skill_indexer.py`, and returns the top-k most relevant skill names.

## Location

```text
~/.hermes/profiles/manager/plugins/search/skill_router.py
```

## Interface

```python
route_skills(
    prompt: str,
    top_k: int = 5,
    skills_dir: Optional[str] = None,
    db_path: Optional[str] = None,
    cache_path: Optional[str] = None,
    ollama_url: Optional[str] = None,
) -> dict
```

Returns:

```json
{
  "recommended_skills": ["skill1", "skill2", ...],
  "confidence": 0.82,
  "fallback": false
}
```

* `recommended_skills` - ordered list of skill names, most relevant first.
* `confidence` - overall confidence based on the best vector-search distance.
* `fallback` - `true` when the router could not produce recommendations. The
caller should fall back to scanning all skills.

## Fallback behaviour

The router never blocks the agent loop. It returns `fallback: true` with an
empty `recommended_skills` list when any of the following occur:

* The prompt is empty or whitespace-only.
* Ollama / `EmbedClient` is unavailable.
* LanceDB / `SkillVectorStore` cannot be opened.
* The skills table is empty and `index_skills()` fails or still yields no records.
* Vector search returns no results.

## Configuration

Set these environment variables to override defaults:

| Variable | Default | Purpose |
|---|---|---|
| `HERMES_SKILLS_DIR` | `~/.hermes/profiles/worker-admin/skills` | Directory containing `SKILL.md` files to index. |
| `HERMES_SKILL_DB_PATH` | `~/.hermes/skill_index.db` | LanceDB database with the `skills` table. |
| `HERMES_SKILL_EMBED_CACHE` | `~/.hermes/skill_embed_cache.db` | SQLite cache for prompt embeddings. |
| `HERMES_OLLAMA_URL` | `http://localhost:11434/api/embeddings` | Ollama embeddings endpoint. |

## Usage from code

```python
from skill_router import route_skills

result = route_skills("flash esp32 firmware", top_k=5)
print(result["recommended_skills"])
```

## CLI smoke test

```bash
python3 skill_router.py "flash esp32 firmware" --top-k 5
```

## Running tests

Tests live in the kanban workspace for this task:

```bash
cd ~/.hermes/kanban/boards/embeddings/workspaces/t_a126f26c
PYTHONPATH=~/.hermes/profiles/manager/plugins/search:~/repos/hermes-bot/scripts:~/repos/hermes-bot/scripts/embedding \
  python3 -m pytest test_skill_router.py -v --cov=skill_router
```

## Dependencies

* `embed_client.py` - Ollama/nomic-embed-text wrapper with SQLite caching.
* `skill_indexer.py` - SKILL.md scanner, embedder, and indexer.
* `skill_vector_store.py` - LanceDB table management for skill records.

All three are provided by the `hermes-bot` repo under `scripts/embedding/`.
