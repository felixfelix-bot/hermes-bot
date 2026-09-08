#!/usr/bin/env python3
"""test_dq05_capacity.py — Phase 0 (t_224f750a) dq05-capacity.sh probe tests.

The probe is a zero-token pure-shell capacity check: "is DQ05 available and
underloaded enough to offload CPU-heavy work?" It outputs one JSON line:
reachable/load/free_ram/free_disk + source + decision (OFFLOAD-OK | LOCAL).

These tests drive the script through subprocess with:
  * a mocked PATH (fake `curl`, fake `ssh`) so LAN-down and ssh-fail paths are
    exercised hermetically — no real network, no real DQ05.
  * env overrides (DQ05_CAP_LOCAL_LOAD1, DQ05_CAP_LOCAL_CORES,
    DQ05_CAP_DQ05_CORES) so the local-machine and core-count inputs are
    deterministic regardless of the host the suite runs on.

Run: python3 -m pytest tests/test_dq05_capacity.py -v
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "scripts", "dq05-capacity.sh")

# A realistic resource-monitor /local payload (DQ05 proplus, N95).
HEALTHY_CURL_JSON = json.dumps({
    "hostname": "c03rad0r-DQ05proplus",
    "cpu": {"load_avg": [0.08, 0.06, 0.05], "cpu_pct": 2.1},
    "memory": {"total_gb": 10.9, "used_gb": 5.6, "available_gb": 5.3, "pct": 51.7},
    "swap": {"total_gb": 37.5, "used_gb": 3.1, "pct": 8.2},
    "disk": {"total_gb": 467, "used_gb": 266, "free_gb": 202, "pct": 56.8},
    "uptime_hours": 104.8,
    "timestamp": "2026-09-08T18:13:25Z",
})

# Schema-variant payload: different key names for the same facts.
VARIANT_CURL_JSON = json.dumps({
    "cpu": {"load1": 0.12, "cores": 4},
    "mem": {"avail_mb": 5500},
    "disk": {"free_mb": 200000},
})

BUSY_CURL_JSON = json.dumps({
    "cpu": {"load_avg": [7.9, 6.0, 3.0], "cpu_pct": 90.0},
    "memory": {"available_gb": 5.3},
    "disk": {"free_gb": 202},
})

LOWRAM_CURL_JSON = json.dumps({
    "cpu": {"load_avg": [0.1, 0.1, 0.1], "cpu_pct": 1.0},
    "memory": {"available_gb": 1.2},
    "disk": {"free_gb": 202},
})

# fake ssh stdout: LAN down but ssh up. LOAD + NPROC + MEM + DISK in one trip.
SSH_OK_OUTPUT = "LOAD=0.35 NPROC=4 MEMAVAIL_MB=5425 DISKFREE_MB=206592\n"


class Dq05CapacityProbeTest(unittest.TestCase):
    """Hermetic end-to-end tests of scripts/dq05-capacity.sh."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="dq05-cap-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._fake_env = {}

    # ── helpers ──────────────────────────────────────────────────────────

    def _fake_bin_dir(self, curl_script, ssh_script):
        """Write fake curl/ssh executables into a temp bin dir; return dir."""
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir, exist_ok=True)
        with open(os.path.join(bindir, "curl"), "w") as f:
            f.write("#!/usr/bin/env bash\n" + curl_script + "\n")
        os.chmod(os.path.join(bindir, "curl"), 0o755)
        with open(os.path.join(bindir, "ssh"), "w") as f:
            f.write("#!/usr/bin/env bash\n" + ssh_script + "\n")
        os.chmod(os.path.join(bindir, "ssh"), 0o755)
        # The probe uses `cat` on /proc/loadavg only for the LOCAL machine;
        # local load is injected via env instead, so real cat is fine.
        return bindir

    def _run(self, curl_script, ssh_script, extra_env=None, timeout=15):
        bindir = self._fake_bin_dir(curl_script, ssh_script)
        env = {
            "PATH": bindir + ":" + os.environ.get("PATH", "/usr/bin:/bin"),
            # deterministic machine inputs (bypass real /proc/loadavg/nproc)
            "DQ05_CAP_LOCAL_LOAD1": "19.04",
            "DQ05_CAP_LOCAL_CORES": "4",
            "DQ05_CAP_DQ05_CORES": "4",
            "DQ05_CAP_CURL_TIMEOUT": "1",
            "DQ05_CAP_SSH_TIMEOUT": "1",
        }
        for k, v in self._fake_env.items():
            env[k] = v
        if extra_env:
            env.update(extra_env)
        # HOME must be a real dir so ssh -o BatchMode doesn't trip on config.
        proc = subprocess.run(
            ["bash", SCRIPT],
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )
        self.assertEqual(
            proc.returncode, 0, f"script rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
        # Last non-empty line must be the JSON verdict.
        lines = [l for l in proc.stdout.splitlines() if l.strip()]
        return json.loads(lines[-1])

    # ── curl healthy ─────────────────────────────────────────────────────

    def test_curl_healthy_offload_ok(self):
        """LAN monitor up, DQ05 idle, local stressed, self-contained → OFFLOAD-OK."""
        curl = 'echo \'%s\'\nexit 0' % HEALTHY_CURL_JSON.replace("'", "'\\''")
        ssh = "echo 'ssh should not be called' >&2\nexit 42"
        out = self._run(
            curl,
            ssh,
            extra_env={"DQ05_CAP_SELF_CONTAINED": "1"},
        )
        self.assertTrue(out["reachable"])
        self.assertEqual(out["source"], "curl")
        self.assertAlmostEqual(out["load"], 0.08, places=2)
        self.assertGreaterEqual(out["free_ram_mb"], 2048)
        self.assertEqual(out["decision"], "OFFLOAD-OK")

    def test_curl_healthy_not_self_contained_local(self):
        """Same healthy state but caller did NOT mark task self-contained → LOCAL."""
        curl = 'echo \'%s\'\nexit 0' % HEALTHY_CURL_JSON.replace("'", "'\\''")
        ssh = "exit 42"
        out = self._run(curl, ssh)
        self.assertTrue(out["reachable"])
        self.assertEqual(out["decision"], "LOCAL")
        self.assertIn("self", out.get("reason", ""))

    # ── LAN down → ssh fallback ──────────────────────────────────────────

    def test_lan_down_ssh_up_fallback(self):
        """curl fails (LAN/resource-monitor down), ssh works → source=ssh."""
        curl = "echo 'curl: (7) Failed to connect' >&2\nexit 7"
        ssh = "echo '%s'\nexit 0" % SSH_OK_OUTPUT.strip()
        out = self._run(
            curl,
            ssh,
            extra_env={"DQ05_CAP_SELF_CONTAINED": "1"},
        )
        self.assertTrue(out["reachable"])
        self.assertEqual(out["source"], "ssh")
        self.assertAlmostEqual(out["load"], 0.35, places=2)
        self.assertGreaterEqual(out["free_ram_mb"], 2048)
        self.assertEqual(out["decision"], "OFFLOAD-OK")

    def test_lan_down_ssh_down_local(self):
        """Both curl and ssh fail → reachable=false, decision=LOCAL (fail-soft)."""
        curl = "echo 'curl: (7) Failed to connect' >&2\nexit 7"
        ssh = "echo 'ssh: connect to host dq05 port 22: Connection timed out' >&2\nexit 255"
        out = self._run(curl, ssh)
        self.assertFalse(out["reachable"])
        self.assertEqual(out["source"], "none")
        self.assertEqual(out["decision"], "LOCAL")

    # ── decision-rule branches (all must hold for OFFLOAD-OK) ───────────

    def test_local_idle_means_local(self):
        """Local machine underloaded (load/core <= 2.0) → LOCAL, never offload."""
        curl = 'echo \'%s\'\nexit 0' % HEALTHY_CURL_JSON.replace("'", "'\\''")
        ssh = "exit 42"
        env = self._fake_env
        out = self._run(
            curl,
            ssh,
            extra_env={"DQ05_CAP_SELF_CONTAINED": "1",
                       "DQ05_CAP_LOCAL_LOAD1": "0.5",
                       "DQ05_CAP_LOCAL_CORES": "4"},
        )
        self.assertTrue(out["reachable"])
        self.assertEqual(out["decision"], "LOCAL")
        self.assertIn("local", out.get("reason", ""))

    def test_dq05_busy_means_local(self):
        """DQ05 load/core >= 1.0 → LOCAL even if local stressed."""
        curl = 'echo \'%s\'\nexit 0' % BUSY_CURL_JSON.replace("'", "'\\''")
        ssh = "exit 42"
        out = self._run(
            curl,
            ssh,
            extra_env={"DQ05_CAP_SELF_CONTAINED": "1"},
        )
        self.assertTrue(out["reachable"])
        self.assertEqual(out["decision"], "LOCAL")
        self.assertIn("load", out.get("reason", ""))

    def test_dq05_low_ram_means_local(self):
        """DQ05 free RAM <= 2GB → LOCAL."""
        curl = 'echo \'%s\'\nexit 0' % LOWRAM_CURL_JSON.replace("'", "'\\''")
        ssh = "exit 42"
        out = self._run(
            curl,
            ssh,
            extra_env={"DQ05_CAP_SELF_CONTAINED": "1"},
        )
        self.assertTrue(out["reachable"])
        self.assertEqual(out["decision"], "LOCAL")
        self.assertIn("ram", out.get("reason", ""))

    # ── schema variance tolerance ────────────────────────────────────────

    def test_curl_schema_variant_tolerated(self):
        """resource-monitor JSON key variance is tolerated (load1/cores/mem/disk)."""
        curl = 'echo \'%s\'\nexit 0' % VARIANT_CURL_JSON.replace("'", "'\\''")
        ssh = "exit 42"
        out = self._run(
            curl,
            ssh,
            extra_env={"DQ05_CAP_SELF_CONTAINED": "1"},
        )
        self.assertTrue(out["reachable"])
        self.assertEqual(out["source"], "curl")
        self.assertAlmostEqual(out["load"], 0.12, places=2)
        self.assertEqual(out["free_ram_mb"], 5500)
        self.assertEqual(out["free_disk_mb"], 200000)
        self.assertEqual(out["decision"], "OFFLOAD-OK")

    def test_curl_garbage_falls_back_to_ssh(self):
        """:9100 returns non-JSON garbage → treated as down, ssh fallback used."""
        curl = "echo 'not json at all'\nexit 0"
        ssh = "echo '%s'\nexit 0" % SSH_OK_OUTPUT.strip()
        out = self._run(
            curl,
            ssh,
            extra_env={"DQ05_CAP_SELF_CONTAINED": "1"},
        )
        self.assertTrue(out["reachable"])
        self.assertEqual(out["source"], "ssh")


if __name__ == "__main__":
    unittest.main()
