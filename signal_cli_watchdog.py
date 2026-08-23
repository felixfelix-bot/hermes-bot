#!/usr/bin/env python3
"""signal_cli_watchdog — detect and fix signal-cli crashes.

Checks if signal-cli is running and listening on port 8080. If not:
1. Kills any process that stole port 8080
2. Restarts signal-cli
3. Restarts the Hermes gateway (so it reconnects)
4. Sends an alert via Matrix (Signal may be down, so Matrix is the fallback)

Designed to run as a frequent cron job (every 2-5 minutes).
No LLM cost — pure system check, only creates an agent session if action needed.
"""
from __future__ import annotations
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

SIGNAL_CLI_PORT = 8080
SIGNAL_CLI_SERVICE = "signal-cli"
GATEWAY_SERVICE = "hermes-gateway"
HERMES_NOTIFY = str(Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "hermes")
STATE_FILE = Path.home() / ".hermes" / "bot" / "signal_watchdog_state.json"


def _is_port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except (ConnectionRefusedError, TimeoutError, OSError):
        return False


def _is_signal_cli_running() -> bool:
    result = subprocess.run(
        ["systemctl", "--user", "is-active", SIGNAL_CLI_SERVICE],
        capture_output=True, text=True, timeout=5,
    )
    return result.stdout.strip() == "active"


def _who_has_port(port: int) -> int | None:
    result = subprocess.run(
        ["ss", "-tlnp", f"sport = :{port}"],
        capture_output=True, text=True, timeout=5,
    )
    for line in result.stdout.splitlines():
        if f":{port}" in line and "pid=" in line:
            try:
                pid_str = line.split("pid=")[1].split(",")[0].split(")")[0]
                return int(pid_str)
            except (IndexError, ValueError):
                pass
    return None


def _restart_signal_cli() -> bool:
    try:
        subprocess.run(["systemctl", "--user", "restart", SIGNAL_CLI_SERVICE],
                       capture_output=True, timeout=30)
        time.sleep(8)
        return _is_signal_cli_running()
    except Exception:
        return False


def _restart_gateway():
    try:
        subprocess.run(["systemctl", "--user", "restart", GATEWAY_SERVICE],
                       capture_output=True, timeout=10)
    except Exception:
        pass


def _notify(message: str):
    print(message)
    # Try to send via Matrix (Signal may be down)
    try:
        subprocess.run(
            [HERMES_NOTIFY, "-q", "-p", "manager", "--platform", "matrix",
             "--chat", "!cqgmiHeQtATPAJwJZg:matrix.org", message],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        pass


def _load_state() -> dict:
    import json
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_state(state: dict):
    import json
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def check_and_fix() -> bool:
    """Returns True if signal-cli is healthy, False if action was taken."""
    state = _load_state()
    now = time.time()

    # Check 1: Is signal-cli service active?
    if not _is_signal_cli_running():
        _notify("⚠️ signal-cli watchdog: service is DOWN. Attempting restart...")
        if _restart_signal_cli():
            _notify("✅ signal-cli watchdog: service restarted successfully.")
            _restart_gateway()
            state["last_fix"] = now
            state["last_issue"] = "service_down"
            _save_state(state)
            return False
        else:
            _notify("❌ signal-cli watchdog: restart FAILED. Manual intervention needed.")
            state["last_fix"] = now
            state["last_issue"] = "restart_failed"
            _save_state(state)
            return False

    # Check 2: Is port 8080 actually signal-cli (not stolen)?
    if not _is_port_open(SIGNAL_CLI_PORT):
        _notify(f"⚠️ signal-cli watchdog: port {SIGNAL_CLI_PORT} not responding. Restarting...")
        if _restart_signal_cli():
            _notify("✅ signal-cli watchdog: port recovered after restart.")
            _restart_gateway()
            state["last_fix"] = now
            state["last_issue"] = "port_closed"
            _save_state(state)
            return False
        else:
            _notify("❌ signal-cli watchdog: port still down after restart.")
            return False

    # Check 3: Did something else steal the port?
    thief_pid = _who_has_port(SIGNAL_CLI_PORT)
    if thief_pid:
        # Check if it's actually our Java signal-cli process
        try:
            result = subprocess.run(
                ["ps", "-p", str(thief_pid), "-o", "comm="],
                capture_output=True, text=True, timeout=5,
            )
            process_name = result.stdout.strip()
            if "java" not in process_name.lower():
                _notify(f"⚠️ signal-cli watchdog: port {SIGNAL_CLI_PORT} stolen by "
                        f"{process_name} (PID {thief_pid}). Killing and restarting signal-cli.")
                subprocess.run(["kill", str(thief_pid)], capture_output=True, timeout=5)
                time.sleep(2)
                if _restart_signal_cli():
                    _notify("✅ signal-cli watchdog: port thief killed, signal-cli restarted.")
                    _restart_gateway()
                    state["last_fix"] = now
                    state["last_issue"] = f"port_stolen_by_{process_name}"
                    _save_state(state)
                    return False
        except Exception:
            pass

    return True  # All healthy


if __name__ == "__main__":
    healthy = check_and_fix()
    if healthy:
        print(f"[{time.strftime('%H:%M:%S')}] signal-cli: healthy")
    sys.exit(0 if healthy else 1)
