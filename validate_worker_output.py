#!/usr/bin/env python3
"""validate_worker_output — Checks worker output for obvious errors.

Stage 3 of cost-quality optimization: workers run this on their own output
before calling kanban_complete. If validation fails, the worker fixes the
error and retries locally (cheap) instead of escalating to the manager
(expensive).

Usage:
    echo "<worker_output>" | python3 validate_worker_output.py
    python3 validate_worker_output.py --file /path/to/output.py
    python3 validate_worker_output.py --code "<python code string>"

Exit codes:
    0 = PASS (output looks clean)
    1 = FAIL (syntax error, traceback, or parse error detected)

Output (JSON):
    {"passed": true/false, "errors": [...], "warnings": [...]}
"""
import ast
import json
import re
import sys
import os
from pathlib import Path


# Patterns that indicate worker output problems
TRACEBACK_PATTERN = re.compile(r"Traceback \(most recent call last\):", re.IGNORECASE)
SYNTAX_ERROR_PATTERN = re.compile(r"SyntaxError:|IndentationError:|TabError:", re.IGNORECASE)
JSON_ERROR_PATTERN = re.compile(r"json\.decoder\.JSONDecodeError|Expecting value|Expecting property", re.IGNORECASE)
IMPORT_ERROR_PATTERN = re.compile(r"ModuleNotFoundError|ImportError:", re.IGNORECASE)
RUNTIME_ERROR_PATTERN = re.compile(
    r"(NameError|TypeError|AttributeError|KeyError|IndexError|ValueError"
    r"|ZeroDivisionError|FileNotFoundError|PermissionError):",
    re.IGNORECASE,
)

# Extract code blocks from markdown-formatted output
CODE_BLOCK_PATTERN = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)


def extract_code_blocks(text: str) -> list[str]:
    """Extract Python code from markdown code blocks."""
    blocks = CODE_BLOCK_PATTERN.findall(text)
    if not blocks:
        # No code blocks — DON'T treat entire text as code if it contains
        # tracebacks or error output (would cause false syntax errors)
        stripped = text.strip()
        has_error_output = bool(TRACEBACK_PATTERN.search(text))
        if stripped and not stripped.startswith(("{", "[")) and not has_error_output:
            return [stripped]
    return blocks


def check_syntax(code: str) -> list[str]:
    """Check Python code for syntax errors using ast.parse."""
    errors = []
    try:
        ast.parse(code)
    except SyntaxError as e:
        errors.append(f"SyntaxError at line {e.lineno}: {e.msg}")
    return errors


def check_for_tracebacks(text: str) -> list[str]:
    """Check if output contains Python tracebacks (sign of a crash)."""
    errors = []
    if TRACEBACK_PATTERN.search(text):
        # Extract the last line of the traceback (the actual error)
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if TRACEBACK_PATTERN.search(line):
                # Look for the error line after the traceback
                for j in range(i + 1, min(i + 30, len(lines))):
                    if any(p.search(lines[j]) for p in [
                        SYNTAX_ERROR_PATTERN, RUNTIME_ERROR_PATTERN,
                        IMPORT_ERROR_PATTERN, JSON_ERROR_PATTERN
                    ]):
                        errors.append(f"Traceback detected: {lines[j].strip()}")
                        break
                else:
                    errors.append("Traceback detected (unable to extract error line)")
                break
    return errors


def check_json_validity(text: str) -> list[str]:
    """If output looks like JSON, verify it parses."""
    errors = []
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            json.loads(stripped)
        except json.JSONDecodeError as e:
            errors.append(f"JSON parse error: {e}")
    return errors


def validate(text: str) -> dict:
    """Run all validation checks on worker output.

    Returns {"passed": bool, "errors": list, "warnings": list}.
    """
    errors = []
    warnings = []

    # Check for tracebacks in the raw output
    errors.extend(check_for_tracebacks(text))

    # Check JSON validity if it looks like JSON
    errors.extend(check_json_validity(text))

    # Extract and check Python code blocks
    code_blocks = extract_code_blocks(text)
    for i, code in enumerate(code_blocks):
        syntax_errors = check_syntax(code)
        if syntax_errors:
            label = f" (code block {i+1})" if len(code_blocks) > 1 else ""
            for se in syntax_errors:
                errors.append(f"{se}{label}")

    # Warn if no code blocks and no JSON (might be incomplete)
    if not code_blocks and not text.strip().startswith(("{", "[")):
        if len(text.strip()) < 10:
            warnings.append("Output is very short — may be incomplete")

    return {
        "passed": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "code_blocks_found": len(code_blocks),
    }


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Validate worker output for errors")
    parser.add_argument("--file", help="Read output from file")
    parser.add_argument("--code", help="Validate a code string directly")
    args = parser.parse_args()

    if args.file:
        text = Path(args.file).read_text()
    elif args.code:
        text = args.code
    else:
        text = sys.stdin.read()

    result = validate(text)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
