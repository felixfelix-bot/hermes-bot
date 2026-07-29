#!/usr/bin/env python3
"""format_review_prompt — Build a truncated review prompt for manager escalation.

Stage 5 of cost-quality optimization: when a worker task fails and escalates
to the manager (GLM-5.2), the manager should receive ONLY:
  - The original task spec
  - The worker's final artifact
  - The error/stack trace

NOT the full worker conversation history. This avoids the "double-spend trap"
where the manager reprocesses all the worker's intermediate reasoning.

Usage:
    python3 format_review_prompt.py \
        --task "Write a function that..." \
        --artifact "def broken(..." \
        --error "assert -1 == 3" \
        [--max-tokens 2000]

Output: formatted review prompt (string) to send to the manager.
"""
import argparse
import sys
from pathlib import Path


MAX_TOKENS_DEFAULT = 2000
CHARS_PER_TOKEN = 4  # rough estimate


def truncate(text: str, max_chars: int) -> str:
    """Truncate text to max_chars, adding truncation notice if needed."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n[... truncated, see full output at the worker session]"


def build_review_prompt(
    task_spec: str,
    artifact: str,
    error: str,
    max_tokens: int = MAX_TOKENS_DEFAULT,
) -> str:
    """Build a truncated review prompt for the manager.

    Caps total prompt at max_tokens * CHARS_PER_TOKEN characters.
    """
    max_chars = max_tokens * CHARS_PER_TOKEN

    # Reserve space for the template structure (~500 chars)
    content_budget = max_chars - 500
    if content_budget < 200:
        content_budget = 200

    # Split budget: 20% task, 50% artifact, 30% error
    task_budget = int(content_budget * 0.2)
    artifact_budget = int(content_budget * 0.5)
    error_budget = int(content_budget * 0.3)

    task_spec = truncate(task_spec.strip(), task_budget)
    artifact = truncate(artifact.strip(), artifact_budget)
    error = truncate(error.strip(), error_budget)

    prompt = f"""## Worker Task Escalation

A worker ({"`glm-4.5-flash`"}) attempted this task but failed. Review the output and fix it.

### Original Task
{task_spec}

### Worker Output (artifact only)
```
{artifact}
```

### Error / Test Failure
```
{error}
```

### Instructions
1. Identify the bug in the worker output
2. Output ONLY the corrected code (no explanations)
3. If the task spec is ambiguous, note what needs clarification
"""
    return prompt


def main():
    parser = argparse.ArgumentParser(description="Build truncated manager review prompt")
    parser.add_argument("--task", required=True, help="Original task specification")
    parser.add_argument("--artifact", required=True, help="Worker's final output/artifact")
    parser.add_argument("--error", required=True, help="Error message or stack trace")
    parser.add_argument("--max-tokens", type=int, default=MAX_TOKENS_DEFAULT,
                        help=f"Max tokens for review prompt (default: {MAX_TOKENS_DEFAULT})")
    args = parser.parse_args()

    # Read from files if arguments are file paths
    task = Path(args.task).read_text() if Path(args.task).exists() else args.task
    artifact = Path(args.artifact).read_text() if Path(args.artifact).exists() else args.artifact
    error = Path(args.error).read_text() if Path(args.error).exists() else args.error

    prompt = build_review_prompt(task, artifact, error, args.max_tokens)
    print(prompt)

    # Log token estimate to stderr
    est_tokens = len(prompt) // CHARS_PER_TOKEN
    print(f"\n[Estimated {est_tokens} tokens]", file=sys.stderr)


if __name__ == "__main__":
    main()
