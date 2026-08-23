#!/usr/bin/env python3
"""gen_llms_txt — generate a starter llms.txt for a repo (standard format).

Produces orientation/overview for any LLM. Curated-edit friendly. Usage:
  gen_llms_txt.py <repo>  > <repo>/llms.txt   # review, then commit
Reads: README, build markers, top source files (via git ls-files). No LLM tokens.
"""
from __future__ import annotations
import re, subprocess, sys
from pathlib import Path


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def _is_prose(line: str) -> bool:
    """True if a stripped README line is usable as a one-line project summary.

    Skips headings, list bullets, blockquotes, tables, markdown images/links,
    HTML tags, and badge/shield lines — these are the common sources of garbage
    summaries (e.g. a leading logo image, a Discord badge, an empty README)."""
    s = line.strip()
    if not s or len(s) < 3:
        return False
    if s.startswith(("#", "*", "-", "+", "<", ">", "|", "!", "[")):
        return False
    # image-only or link-only lines: e.g. `![alt](url)` / `[![alt](badge)](url)`
    if s.startswith(("![", "[![")) or (s.count("](") >= 1 and s.endswith(")")):
        return False
    return any(c.isalpha() for c in s)


def _trim_words(text: str, limit: int = 200) -> str:
    """Truncate to <=limit chars on a word boundary, with an ellipsis if cut."""
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit)
    if cut < limit // 2:  # no good boundary; hard cut
        cut = limit
    return text[:cut].rstrip() + "…"


_TAG = re.compile(r"<[^>]+>")


def _strip_tags(line: str) -> str:
    """Drop inline HTML tags (e.g. `<a href=...>t.me/x</a>` -> `t.me/x`), collapse spaces."""
    return re.sub(r"\s+", " ", _TAG.sub("", line)).strip()


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: gen_llms_txt.py <repo>", file=sys.stderr); return 1
    repo = Path(sys.argv[1]).resolve()
    name = repo.name
    # summary from first usable README line; never empty
    summary = f"{name} project."
    for rn in ("README.md", "README.org", "readme.md"):
        rp = repo / rn
        if rp.exists():
            lines = [l.strip() for l in rp.read_text(errors="ignore").splitlines() if l.strip()]
            para = next((l for l in lines if _is_prose(l)), "")
            summary = _trim_words(_strip_tags(para)) if para else f"{name} project."
            break
    # language / build hints
    markers = []
    for m, tag in [("Cargo.toml","Rust"),("go.mod","Go"),("package.json","npm/TS-JS"),
                   ("pyproject.toml","Python"),("setup.py","Python"),("platformio.ini","ESP32/PlatformIO"),
                   ("sdkconfig","ESP-IDF"),("CMakeLists.txt","CMake/C++"),("Makefile","Make")]:
        if (repo / m).exists():
            markers.append(tag)
    # top source dirs
    files = run(["git", "-C", str(repo), "ls-files"]).stdout.splitlines()
    exts = {}
    for f in files:
        if "." in f:
            e = f.rsplit(".", 1)[-1].lower()
            exts[e] = exts.get(e, 0) + 1
    top = sorted(exts.items(), key=lambda x: -x[1])[:6]

    out = []
    out.append(f"# {name}\n")
    out.append(f"> {summary}\n")
    out.append("Project orientation for LLMs (see https://llmstxt.org/). "
               "Cross-link: AGENTS.md (workflow rules), HANDOVER.md (session state).\n")
    if markers:
        out.append("## Stack\n")
        out.append("Languages/build: " + ", ".join(markers) + ".\n")
    if top:
        out.append("## Structure (top file types)\n")
        for e, n in top:
            out.append(f"- `.{e}`: {n} files")
        out.append("")
    out.append("## Build & test\n")
    out.append("- Inspect `Makefile` / `package.json` / `Cargo.toml` / `platformio.ini` for the canonical commands.\n")
    out.append("## Optional\n")
    out.append(f"- [README](./README.md): human readme")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
