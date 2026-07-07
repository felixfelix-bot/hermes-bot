#!/usr/bin/env python3
"""worktree_sync — reconcile Hermes worker profiles to `git worktree list`.

Source of truth = git worktrees. Creates one Hermes profile per worktree (slug
<repo>-<branch>, sanitized/truncated), skips /tmp, migrates /tmp worktrees out,
auto-assigns worker_harness from repo language. Idempotent; config-only (NO LLM
tokens). Writes ~/.hermes/bot/worktree_manifest.json.

Reads repo list from ~/.hermes/bot/repos.txt (one path per line, $HOME-relative
or absolute). worker-base profile must exist (created by 00-bootstrap).
"""
from __future__ import annotations
import json, os, re, subprocess, sys
from pathlib import Path

HOME = Path.home()
BOT = HOME / ".hermes" / "bot"
MANIFEST = BOT / "worktree_manifest.json"
REPOS_FILE = BOT / "repos.txt"
WORKTREES_DIR = HOME / "worktrees"
PARK_AFTER_DAYS = int(os.environ.get("PARK_AFTER_DAYS", "14"))


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def existing_profiles() -> set[str]:
    r = run(["hermes", "profile", "list"])
    out = set()
    for line in r.stdout.splitlines():
        line = line.strip()
        if line and not line.startswith("Profile") and not line.startswith("─"):
            out.add(line.split()[0])
    return out


def slugify(repo: str, branch: str) -> str:
    base = Path(repo).name
    s = f"{base}-{branch}".lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:40]


def default_branch(repo: str) -> str:
    r = run(["git", "-C", repo, "symbolic-ref", "refs/remotes/origin/HEAD"])
    if r.returncode == 0:
        return r.stdout.strip().split("heads/")[-1]
    for b in ("main", "master"):
        if run(["git", "-C", repo, "show-ref", "--verify", "--quiet", f"refs/heads/{b}"]).returncode == 0:
            return b
    return "main"


def harness_for(repo: str) -> str:
    """typed/compiled -> opencode; dynamic -> pi."""
    p = Path(repo)
    typed = ["Cargo.toml", "go.mod", "package.json", "platformio.ini", "sdkconfig", "CMakeLists.txt"]
    if any((p / m).exists() for m in typed):
        # package.json could be JS (dynamic) — check for .ts/.tsx presence
        if (p / "package.json").exists() and not any(p.rglob("*.ts")) and not any(p.rglob("*.tsx")):
            return "pi"
        return "opencode"
    if any((p / m).exists() for m in ("pyproject.toml", "setup.py")) or list(p.glob("*.py")):
        return "pi"
    return "opencode"  # default to the richer harness


def parse_worktrees(repo: str):
    r = run(["git", "-C", repo, "worktree", "list", "--porcelain"])
    wt, branch = None, None
    for line in r.stdout.splitlines():
        if line.startswith("worktree "):
            wt = line.split(" ", 1)[1]
        elif line.startswith("branch "):
            branch = line.split(" ", 1)[1].split("heads/")[-1]
        elif line == "" and wt:
            yield wt, branch or "HEAD"
            wt, branch = None, None
    if wt:
        yield wt, branch or "HEAD"


def migrate_tmp(repo: str, wt: str, branch: str) -> str | None:
    """Move a /tmp worktree to ~/worktrees/<name>; return new path or None."""
    if not wt.startswith("/tmp"):
        return None
    name = Path(wt).name or slugify(repo, branch)
    WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
    dest = WORKTREES_DIR / name
    if dest.exists():
        return str(dest)
    r = run(["git", "-C", repo, "worktree", "move", wt, str(dest)])
    if r.returncode == 0:
        print(f"  migrated {wt} -> {dest}")
        return str(dest)
    print(f"  WARN: could not move {wt}: {r.stderr.strip()}", file=sys.stderr)
    return None


def main() -> int:
    BOT.mkdir(parents=True, exist_ok=True)
    if not REPOS_FILE.exists():
        print(f"missing {REPOS_FILE}; create it (one repo path per line)", file=sys.stderr)
        return 1
    profiles = existing_profiles()
    has_base = "worker-base" in profiles
    if not has_base:
        print("WARN: worker-base profile missing — profiles will NOT be created yet.", file=sys.stderr)

    manifest = []
    for line in REPOS_FILE.read_text().splitlines():
        repo = line.strip()
        if not repo or repo.startswith("#"):
            continue
        repo = str(Path(repo).expanduser())
        if not Path(repo).is_dir():
            continue
        db = default_branch(repo)
        harn = harness_for(repo)
        for wt, branch in parse_worktrees(repo):
            new = migrate_tmp(repo, wt, branch)
            if new:
                wt = new
            if wt.startswith("/tmp"):
                print(f"  SKIP (still /tmp): {wt}")
                continue
            slug = slugify(repo, branch)
            entry = {"slug": slug, "repo": repo, "worktree": wt, "branch": branch,
                     "default_branch": db, "harness": harn}
            manifest.append(entry)
            if has_base and slug not in profiles:
                rr = run(["hermes", "profile", "create", slug,
                          "--clone-from", "worker-base",
                          "--description", f"{Path(repo).name} worker ({branch})",
                          "--no-alias"])
                if rr.returncode == 0:
                    print(f"  + profile {slug}  [{harn}]  {wt}")
                    profiles.add(slug)
                else:
                    print(f"  ! profile {slug} failed: {rr.stderr.strip()[:120]}", file=sys.stderr)
            elif slug in profiles:
                print(f"  = profile {slug} exists  [{harn}]")
    MANIFEST.write_text(json.dumps(manifest, indent=2))
    print(f"manifest: {len(manifest)} worktrees -> {MANIFEST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
