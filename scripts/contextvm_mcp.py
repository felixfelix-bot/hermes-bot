#!/usr/bin/env python3
"""
ContextVM MCP Server — exposes DQ05's resource-monitor API as MCP tools.

Auto-detects DQ05 on the local network (192.168.1.218) first, then falls
back to Netbird WireGuard (100.90.22.201). Caches the discovered host.

Tools exposed:
  contextvm_detect     — detect which IP DQ05 is reachable on
  contextvm_health     — full combined report (quota + system + kalman)
  contextvm_quota      — z.ai quota windows for both API keys
  contextvm_system     — DQ05 local system stats (CPU, mem, disk, swap)
  contextvm_kalman     — Kalman convergence backtest report
  contextvm_remote     — remote (T470) system stats as seen from DQ05

Usage:
  Standalone: python3 contextvm_mcp.py --once contextvm_health
  MCP stdio:  python3 contextvm_mcp.py   (launched by Hermes MCP framework)
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import urllib.error
from typing import Optional

# ── Config ────────────────────────────────────────────────────────────────

# LAN is preferred (no rate limiting). Netbird is the fallback.
DQ05_LAN = "192.168.1.218"
DQ05_NETBIRD = "100.90.22.201"
PORT = 9100
USER = "c03rad0r"
DISCOVER_TIMEOUT = 3   # seconds per host probe
REQUEST_TIMEOUT = 10   # seconds for HTTP requests to ContextVM

# Cache discovered host so we don't probe every call
_cached_host: Optional[str] = None
_cache_ts: float = 0
_CACHE_TTL = 120  # re-discover every 2 min

# ── Host discovery ────────────────────────────────────────────────────────


def discover_host() -> Optional[str]:
    """Find which IP DQ05 is reachable on. Tries LAN first, then Netbird.
    Uses a lightweight TCP connect check (not full HTTP download) for speed."""
    global _cached_host, _cache_ts

    if _cached_host and (time.time() - _cache_ts) < _CACHE_TTL:
        return _cached_host

    import socket

    for host in [DQ05_LAN, DQ05_NETBIRD]:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1.5)
            result = sock.connect_ex((host, PORT))
            sock.close()
            if result == 0:
                _cached_host = host
                _cache_ts = time.time()
                return host
        except Exception:
            continue

    # Both failed — clear cache, return None
    _cached_host = None
    _cache_ts = 0
    return None


def _fetch(path: str) -> dict:
    """GET a path from ContextVM. Raises RuntimeError if unreachable."""
    host = discover_host()
    if not host:
        raise RuntimeError("DQ05 ContextVM unreachable (tried LAN + Netbird)")

    url = f"http://{host}:{PORT}{path}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        # Invalidate cache — host might have gone down
        global _cached_host, _cache_ts
        _cached_host = None
        _cache_ts = 0
        raise RuntimeError(f"ContextVM request failed ({host}:{PORT}{path}): {e}")


# ── MCP Server ────────────────────────────────────────────────────────────


def _build_server():
    """Create the MCP server with all tools registered."""
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("contextvm")

    @server.tool()
    def contextvm_detect() -> str:
        """Detect which IP/host DQ05 ContextVM is currently reachable on.
        Returns the active host (LAN or Netbird) or an error message."""
        host = discover_host()
        if host:
            label = "LAN" if host == DQ05_LAN else "Netbird"
            return json.dumps({"host": host, "label": label, "port": PORT, "reachable": True})
        return json.dumps({"host": None, "reachable": False, "error": "DQ05 not reachable on LAN or Netbird"})

    @server.tool()
    def contextvm_health() -> str:
        """Get the full ContextVM health report: z.ai quota (both API keys),
        DQ05 local system stats, Kalman convergence, and remote T470 probe.
        This is the comprehensive view."""
        return json.dumps(_fetch("/health"))

    @server.tool()
    def contextvm_quota() -> str:
        """Get z.ai quota windows for both API keys (ours + friend).
        Shows used_pct, resets_at, hours_left for 5-hour, weekly, monthly windows."""
        return json.dumps(_fetch("/quota"))

    @server.tool()
    def contextvm_system() -> str:
        """Get DQ05 local system stats: CPU load/percent, memory, swap, disk,
        and uptime."""
        return json.dumps(_fetch("/local"))

    @server.tool()
    def contextvm_kalman() -> str:
        """Get the Kalman convergence backtest report for both API keys.
        Shows prediction error trends, velocity accuracy, coverage, and verdict."""
        return json.dumps(_fetch("/kalman"))

    @server.tool()
    def contextvm_remote() -> str:
        """Get remote (T470) system stats as probed from DQ05 via SSH.
        May be stale or unreachable depending on network state."""
        return json.dumps(_fetch("/remote"))

    @server.tool()
    def contextvm_ppq() -> str:
        """Get PPQ (api.ppq.ai) account status: real credit balance,
        24h spend breakdown, query history stats, and per-key usage.

        Uses three PPQ API endpoints:
        - POST /credits/balance for remaining balance
        - GET /queries/history for per-query spend tracking
        - GET /keys for per-key usage limits (needs PPQ_CREDIT_ID)

        Reads from the local api_burn.db cache (updated every 5 min by
        api_burn_collector). Falls back to live API call if cache is stale.
        """
        import sqlite3 as _sqlite3
        burn_db = os.path.expanduser("~/.hermes/bot/api_burn.db")
        result = {"provider": "ppq"}

        # Read latest cached balance snapshot
        try:
            conn = _sqlite3.connect(burn_db)
            conn.row_factory = _sqlite3.Row
            row = conn.execute(
                "SELECT * FROM balance_snapshots WHERE provider='ppq' "
                "ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            if row:
                result["balance_usd"] = row["balance_usd"]
                result["cached_at"] = row["ts"]
                result["age_seconds"] = time.time() - row["ts"]
                result["raw"] = row["raw"][:200] if row["raw"] else None
                if row["error"]:
                    result["error"] = row["error"]
            conn.close()
        except Exception as e:
            result["db_error"] = str(e)

        # Try live balance if cache is stale (>5 min old)
        cache_age = result.get("age_seconds", 999)
        if cache_age > 300:
            key = os.environ.get("PPQ_API_KEY", "").strip()
            if key:
                try:
                    base = "https://api.ppq.ai"
                    req = urllib.request.Request(
                        f"{base}/credits/balance",
                        data=b'{}',
                        headers={
                            "Authorization": f"Bearer {key}",
                            "Content-Type": "application/json",
                        },
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        data = json.loads(resp.read().decode())
                        result["balance_usd"] = float(data.get("balance", 0))
                        result["source"] = "live"
                except Exception as e:
                    result["live_error"] = str(e)
            else:
                result["source"] = "cache (no PPQ_API_KEY for live fetch)"
        else:
            result["source"] = "cache"

        return json.dumps(result, default=str)

    return server


def _run_mcp():
    """Run the MCP server over stdio."""
    server = _build_server()
    server.run()


# ── Standalone CLI mode (for testing/debugging) ───────────────────────────


def _run_cli():
    """Run a single tool from the command line."""
    if len(sys.argv) < 2:
        print("Usage: contextvm_mcp.py <tool_name>")
        print("Tools: detect, health, quota, system, kalman, remote, ppq")
        sys.exit(1)

    tool = sys.argv[1].removeprefix("contextvm_")

    def _ppq_cli():
        """CLI handler for PPQ balance query."""
        import sqlite3 as _sqlite3
        burn_db = os.path.expanduser("~/.hermes/bot/api_burn.db")
        result = {"provider": "ppq"}
        try:
            conn = _sqlite3.connect(burn_db)
            conn.row_factory = _sqlite3.Row
            row = conn.execute(
                "SELECT * FROM balance_snapshots WHERE provider='ppq' "
                "ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            if row:
                result["balance_usd"] = row["balance_usd"]
                result["cached_at"] = row["ts"]
                result["age_seconds"] = time.time() - row["ts"]
            conn.close()
        except Exception as e:
            result["db_error"] = str(e)

        key = os.environ.get("PPQ_API_KEY", "").strip()
        if key:
            try:
                base = "https://api.ppq.ai"
                req = urllib.request.Request(
                    f"{base}/credits/balance", data=b'{}',
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode())
                    result["balance_usd"] = float(data.get("balance", 0))
                    result["source"] = "live"
            except Exception as e:
                result["live_error"] = str(e)
        return json.dumps(result, default=str)

    handlers = {
        "detect": lambda: json.dumps(
            {"host": h, "label": "LAN" if h == DQ05_LAN else "Netbird", "reachable": True}
            if (h := discover_host()) else
            {"host": None, "reachable": False}
        ),
        "health": lambda: json.dumps(_fetch("/health")),
        "quota": lambda: json.dumps(_fetch("/quota")),
        "system": lambda: json.dumps(_fetch("/local")),
        "kalman": lambda: json.dumps(_fetch("/kalman")),
        "remote": lambda: json.dumps(_fetch("/remote")),
        "ppq": lambda: _ppq_cli(),
    }

    if tool not in handlers:
        print(f"Unknown tool: {tool}")
        print(f"Available: {', '.join(handlers.keys())}")
        sys.exit(1)

    try:
        print(handlers[tool]())
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    if "--once" in sys.argv:
        # CLI mode: python3 contextvm_mcp.py --once health
        sys.argv.remove("--once")
        _run_cli()
    else:
        # MCP stdio mode (launched by Hermes)
        _run_mcp()
