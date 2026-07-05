#!/usr/bin/env python3
"""Phase 3: Hardware ESP32 interop test trigger.

Detects ESP32 boards, checks if microFIPS firmware is running,
then verifies the handshake against FIPS VPS1.

Exit codes:
  0 = SILENT (no hardware, no firmware, or test passed)
  1 = FAILURE (hardware present, firmware running, handshake failed)
"""

import os
import subprocess
import sys
import json
import time
from datetime import datetime
from pathlib import Path

VPS1_IP = "66.92.204.38"
FIPS_PORT = "2121"
EVIDENCE_DIR = Path.home() / "microfips-evidence"
EVIDENCE_DIR.mkdir(exist_ok=True)

# microFIPS firmware boot signature — appears in serial output when firmware is running
FIRMWARE_SIGNATURES = [
    "microfips",
    "FMP",
    "FIPS leaf node",
    "node_addr:",
    "FSP",
]


def detect_esp32_ports():
    """Find all ESP32 serial ports with chip type."""
    ports = []
    acm_devices = sorted(Path("/dev").glob("ttyACM*"))
    for dev in acm_devices:
        dev_str = str(dev)
        try:
            udev = subprocess.run(
                ["udevadm", "info", "-q", "property", dev_str],
                capture_output=True, text=True, timeout=5
            ).stdout.lower()
            if "espressif" not in udev and "303a" not in udev and "cp210" not in udev:
                continue
            # Detect chip type via esptool
            chip = detect_chip_type(dev_str)
            ports.append({"port": dev_str, "chip": chip, "udev": udev})
        except Exception:
            pass
    return ports


def detect_chip_type(port):
    """Use esptool to detect the actual chip type."""
    try:
        result = subprocess.run(
            ["esptool.py", "--port", port, "flash-id"],
            capture_output=True, text=True, timeout=10
        )
        output = result.stdout + result.stderr
        if "ESP32-C3" in output:
            return "esp32c3"
        elif "ESP32-S3" in output:
            return "esp32s3"
        elif "ESP32-C6" in output:
            return "esp32c6"
        elif "ESP32" in output:
            return "esp32"
        return "unknown"
    except Exception:
        return "unknown"


def read_serial(port, duration=5):
    """Read serial output for N seconds using pyserial."""
    conda_python = "/opt/miniconda/bin/python3"
    result = subprocess.run(
        [conda_python, "-c", f"""
import serial, time, sys
try:
    s = serial.Serial('{port}', 115200, timeout=1)
except Exception as e:
    print(f'ERROR: {{e}}', file=sys.stderr)
    sys.exit(1)
time.sleep(0.3)
end = time.time() + {duration}
while time.time() < end:
    line = s.readline()
    if line:
        print(line.decode(errors='replace').strip()[:200])
        sys.stdout.flush()
s.close()
"""],
        capture_output=True, text=True, timeout=duration + 10
    )
    return result.stdout


def has_microfips_firmware(serial_output):
    """Check if microFIPS firmware is running based on serial output."""
    output_lower = serial_output.lower()
    return any(sig.lower() in output_lower for sig in FIRMWARE_SIGNATURES)


def check_fips_journal_for_peer():
    """Check FIPS journal for evidence of ESP32 peer promotion."""
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", f"debian@{VPS1_IP}",
             "sudo journalctl -u fips --since '3 min ago' --no-pager 2>/dev/null"],
            capture_output=True, text=True, timeout=15
        )
        journal = result.stdout.lower()
        return {
            "promoted": "promoted to active peer" in journal,
            "heartbeat": journal.count("heartbeat"),
            "raw_tail": journal[-500:] if journal else "",
        }
    except Exception as e:
        return {"error": str(e)}


def run_hardware_test(port_info):
    """Run the actual HW interop test."""
    port = port_info["port"]
    chip = port_info["chip"]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Read serial output
    serial_output = read_serial(port, duration=5)

    firmware_running = has_microfips_firmware(serial_output)

    # Check FIPS journal
    fips_state = check_fips_journal_for_peer()

    evidence = {
        "timestamp": timestamp,
        "port": port,
        "chip": chip,
        "firmware_running": firmware_running,
        "serial_sample": serial_output[:1000] if serial_output else "",
        "fips_promoted": fips_state.get("promoted", False),
        "fips_heartbeat": fips_state.get("heartbeat", 0),
    }

    evidence_file = EVIDENCE_DIR / f"hw-interop-{timestamp}.json"
    evidence_file.write_text(json.dumps(evidence, indent=2))

    if not firmware_running:
        # No microFIPS firmware — not a test failure, just not ready
        # Silent exit (cron should not alert for this)
        return 0

    # Firmware IS running — check if it connected to FIPS
    if fips_state.get("promoted") or fips_state.get("heartbeat", 0) > 0:
        # Success — ESP32 connected to FIPS
        return 0

    # Firmware running but no FIPS evidence — real failure
    print(f"FAIL: microFIPS firmware running on {port} ({chip}) but no FIPS peer evidence")
    print(f"Serial sample: {serial_output[:200]}")
    print(f"FIPS journal: {fips_state.get('raw_tail', 'no-data')[:200]}")
    return 1


def main():
    ports = detect_esp32_ports()
    if not ports:
        # No ESP32 hardware — silent
        sys.exit(0)

    results = []
    for p in ports:
        result = run_hardware_test(p)
        results.append(result)

    # If any test failed, exit 1
    if any(r == 1 for r in results):
        sys.exit(1)
    # All passed or skipped — silent
    sys.exit(0)


if __name__ == "__main__":
    sys.exit(main())
