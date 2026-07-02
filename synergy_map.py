#!/usr/bin/env python3
"""synergy_map — build a repo map + thematic clusters for the synergy watcher.

Reads each repo's llms.txt (else README) -> summary + keywords; clusters repos that
share themes (tollgate, market, cashu, mesh, esp32, nostr, cvm, nip, etc.). Output:
~/.hermes/bot/synergy_map.json. Internal-only (NO tokens, NO posting). The public
alert + PRana posting paths are BLOCKED on Signal + agent npub (see DECISIONS D-042).
"""
from __future__ import annotations
import json, re, subprocess, sys
from pathlib import Path

HOME = Path.home()
BOT = HOME / ".hermes" / "bot"
REPOS = BOT / "repos.txt"
OUT = BOT / "synergy_map.json"
THEMES = ["tollgate", "market", "cashu", "mesh", "esp32", "nostr", "cvm", "nip",
          "lightning", "bitcoin", "miner", "balloon", "radio", "lora", "ghdl", "hd",
          "soveng", "sec"]


def summary_of(repo: str) -> tuple[str, list[str]]:
    p = Path(repo)
    text = ""
    for f in ("llms.txt", "README.md", "readme.md"):
        fp = p / f
        if fp.exists():
            text = fp.read_text(errors="ignore")[:1500]
            break
    name = p.name
    line = next((l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#") and not l.startswith(">")), name)
    themes = [t for t in THEMES if t in (name + " " + text).lower()]
    return line[:200], themes


def main() -> int:
    if not REPOS.exists():
        print("missing repos.txt", file=sys.stderr); return 1
    repos = {}
    for line in REPOS.read_text().splitlines():
        r = line.strip()
        if not r or r.startswith("#") or not Path(r).is_dir():
            continue
        s, t = summary_of(r)
        repos[r] = {"name": Path(r).name, "summary": s, "themes": t}
    # cluster by theme
    clusters = {}
    for r, info in repos.items():
        for t in info["themes"]:
            clusters.setdefault(t, []).append(info["name"])
    # overlap candidates: repos sharing >=2 themes
    pairs = []
    names = list(repos)
    for i, a in enumerate(names):
        for b in names[i+1:]:
            shared = set(repos[a]["themes"]) & set(repos[b]["themes"])
            if len(shared) >= 2:
                pairs.append({"a": repos[a]["name"], "b": repos[b]["name"], "shared": sorted(shared)})
    OUT.write_text(json.dumps({"repos": repos, "theme_clusters": clusters, "overlap_candidates": pairs}, indent=2))
    print(f"synergy map: {len(repos)} repos, {len(clusters)} themes, {len(pairs)} overlap candidates -> {OUT}")
    for c, members in sorted(clusters.items(), key=lambda x: -len(x[1])):
        if len(members) > 1:
            print(f"  [{c}] {', '.join(members)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
