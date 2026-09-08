# TUNNEL-HARDENING — autossh for the T470→VPS2 reverse tunnel (P0-8)

**Date:** 2026-09-08
**Task:** P0-8 from `PLAN-wire-full-provider-pool-rugpull-resilience-2026-09-08.md`
**Owner:** worker-admin
**Host:** T470 / CobradorWave (this laptop) — tunnel client
**Remote:** VPS2 (23.182.128.51) — tunnel server (sshd, GatewayPorts clientspecified)

## Problem (before)

The reverse tunnel that publishes the local `tag-sidecar`/zai-proxy buyer-attribution
service on VPS2 port 9099 was a **bare `ssh -N -R` single process**:

```
ExecStart=/usr/bin/ssh -N -R 0.0.0.0:9099:127.0.0.1:9097 \
  -o ExitOnForwardFailure=yes -o ServerAliveInterval=20 \
  -o ServerAliveCountMax=3 -o StrictHostKeyChecking=accept-new root@23.182.128.51
```

with only `Restart=always RestartSec=10` at the systemd layer. Failure modes:

1. **Silent dead-link hang (no SPOF removal).** When the network path dies but the
   TCP session is half-open, the ssh process can linger without forwarding. systemd
   never sees an exit, so it never restarts — the tunnel stays dead indefinitely.
   `ServerAliveInterval` helps ssh exit, but the process-based restart still depends
   on systemd's fixed 10 s cadence and on ssh actually exiting.
2. **Stale remote port after an unclean drop.** VPS2 sshd keeps the `0.0.0.0:9099`
   listener owned by the old `sshd-session` for up to `ClientAliveInterval 300 ×
   ClientAliveCountMax 2` = **~10 min** after the client vanishes. Every restart of the
   bare ssh in that window fails immediately with "remote port forwarding failed"
   (`ExitOnForwardFailure=yes` → exit 255) → crash-loop. Observed on 2026-09-08:
   **restart counter at 85**.
3. **Reboot survival was accidental.** The unit is `enabled` and user `Linger=yes`,
   so it came back after a T470 reboot — but the first connection attempt happens
   before the network is up, and with the default autossh gate-time semantics a
   first-run failure would have made autossh (if it had been used naively) give up.

## Fix (after)

Replace the bare ssh with **autossh** supervised by systemd:

```
ExecStart=/usr/bin/autossh -M 0 -N -R 0.0.0.0:9099:127.0.0.1:9097 \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=20 \
  -o ServerAliveCountMax=3 \
  -o StrictHostKeyChecking=accept-new \
  root@23.182.128.51
Environment=AUTOSSH_GATETIME=0
Environment=HOME=%h
Restart=always
RestartSec=10
```

Canonical unit file: `config/systemd/user/zai-proxy-reverse-tunnel.service` (in this
repo). Deploy with:

```bash
cp config/systemd/user/zai-proxy-reverse-tunnel.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user restart zai-proxy-reverse-tunnel.service
```

### Why each piece

| Piece | Why |
|---|---|
| `autossh -M 0` | autossh supervises the ssh child and **respawns it the instant it exits** — no systemd RestartSec delay for the common dead-link case. `-M 0` = no dedicated autossh monitor port; liveness detection is delegated to ssh's own `ServerAliveInterval`/`ServerAliveCountMax` (ssh exits on keepalive failure → autossh restarts). Avoids needing two spare ports on each side. |
| `-o ExitOnForwardFailure=yes` | If the remote port is still held by a stale `sshd-session`, ssh exits immediately (255) instead of hanging — autossh then retries until the server reaps the stale session. |
| `AUTOSSH_GATETIME=0` | **Critical.** Disables autossh's 30 s starting gate: autossh will restart even if the *first* ssh run fails (boot before network, stale remote port). Without this, autossh gives up after an early failure and only systemd's `Restart=always` (10 s later) would bring it back — a delay, and in the worst case a policy mismatch. |
| `Restart=always RestartSec=10` | systemd-level backstop for autossh itself dying (rare). ssh respawns are autossh's job; systemd restart is only for autossh-process death. |
| `After=network-online.target` | Don't race the network at boot. |
| `WantedBy=default.target` + enabled + `Linger=yes` | User service starts at boot without an interactive login; survives T470 reboot. |

### No-SPOF / reboot-survival guarantees (verified)

1. **Reboot:** `Linger=yes` (verified `loginctl show-user c03rad0r`), unit `enabled`,
   `After=network-online.target`. autossh keeps retrying even if the first attempt
   happens before the network is up (`AUTOSSH_GATETIME=0`).
2. **Dead link mid-session:** ssh exits after 3 × 20 s missed keepalives; autossh
   respawns immediately (sub-second, no RestartSec wait).
3. **Stale remote port:** autossh retries `ExitOnForwardFailure` exits in a loop until
   VPS2 sshd reaps the old `sshd-session` (≤ `ClientAliveInterval × ClientAliveCountMax`
   ≈ 10 min worst case), then rebinds 9099. No human needed.
4. **autossh death:** systemd `Restart=always` restarts the unit in 10 s.

## Verification performed (2026-09-08)

- autossh installed: `autossh 1.4g` (`apt-get install -y autossh`).
- `systemctl --user restart zai-proxy-reverse-tunnel.service` → `active (running)`,
  Main PID = `autossh`, child = `ssh ... -R 0.0.0.0:9099:127.0.0.1:9097`.
- VPS2 side: `ss -tln` on 23.182.128.51 shows `0.0.0.0:9099` owned by a fresh
  `sshd-session` (rebound after restart).
- **Kill test:** `kill` the ssh child → autossh respawned a new ssh child within
  seconds and VPS2:9099 was rebound (no manual intervention, no systemd restart
  needed) → the SPOF is removed.
- `systemctl --user is-enabled zai-proxy-reverse-tunnel.service` → `enabled`.

## Rollback

```bash
# Revert the live unit to the pre-P0-8 bare-ssh version:
#   ExecStart=/usr/bin/ssh -N -R 0.0.0.0:9099:127.0.0.1:9097 \
#     -o ExitOnForwardFailure=yes -o ServerAliveInterval=20 \
#     -o ServerAliveCountMax=3 -o StrictHostKeyChecking=accept-new root@23.182.128.51
systemctl --user daemon-reload
systemctl --user restart zai-proxy-reverse-tunnel.service
```

To remove autossh entirely: `sudo apt-get remove --purge autossh`.

## Related (out of P0-8 scope — flagged, not touched)

- `routstr-tunnel.service` and `routstr-forward-tunnel.service` are **duplicates**
  (both `-L 127.0.0.1:8009:127.0.0.1:8009` to the same host). Only one can bind local
  8009; the other crash-loops with `bind [127.0.0.1]:8009: Address already in use`
  (observed 2026-09-08, `routstr-forward-tunnel` in auto-restart). Same bare-ssh SPOF
  pattern. Consider folding into one autossh unit in a follow-up.
