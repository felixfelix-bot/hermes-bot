#!/usr/bin/env python3
"""gen_handovers — rebuild the worktree → handover index.

Scans every git worktree reachable from repos.txt, records repo / branch /
HANDOVER.md presence, and writes ~/.hermes/bot/handovers/INDEX.md. Internal-only
(no tokens, no posting). Idempotent: safe to run nightly; the index is the map
the manager reads to find a project's resumable state.

Repo name is derived from each worktree's git common-dir (the linked worktree's
common-dir points at the main repo's .git), so `esp-miner-tollgate` reports its
real repo `esp-miner`, not its worktree dir name.
"""
from __future__ import annotations
import re
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
BOT = HOME / ".hermes" / "bot"
REPOS = BOT / "repos.txt"
HANDOVERS = BOT / "handovers"
OUT = HANDOVERS / "INDEX.md"


def git(wt: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(wt), *args],
        capture_output=True, text=True,
    ).stdout.strip()


def repo_of(wt: Path) -> str:
    """Real repo name for a worktree via its git common-dir."""
    common = git(wt, "rev-parse", "--git-common-dir")
    if not common:
        return wt.name
    cabs = (wt / common).resolve() if not Path(common).is_absolute() else Path(common).resolve()
    # common-dir is `<repo>/.git` for both main and linked worktrees.
    if cabs.name == ".git":
        return cabs.parent.name or wt.name
    return wt.name


def worktrees_from(repos_file: Path) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for line in repos_file.read_text().splitlines():
        r = line.strip()
        if not r or r.startswith("#") or not Path(r).is_dir():
            continue
        res = subprocess.run(
            ["git", "-C", r, "worktree", "list", "--porcelain"],
            capture_output=True, text=True,
        ).stdout
        for blk in res.split("\n\n"):
            for ln in blk.splitlines():
                if ln.startswith("worktree "):
                    p = Path(ln[len("worktree "):].strip())
                    if p.is_dir() and p not in seen:
                        seen.add(p)
                        out.append(p)
    return out


def display(p: Path) -> str:
    s = str(p)
    home = str(HOME)
    return "~/" + s[len(home) + 1:] if s.startswith(home + "/") else s


def handover_file(wt: Path) -> str:
    """Any HANDOVER*.md at the worktree root (case-insensitive); shows the name."""
    hands = sorted(
        f.name for f in wt.iterdir()
        if f.is_file() and re.match(r"(?i)^handover.*\.md$", f.name)
    )
    return ", ".join(hands) if hands else "—"


def main() -> int:
    if not REPOS.exists():
        print("missing repos.txt", file=sys.stderr)
        return 1
    HANDOVERS.mkdir(parents=True, exist_ok=True)

    rows = []
    for wt in worktrees_from(REPOS):
        branch = git(wt, "rev-parse", "--abbrev-ref", "HEAD") or "(detached)"
        rows.append((display(wt), repo_of(wt), branch, handover_file(wt)))
    # Preserve discovery order (repos.txt order, each repo followed by its
    # linked worktrees) — matches the index the manager already reads.

    lines = [
        "# Handover Index — all git worktrees → handover location",
        "",
        "> Auto-generated. Manager reads this to find any project's resumable state.",
        "",
        "| Worktree | Repo | Branch | Handover |",
        "|---|---|---|---|",
    ]
    for disp, repo, branch, hand in rows:
        lines.append(f"| `{disp}` | {repo} | {branch} | {hand} |")
    OUT.write_text("\n".join(lines) + "\n")

    n_hand = sum(1 for *_, h in rows if h != "—")
    print(f"handover index: {len(rows)} worktrees, {n_hand} with a handover -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
