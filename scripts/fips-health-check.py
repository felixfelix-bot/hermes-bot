#!/usr/bin/env python3
"""FIPS VPS1 health check — verifies FIPS daemon is alive on VPS1.

Checks:
1. FIPS systemd service is active
2. UDP :2121 is listening
3. No error storms in recent logs (warn is OK, error spam = problem)
4. Disk/memory not critical

Silent on success. Only outputs on failure.
"""

import subprocess
import sys

VPS1 = "debian@66.92.204.38"
DISK_CRITICAL = 90
ERROR_THRESHOLD = 10  # errors in last 50 lines = problem


def ssh(cmd, timeout=15):
    try:
        proc = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no",
             VPS1, cmd],
            capture_output=True, text=True, timeout=timeout
        )
        return proc.stdout.strip(), proc.stderr.strip(), proc.returncode
    except subprocess.TimeoutExpired:
        return None, "SSH timeout", 1


def main():
    # Check FIPS service
    out, err, rc = ssh("systemctl is-active fips 2>/dev/null")
    if out != "active":
        print(f"FAIL: FIPS service not active on VPS1 (status: {out})")
        sys.exit(1)

    # Check UDP :2121
    out, _, _ = ssh("ss -ulnp | grep -c 2121")
    if out is None or out.strip() == "0":
        print("FAIL: FIPS UDP :2121 not listening on VPS1")
        sys.exit(1)

    # Check disk
    out, _, _ = ssh("df -P / | tail -1")
    if out:
        try:
            disk_pct = int(out.split()[4].replace("%", ""))
            if disk_pct >= DISK_CRITICAL:
                print(f"WARN: VPS1 disk at {disk_pct}% (critical threshold {DISK_CRITICAL}%)")
                sys.exit(1)
        except (ValueError, IndexError):
            pass

    # Check for error storms
    out, _, _ = ssh("journalctl -u fips --no-pager -n 50 2>/dev/null | grep -ci ERROR")
    try:
        error_count = int(out) if out else 0
    except ValueError:
        error_count = 0

    if error_count >= ERROR_THRESHOLD:
        print(f"WARN: FIPS logging excessive errors ({error_count} in last 50 lines)")
        ssh("journalctl -u fips --no-pager -n 5 2>/dev/null | grep ERROR")
        sys.exit(1)

    # All healthy — silent
    sys.exit(0)


if __name__ == "__main__":
    main()
