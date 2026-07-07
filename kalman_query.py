#!/usr/bin/env python3
"""Kalman convergence query tool — wraps local + remote endpoints into one CLI.

This is the canonical way for the manager agent (or any agent/tool) to check
Kalman convergence health. It abstracts away whether the data comes from the
local proxy DB or the remote ContextVM endpoint.

Usage:
    python3 kalman_query.py            # local health (recomputes from DB)
    python3 kalman_query.py local      # same as above
    python3 kalman_query.py remote     # query DQ05 ContextVM :9100/kalman
    python3 kalman_query.py all        # both local + remote, side by side
    python3 kalman_query.py --short    # one-line status block (local)
    python3 kalman_query.py --json     # raw JSON (local, machine-parseable)

Exit codes: 0 = healthy/improving, 1 = degraded/unhealthy, 2 = query error
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
import urllib.error

# ── Config ───────────────────────────────────────────────────────────────────

BOT_DIR = os.path.expanduser("~/.hermes/bot")
DQ05_KALMAN_URL = "http://100.90.22.201:9100/kalman"
DQ05_FULL_URL = "http://100.90.22.201:9100/"
QUERY_TIMEOUT = 10  # seconds

# ── Helpers ──────────────────────────────────────────────────────────────────

def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def query_local(short: bool = False) -> dict | str:
    """Run the local kalman_health.py and return its output."""
    import subprocess
    py = "/usr/bin/python3"
    cmd = [py, os.path.join(BOT_DIR, "kalman_health.py")]
    if short:
        cmd.append("--short")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode not in (0, 1):
            return {"error": f"kalman_health.py exited {r.returncode}", "stderr": r.stderr[:500]}
        if short:
            return r.stdout.strip() or "Kalman Convergence: ? no report"
        # Also grab the short line for display convenience
        r2 = subprocess.run(cmd + ["--short"], capture_output=True, text=True, timeout=30)
        result = json.loads(r.stdout) if r.stdout.strip() else {}
        result["short_line"] = r2.stdout.strip() or "Kalman Convergence: ? no report"
        return result
    except subprocess.TimeoutExpired:
        return {"error": "kalman_health.py timed out (30s)", "short_line": "Kalman Convergence: ? timeout"}
    except json.JSONDecodeError:
        return {"error": "kalman_health.py returned non-JSON", "raw": r.stdout[:500],
                "short_line": "Kalman Convergence: ? parse error"}
    except FileNotFoundError:
        return {"error": f"kalman_health.py not found at {BOT_DIR}",
                "short_line": "Kalman Convergence: ? not found"}


def query_remote(url: str = DQ05_KALMAN_URL) -> dict:
    """Query the DQ05 ContextVM endpoint for cached Kalman health data."""
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=QUERY_TIMEOUT) as r:
            data = json.loads(r.read().decode())
        if not data:
            return {"error": "empty response — no data pushed yet", "url": url}
        return data
    except urllib.error.URLError as e:
        return {"error": f"connection failed: {e.reason}", "url": url}
    except json.JSONDecodeError:
        return {"error": "invalid JSON from endpoint", "url": url}
    except Exception as e:
        return {"error": str(e), "url": url}


def format_all(local: dict | str, remote: dict) -> str:
    """Format both sources for side-by-side comparison."""
    lines = []
    lines.append(f"=== Kalman Convergence Health ({_utc_now()}) ===")
    lines.append("")

    # Local
    if isinstance(local, str):
        lines.append(f"[T470 local]  {local}")
    elif "error" in local:
        lines.append(f"[T470 local]  ERROR: {local['error']}")
    else:
        short = local.get("short_line", "?")
        lines.append(f"[T470 local]  {short}")

    # Remote
    if "error" in remote:
        lines.append(f"[DQ05 remote] ERROR: {remote['error']}")
    else:
        short = remote.get("short_line", "?")
        pushed = remote.get("pushed_at", 0)
        age = ""
        if pushed:
            import time
            age_min = int((time.time() - pushed) / 60)
            age = f" (pushed {age_min}m ago)"
        lines.append(f"[DQ05 remote] {short}{age}")

    # Agreement check
    local_ok = isinstance(local, dict) and "overall_verdict" in local
    remote_ok = "overall_verdict" in remote
    if local_ok and remote_ok:
        lv = local.get("overall_verdict", "?")
        rv = remote.get("overall_verdict", "?")
        if lv == rv:
            lines.append("")
            lines.append(f"✓ Both sources agree: {lv}")
        else:
            lines.append("")
            lines.append(f"⚠ Disagreement: local={lv}, remote={rv}")

    lines.append("")
    lines.append("Query commands:")
    lines.append(f"  Local:  python3 {os.path.join(BOT_DIR, 'kalman_health.py')} --short")
    lines.append(f"  Remote: curl -s {DQ05_KALMAN_URL} | python3 -m json.tool")

    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Query Kalman convergence health (local + remote ContextVM).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("source", nargs="?", default="local",
                   choices=["local", "remote", "all"],
                   help="Data source (default: local)")
    p.add_argument("--short", action="store_true",
                   help="One-line status block (local only, overrides --json)")
    p.add_argument("--json", action="store_true",
                   help="Raw JSON output (local only)")
    args = p.parse_args(argv)

    if args.short and args.source == "local":
        result = query_local(short=True)
        print(result)
        return 0

    if args.json and args.source == "local":
        result = query_local()
        if isinstance(result, dict):
            print(json.dumps(result, indent=2))
            return 0 if result.get("overall_verdict") in ("healthy", "improving") else 1
        print(json.dumps({"error": "unexpected type"}, indent=2))
        return 2

    if args.source == "local":
        result = query_local()
        if isinstance(result, dict) and "error" in result:
            print(f"Error: {result['error']}", file=sys.stderr)
            return 2
        if isinstance(result, dict):
            print(result.get("short_line", json.dumps(result, indent=2)))
            verdict = result.get("overall_verdict", "unknown")
            return 0 if verdict in ("healthy", "improving") else 1
        return 2

    if args.source == "remote":
        result = query_remote()
        if "error" in result:
            print(f"Error: {result['error']}", file=sys.stderr)
            return 2
        print(result.get("short_line", json.dumps(result, indent=2)))
        verdict = result.get("overall_verdict", "unknown")
        return 0 if verdict in ("healthy", "improving") else 1

    if args.source == "all":
        local = query_local()
        remote = query_remote()
        print(format_all(local, remote))
        # Exit based on worst case
        local_bad = isinstance(local, dict) and local.get("overall_verdict") not in ("healthy", "improving")
        remote_bad = "error" in remote or remote.get("overall_verdict") not in ("healthy", "improving")
        return 1 if (local_bad or remote_bad) else 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
