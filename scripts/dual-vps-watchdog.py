#!/usr/bin/env python3
"""Dual-VPS health watchdog — monitors nostr infrastructure on BOTH machines.

VPS1 (66.92.204.38) = primary nostr + FIPS (key auth)
VPS2 (23.182.128.51) = nostr + mints + jitsi (password auth)

Checks per VPS:
1. SSH reachable
2. All tollgate-* containers running (none stopped)
3. Disk usage below threshold
4. Swap usage below threshold (VPS2 is the known problem)
5. Memory available

Silent when healthy. Only outputs on failure (watchdog pattern).
State file: ~/.local/state/vps-dual-watchdog/alert.json
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

VPS1 = {
    "name": "VPS1",
    "host": "66.92.204.38",
    "user": "debian",
    "auth": "key",  # SSH key auth
}

VPS2_PASS = os.environ.get("VPS2_PASSWORD", "")
if not VPS2_PASS:
    # Try loading from .env
    env_path = Path.home() / "tollgate-infrastructure-kit" / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("VPS2_PASSWORD="):
                VPS2_PASS = line.split("=", 1)[1].strip().strip('"')
                break

VPS2 = {
    "name": "VPS2",
    "host": "23.182.128.51",
    "user": "debian",
    "auth": "password",
    "password": VPS2_PASS,
}

DISK_CRITICAL = 90
DISK_WARNING = 80
SWAP_CRITICAL_PCT = 70   # swap used % of total swap
SWAP_CRITICAL_ABS_MB = 3000  # >3GB swap used = pressure
MEM_AVAILABLE_MIN_MB = 500

STATE_DIR = Path.home() / ".local" / "state" / "vps-dual-watchdog"
ALERT_FILE = STATE_DIR / "alert.json"


def ssh_vps(vps, command, timeout=15):
    ssh_cmd = ["ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no"]
    if vps["auth"] == "password" and vps.get("password"):
        ssh_cmd = ["sshpass", "-p", vps["password"]] + ssh_cmd
    ssh_cmd.append(f"{vps['user']}@{vps['host']}")
    ssh_cmd.append(command)
    try:
        proc = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=timeout)
        return proc.stdout.strip(), proc.stderr.strip(), proc.returncode
    except subprocess.TimeoutExpired:
        return None, "SSH timeout", 1
    except FileNotFoundError:
        return None, f"sshpass not found (needed for {vps['name']})", 1
    except Exception as e:
        return None, str(e), 1


def check_vps(vps):
    failures = []
    name = vps["name"]

    # 1. SSH reachable + containers
    out, err, rc = ssh_vps(vps, "docker ps -a --format '{{.Names}}\t{{.State}}'")
    if out is None:
        failures.append(f"{name}: SSH failed — {err}")
        return failures

    stopped = []
    running_tollgate = 0
    for line in out.strip().split("\n"):
        if "\t" not in line:
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        cname, cstate = parts[0], parts[1]
        if cname.startswith("tollgate-"):
            if cstate != "running":
                stopped.append(f"{cname} ({cstate})")
            else:
                running_tollgate += 1

    if stopped:
        failures.append(f"{name}: {len(stopped)} stopped tollgate containers: {', '.join(stopped[:5])}")

    # 2. Disk
    out, _, _ = ssh_vps(vps, "df -P / | tail -1")
    if out:
        try:
            parts = out.split()
            disk_pct = int(parts[4].replace("%", ""))
            disk_free_gb = int(parts[3]) / 1024 / 1024
            if disk_pct >= DISK_CRITICAL:
                failures.append(f"{name}: disk {disk_pct}% CRITICAL ({disk_free_gb:.0f}G used)")
            elif disk_pct >= DISK_WARNING:
                failures.append(f"{name}: disk {disk_pct}% WARNING ({disk_free_gb:.0f}G used)")
        except (ValueError, IndexError):
            pass

    # 3. Memory + Swap
    out, _, _ = ssh_vps(vps, "free -m")
    if out:
        lines = out.strip().split("\n")
        for line in lines:
            if line.startswith("Swap:"):
                parts = line.split()
                if len(parts) >= 4:
                    try:
                        swap_total = int(parts[1])
                        swap_used = int(parts[2])
                        if swap_total > 0:
                            swap_pct = int(swap_used / swap_total * 100)
                            if swap_used > SWAP_CRITICAL_ABS_MB or swap_pct > SWAP_CRITICAL_PCT:
                                failures.append(
                                    f"{name}: swap {swap_used}M/{swap_total}M used ({swap_pct}%) — pressure"
                                )
                    except (ValueError, IndexError):
                        pass
            elif line.startswith("Mem:"):
                parts = line.split()
                if len(parts) >= 7:
                    try:
                        mem_avail = int(parts[6])
                        if mem_avail < MEM_AVAILABLE_MIN_MB:
                            failures.append(f"{name}: only {mem_avail}M memory available")
                    except (ValueError, IndexError):
                        pass

    # 4. FIPS health (VPS1 only)
    if name == "VPS1":
        out, _, _ = ssh_vps(vps, "systemctl is-active fips 2>/dev/null")
        if out and out.strip() != "active":
            failures.append(f"{name}: FIPS service not active (status: {out})")

    return failures


def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    all_failures = []
    for vps in [VPS1, VPS2]:
        failures = check_vps(vps)
        all_failures.extend(failures)

    if all_failures:
        alert = {
            "alert_time": datetime.now(timezone.utc).isoformat(),
            "failures": all_failures,
        }
        ALERT_FILE.write_text(json.dumps(alert, indent=2))
        for f in all_failures:
            print(f)
        sys.exit(1)
    else:
        ALERT_FILE.unlink(missing_ok=True)
        sys.exit(0)


if __name__ == "__main__":
    main()
