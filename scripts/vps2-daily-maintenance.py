#!/usr/bin/env python3
"""Daily VPS2 maintenance: flush stale swap, prune stopped containers, vacuum journal.

Runs daily at off-peak hours. Prevents the swap pressure / disk creep
that VPS2 experienced from accumulating non-nostr containers.
Silent on success.
"""

import subprocess
import sys
from pathlib import Path
from os import environ

VPS2_PASS = environ.get("VPS2_PASSWORD", "")
if not VPS2_PASS:
    env_path = Path.home() / "tollgate-infrastructure-kit" / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("VPS2_PASSWORD="):
                VPS2_PASS = line.split("=", 1)[1].strip().strip('"')
                break

SSH_BASE = ["sshpass", "-p", VPS2_PASS, "ssh",
            "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no",
            "debian@23.182.128.51"]


def ssh(cmd, timeout=120):
    try:
        proc = subprocess.run(SSH_BASE + [cmd], capture_output=True, text=True, timeout=timeout)
        return proc.stdout.strip(), proc.stderr.strip(), proc.returncode
    except subprocess.TimeoutExpired:
        return None, "timeout", 1


def main():
    issues = []

    # 1. Prune stopped containers + dangling images
    out, err, rc = ssh("docker container prune -f 2>/dev/null; docker image prune -f 2>/dev/null | tail -1")
    if rc != 0 and "timeout" in (err or ""):
        issues.append("docker prune timed out")

    # 2. Vacuum journal to 200M
    ssh("sudo journalctl --vacuum-size=200M 2>/dev/null | tail -1")

    # 3. Clean apt cache
    ssh("sudo apt-get clean 2>/dev/null")

    # 4. Flush stale swap pages
    out, _, _ = ssh("free -m | grep Swap")
    if out:
        parts = out.split()
        if len(parts) >= 3:
            try:
                swap_used = int(parts[2])
                if swap_used > 1000:  # >1GB swap used = flush
                    ssh("sudo swapoff -a && sudo swapon -a", timeout=300)
            except (ValueError, IndexError):
                pass

    # 5. Verify state after cleanup
    out, _, _ = ssh("df -P / | tail -1")
    disk_pct = 0
    if out:
        try:
            disk_pct = int(out.split()[4].replace("%", ""))
        except (ValueError, IndexError):
            pass

    out, _, _ = ssh("free -m | grep -E 'Swap'")
    swap_used_mb = 0
    if out:
        try:
            swap_used_mb = int(out.split()[2])
        except (ValueError, IndexError):
            pass

    if disk_pct >= 85:
        issues.append(f"disk still at {disk_pct}% after cleanup")
    if swap_used_mb > 2000:
        issues.append(f"swap still at {swap_used_mb}M after flush")

    if issues:
        for i in issues:
            print(f"VPS2 maintenance: {i}")
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
