#!/usr/bin/env python3
"""code_index — local vector index of all repos for semantic code search.

Uses Ollama's nomic-embed-text model. Stores embeddings in SQLite.
Workers call `search` before reading files — saves ~90% tokens on code lookup.

Usage:
  code_index.py build           # rebuild entire index (walks all repos)
  code_index.py search "query"  # semantic search, returns top-5 snippets
  code_index.py stats           # index statistics
"""
from __future__ import annotations
import json, os, sqlite3, sys, time, urllib.request
import struct, math
from pathlib import Path

HOME = Path.home()
DB_PATH = HOME / ".hermes" / "bot" / "code_index.db"
OLLAMA_URL = "http://127.0.0.1:11434/api/embeddings"
EMBED_MODEL = "nomic-embed-text"
MAX_CHUNK_LINES = 80
MIN_CHUNK_LINES = 5
TOP_K = 5

CODE_EXTS = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".c", ".cpp",
             ".h", ".hpp", ".sh", ".bash", ".yml", ".yaml", ".toml",
             ".json", ".md", ".vue", ".svelte", ".rb", ".java", ".kt",
             ".swift", ".lua", ".dockerfile", ".env", ".ini", ".cfg"}

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
             "dist", "build", ".next", ".cache", "target", "*.egg-info",
             ".hermes", ".opencode", ".config", ".local", ".cache",
             "Downloads", "Desktop", ".bun", ".cargo"}


def embed(text: str) -> list[float]:
    """Get embedding from Ollama."""
    try:
        req = urllib.request.Request(
            OLLAMA_URL,
            data=json.dumps({"model": EMBED_MODEL, "prompt": text[:8000]}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())["embedding"]
    except Exception as e:
        print(f"  embed error: {e}", file=sys.stderr)
        return []


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed multiple texts (sequential — Ollama handles one at a time)."""
    return [embed(t) for t in texts]


def blob(vec: list[float]) -> bytes:
    """Pack float list into bytes."""
    return struct.pack(f"{len(vec)}f", *vec)


def unblob(data: bytes) -> list[float]:
    """Unpack bytes back to float list."""
    n = len(data) // 4
    return list(struct.unpack(f"{n}f", data))


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def chunk_file(path: Path) -> list[tuple[int, int, str]]:
    """Split a file into chunks by function/class boundaries.
    Returns list of (line_start, line_end, content)."""
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except Exception:
        return []

    chunks = []
    current_start = 0
    current_lines = []

    for i, line in enumerate(lines):
        current_lines.append(line)

        is_boundary = (
            (line.strip() == "" and len(current_lines) >= MIN_CHUNK_LINES)
            or len(current_lines) >= MAX_CHUNK_LINES
            or (line.startswith("def ") or line.startswith("class ")
                or line.startswith("function ") or line.startswith("export ")
                or line.startswith("pub fn ") or line.startswith("fn ")
                or line.startswith("// ---") or line.startswith("# ---"))
            and len(current_lines) > MIN_CHUNK_LINES
        )

        if is_boundary:
            content = "\n".join(current_lines).strip()
            if content:
                chunks.append((current_start, i, content))
            current_start = i + 1
            current_lines = []

    if current_lines:
        content = "\n".join(current_lines).strip()
        if content:
            chunks.append((current_start, len(lines) - 1, content))

    return chunks


def find_repos() -> list[Path]:
    """Find all git repos in ~/*/ and ~/worktrees/*/."""
    repos = []
    for pattern in ["*/", "worktrees/*/"]:
        for d in HOME.glob(pattern):
            if not (d / ".git").exists():
                continue
            if d.name in SKIP_DIRS:
                continue
            repos.append(d)
    return repos


def init_db(db: sqlite3.Connection):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY,
            repo TEXT,
            file_path TEXT,
            line_start INTEGER,
            line_end INTEGER,
            content TEXT,
            embedding BLOB
        );
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        DELETE FROM chunks;
    """)


