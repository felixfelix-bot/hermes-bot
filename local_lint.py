#!/usr/bin/env python3
"""local_lint — auto-fix trivial code errors locally before cloud round-trip.

Saves API tokens by catching syntax/formatting issues that would otherwise
trigger a cloud correction loop. Detects language by extension, runs the
appropriate formatter/linter, reports what was fixed.

Usage:
  local_lint.py <file>          # lint + auto-fix one file
  local_lint.py <dir>           # lint all code files in directory
  local_lint.py --check <file>  # check only, don't modify (returns 1 if issues)

Exit codes: 0=clean, 1=had issues (fixed or not), 2=no linter available
"""
from __future__ import annotations
import os, subprocess, sys
from pathlib import Path

LINTERS = {
    ".py":     {"fix": ["black", "-q"], "check": ["python3", "-m", "py_compile"]},
    ".js":     {"fix": ["npx", "--yes", "prettier", "--write"], "check": ["npx", "--yes", "eslint"]},
    ".jsx":    {"fix": ["npx", "--yes", "prettier", "--write"]},
    ".ts":     {"fix": ["npx", "--yes", "prettier", "--write"]},
    ".tsx":    {"fix": ["npx", "--yes", "prettier", "--write"]},
    ".go":     {"fix": ["gofmt", "-w"], "check": ["go", "vet"]},
    ".rs":     {"fix": ["rustfmt", "--edition", "2021"]},
    ".c":      {"fix": ["clang-format", "-i"]},
    ".cpp":    {"fix": ["clang-format", "-i"]},
    ".h":      {"fix": ["clang-format", "-i"]},
    ".sh":     {"fix": ["shfmt", "-w"]},
    ".bash":   {"fix": ["shfmt", "-w"]},
    ".yml":    {"fix": ["npx", "--yes", "prettier", "--write"]},
    ".yaml":   {"fix": ["npx", "--yes", "prettier", "--write"]},
    ".json":   {"fix": ["npx", "--yes", "prettier", "--write"]},
}

CODE_EXTS = set(LINTERS.keys())


def has_tool(cmd: str) -> bool:
    """Check if a command is available."""
    return subprocess.run(["which", cmd], capture_output=True).returncode == 0


def lint_file(path: Path, check_only: bool = False) -> tuple[bool, str]:
    """Lint a single file. Returns (had_issues, message)."""
    ext = path.suffix.lower()
    config = LINTERS.get(ext)
    if not config:
        return False, f"no linter for {ext}"

    action = "check" if check_only and "check" in config else "fix"
    cmd = config.get(action)
    if not cmd:
        cmd = config.get("fix")
        if not cmd:
            return False, f"no {action} command for {ext}"

    # Check if the tool is available
    tool = cmd[0]
    if tool in ("npx",) and not has_tool("npx"):
        return False, "npx not available"
    if tool not in ("npx", "python3") and not has_tool(tool):
        return False, f"{tool} not installed"

    try:
        result = subprocess.run(
            cmd + [str(path)],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return False, "clean"
        else:
            stderr = result.stderr.strip()[:200]
            return True, f"{tool} found issues: {stderr}"
    except subprocess.TimeoutExpired:
        return False, f"{tool} timed out"
    except FileNotFoundError:
        return False, f"{tool} not found"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    check_only = "--check" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("No file/directory specified", file=sys.stderr)
        return 1

    target = Path(args[0])
    files = []

    if target.is_dir():
        for f in target.rglob("*"):
            if f.is_file() and f.suffix.lower() in CODE_EXTS:
                if ".git" not in f.parts and "node_modules" not in f.parts:
                    files.append(f)
    elif target.is_file():
        files.append(target)
    else:
        print(f"Path not found: {target}", file=sys.stderr)
        return 1

    issues_found = 0
    fixed = 0
    skipped = 0

    for f in files:
        had_issue, msg = lint_file(f, check_only)
        if had_issue:
            issues_found += 1
            if not check_only:
                fixed += 1
                print(f"  fixed: {f} ({msg})")
            else:
                print(f"  issue: {f} ({msg})")
        elif "not" in msg and "available" in msg:
            skipped += 1

    total = len(files)
    if issues_found == 0:
        if skipped > 0:
            print(f"{total} files checked, {total - skipped} clean, {skipped} skipped (no linter)")
        else:
            print(f"{total} files clean")
        return 0
    else:
        verb = "found" if check_only else "fixed"
        print(f"{total} files checked, {issues_found} issues {verb}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
