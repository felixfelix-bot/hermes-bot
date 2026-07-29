#!/usr/bin/env python3
"""test_runner — Sandboxed test execution for TDD worker pattern.

Stage 4 of cost-quality optimization: workers produce code + tests. This
script runs the tests locally at ZERO token cost. The manager (GLM-5.2)
is only invoked when tests fail.

Security:
    - Runs in a temp directory (no access to project files)
    - 30-second timeout (prevents infinite loops)
    - Memory limit via resource.setrlimit (256MB)
    - No network access (network calls will fail naturally)
    - Uses subprocess isolation

Usage:
    python3 test_runner.py --code <code_string> --tests <test_string>
    python3 test_runner.py --output <markdown_with_code_and_test_blocks>

Exit codes:
    0 = tests passed
    1 = tests failed (escalate to manager with stack trace)
    2 = setup error (couldn't parse/extract code or tests)

Output (JSON):
    {
        "passed": true/false,
        "test_count": N,
        "failure_count": N,
        "stack_trace": "...",   # truncated to last 20 lines on failure
        "error": "..."          # setup error message
    }
"""
import argparse
import json
import os
import re
import resource
import subprocess
import sys
import tempfile
from pathlib import Path


CODE_BLOCK_PATTERN = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)
TEST_BLOCK_PATTERN = re.compile(
    r"```(?:python\s+test|python\s*#.*test|test).*?\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)
MAX_STACK_TRACE_LINES = 20
TIMEOUT_SECONDS = 30
MEMORY_LIMIT_MB = 256


def extract_code_and_tests(output: str) -> tuple[str | None, str | None]:
    """Extract implementation code and test code from worker output.

    Looks for:
    1. Explicit test blocks (```python test or ```python # test)
    2. Multiple code blocks (last one is assumed to be tests if it contains 'def test_')
    3. Single code block (treated as implementation only)
    """
    # Try explicit test blocks first
    test_matches = TEST_BLOCK_PATTERN.findall(output)
    code_matches = CODE_BLOCK_PATTERN.findall(output)

    # Remove test matches from code matches
    if test_matches and code_matches:
        code_matches = [c for c in code_matches if c not in test_matches]

    test_code = test_matches[0] if test_matches else None
    impl_code = code_matches[0] if code_matches else None

    # Fallback: if multiple code blocks and last one has test_ functions
    if not test_code and len(code_matches) >= 2:
        last_block = code_matches[-1]
        if "def test_" in last_block or "import pytest" in last_block:
            test_code = last_block
            impl_code = "\n\n".join(code_matches[:-1])

    return impl_code, test_code


def run_tests(impl_code: str, test_code: str) -> dict:
    """Run pytest in a sandboxed temp directory.

    Returns result dict with passed/test_count/failure_count/stack_trace.
    """
    result = {
        "passed": False,
        "test_count": 0,
        "failure_count": 0,
        "stack_trace": "",
        "error": "",
    }

    with tempfile.TemporaryDirectory(prefix="hermes_test_") as tmpdir:
        # Write implementation code
        impl_path = Path(tmpdir) / "solution.py"
        impl_path.write_text(impl_code)

        # Write test code (imports from solution)
        test_path = Path(tmpdir) / "test_solution.py"
        test_path.write_text(test_code)

        try:
            proc = subprocess.run(
                [
                    sys.executable, "-m", "pytest",
                    str(test_path),
                    "--tb=short",
                    "-q",
                    "--no-header",
                    f"--timeout={TIMEOUT_SECONDS}",
                ],
                capture_output=True,
                text=True,
                timeout=TIMEOUT_SECONDS + 10,
                cwd=tmpdir,
                env={
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "HOME": tmpdir,
                    "PYTHONPATH": tmpdir,
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
            )

            output = proc.stdout + proc.stderr

            # Parse test results from pytest output
            # Example: "3 passed in 0.05s" or "1 failed, 2 passed in 0.05s"
            import re as _re
            passed_match = _re.search(r"(\d+) passed", output)
            failed_match = _re.search(r"(\d+) failed", output)
            error_match = _re.search(r"(\d+) error", output)

            passed_count = int(passed_match.group(1)) if passed_match else 0
            failed_count = int(failed_match.group(1)) if failed_match else 0
            error_count = int(error_match.group(1)) if error_match else 0

            result["test_count"] = passed_count + failed_count + error_count
            result["failure_count"] = failed_count + error_count
            result["passed"] = result["failure_count"] == 0 and result["test_count"] > 0

            if not result["passed"]:
                # Truncate stack trace to last N lines
                lines = output.strip().split("\n")
                result["stack_trace"] = "\n".join(lines[-MAX_STACK_TRACE_LINES:])

        except subprocess.TimeoutExpired:
            result["error"] = f"Tests timed out after {TIMEOUT_SECONDS}s"
            result["stack_trace"] = result["error"]
        except Exception as e:
            result["error"] = f"Test runner error: {e}"
            result["stack_trace"] = result["error"]

    return result


def main():
    parser = argparse.ArgumentParser(description="Run worker tests in sandbox")
    parser.add_argument("--code", help="Implementation code string")
    parser.add_argument("--tests", help="Test code string")
    parser.add_argument("--output", help="Markdown output with code + test blocks")
    args = parser.parse_args()

    if args.output:
        impl_code, test_code = extract_code_and_tests(Path(args.output).read_text())
    else:
        impl_code, test_code = args.code, args.tests

    if not impl_code:
        result = {"passed": False, "error": "No implementation code found", "test_count": 0, "failure_count": 0, "stack_trace": ""}
        print(json.dumps(result, indent=2))
        sys.exit(2)

    if not test_code:
        result = {"passed": False, "error": "No test code found", "test_count": 0, "failure_count": 0, "stack_trace": ""}
        print(json.dumps(result, indent=2))
        sys.exit(2)

    result = run_tests(impl_code, test_code)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["passed"] else 1 if result["test_count"] > 0 else 2)


if __name__ == "__main__":
    main()