def build():
    """Rebuild the entire index."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(DB_PATH))
    init_db(db)

    repos = find_repos()
    total_files = 0
    total_chunks = 0

    for repo in repos:
        repo_files = 0
        repo_chunks = 0

        for path in repo.rglob("*"):
            if any(skip in path.parts for skip in SKIP_DIRS):
                continue
            if not path.is_file():
                continue
            if path.suffix.lower() not in CODE_EXTS and path.name not in ("Dockerfile", "Makefile", "AGENTS.md"):
                continue

            rel_path = str(path.relative_to(repo))
            chunks = chunk_file(path)
            if not chunks:
                continue

            repo_files += 1
            batch_texts = [c[2] for c in chunks]
            embeddings = embed_batch(batch_texts)

            for (ls, le, content), emb in zip(chunks, embeddings):
                if emb:
                    db.execute(
                        "INSERT INTO chunks (repo, file_path, line_start, line_end, content, embedding) VALUES (?,?,?,?,?,?)",
                        (repo.name, rel_path, ls, le, content[:4000], blob(emb)),
                    )
                    repo_chunks += 1
                    total_chunks += 1

        if repo_chunks:
            print(f"  {repo.name}: {repo_files} files, {repo_chunks} chunks")
            total_files += repo_files

    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
               ("built_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
               ("total_files", str(total_files)))
    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
               ("total_chunks", str(total_chunks)))
    db.commit()
    db.close()
    print(f"\nIndex built: {total_files} files, {total_chunks} chunks across {len(repos)} repos")


def search(query: str, k: int = TOP_K):
    """Search the index for relevant code snippets."""
    if not DB_PATH.exists():
        print("No index found. Run: code_index.py build", file=sys.stderr)
        return

    qemb = embed(query)
    if not qemb:
        print("Failed to embed query. Is Ollama running?", file=sys.stderr)
        return

    db = sqlite3.connect(str(DB_PATH))
    rows = db.execute("SELECT repo, file_path, line_start, line_end, content, embedding FROM chunks").fetchall()
    db.close()

    if not rows:
        print("Index is empty. Run: code_index.py build", file=sys.stderr)
        return

    scored = []
    for repo, fpath, ls, le, content, emb_blob in rows:
        sim = cosine(qemb, unblob(emb_blob))
        scored.append((sim, repo, fpath, ls, le, content))

    scored.sort(key=lambda x: -x[0])

    for rank, (sim, repo, fpath, ls, le, content) in enumerate(scored[:k], 1):
        print(f"\n{'='*60}")
        print(f"#{rank} [{sim:.1%}] {repo}/{fpath}:{ls+1}-{le+1}")
        print(f"{'='*60}")
        preview = content[:600]
        if len(content) > 600:
            preview += "\n... (truncated)"
        print(preview)


def stats():
    """Show index statistics."""
    if not DB_PATH.exists():
        print("No index found.")
        return

    db = sqlite3.connect(str(DB_PATH))
    meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
    chunk_count = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    repos = db.execute("SELECT DISTINCT repo FROM chunks").fetchall()
    db.close()

    print(f"Index: {DB_PATH}")
    print(f"  Built: {meta.get('built_at', '?')}")
    print(f"  Chunks: {chunk_count}")
    print(f"  Repos: {len(repos)}")
    size = DB_PATH.stat().st_size / 1e6
    print(f"  Size: {size:.1f}MB")


if __name__ == "__main__":
    # Default to `build` when invoked with no arguments so the nightly
    # refresh cron (which passes no args) actually rebuilds the index
    # instead of just printing help and exiting.
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"

    if cmd == "build":
        build()
    elif cmd == "search":
        if len(sys.argv) < 3:
            print("Usage: code_index.py search \"query\"", file=sys.stderr)
            sys.exit(1)
        search(" ".join(sys.argv[2:]))
    elif cmd == "stats":
        stats()
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)
