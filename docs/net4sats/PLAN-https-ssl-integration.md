# PLAN: HTTPS/SSL Self-Signed Cert Integration

## Problem
Captive portal loaded from router's IP without SSL can't access phone camera (browser security restriction). Self-signed certs for `net4sats.lan` / `net4sats.local` are needed.

## Current State
HTTPS work is **split across two branches** — partial work merged, critical piece unmerged.

### Merged (upstream main, OpenTollGate/tollgate-module-basic-go)
| Component | Status |
|---|---|
| Self-signed cert generation in Go (`src/cmd/tollgate-cli/ssl.go`) | ✅ Merged in PRs #123/#142 |
| SAN support for `net4sats.lan`, `net4sats.local` | ✅ Merged |
| CORS accept `.lan`/`.local` origins (commit `d8d9bb1`) | ✅ Merged — fixes TG003 |
| `tollgate-apply-ssl` as Go CLI wrapper | ✅ Merged |
| NoDogSplash port 443 + WAN firewall rule | ✅ Merged |
| TLS 1.2 transport hardening | ✅ Merged |

### NOT Merged (Amperstrand fork `feat/https-reverse-proxy` branch, PR #121 — CLOSED)
| Component | Status |
|---|---|
| Go-native TLS termination on port 443 | ❌ Needs rebase + merge |
| Reverse proxy: LuCI at `127.0.0.1:8080` | ❌ Needs rebase + merge |
| `uhttpd` bound to localhost only (not public) | ❌ Needs rebase + merge |
| `tollgate-apply-ssl` rewritten for Go TLS | ❌ Needs rebase + merge |
| `setup_self_signed_cert()` in `99-tollgate-setup` | ❌ Needs rebase + merge |

## Decision: Option B — Redirect from NDS Portal to HTTPS Billing Page

The camera (QR scanner) is only needed for one action: scanning a Cashu token to pay.
The initial captive portal page (SSID selection, pricing) doesn't need a camera.

Flow:
1. Phone connects to net4sats-portal WiFi → NDS redirects to HTTP portal (port 2050)
2. User selects data plan → taps "Scan QR to Pay"
3. → Redirected to https://net4sats.lan/pay (Go TLS :443, camera works)
4. Scan token → payment complete → internet granted

NoDogSplash stays on HTTP. The Go TLS proxy handles HTTPS for billing + admin.

## Approach

### Phase 1: Rebase & Merge PR #121

1. **Clone Amperstrand fork** → checkout `feat/https-reverse-proxy` branch
2. **Rebase onto `v0.5.0-alpha3` tag** (current upstream release)
3. **Resolve uhttpd binding conflict** — upstream main binds `uhttpd` to `0.0.0.0:8080`, the reverse proxy branch binds to `127.0.0.1:8080`. Must reconcile:
   - If Go reverse proxy handles port 443 → `uhttpd` should bind to `127.0.0.1:8080` (no public HTTP exposed)
   - But this breaks the captive portal on port 2050 (NoDogSplash handles that separately)
   - Decision needed: does the Go TLS proxy also proxy NoDogSplash's portal endpoint? Or does NoDogSplash remain HTTP?
   - **Recommendation:** NoDogSplash captive portal stays on HTTP (port 2050) — the camera API is only needed on the admin/billing pages, not the captive portal redirect. Go TLS proxy handles `:443` → LuCI admin + tollgate API.
4. **Open new PR** (or reopen PR #121) with rebased branch
5. **Tag human reviewer** — request manual review + approval
6. **After merge** → tag new release `v0.5.0-alpha4` (or `v0.5.0-rc1`)

### Phase 2: Verify on Lab GL-MT6000

1. Flash GL-MT6000 with stock OpenWrt
2. Install the new release (with merged HTTPS)
3. Run:
   ```sh
   tollgate-apply-ssl
   ```
4. Verify:
   - Self-signed cert generated at `/etc/tollgate/ssl/server.{crt,key}`
   - `https://192.168.1.1/net4sats/` works with self-signed cert warning
   - `https://net4sats.lan/` resolves (dnsmasq integration)
   - Phone camera works from the admin panel (HTTPS origin)
   - Portal still works on HTTP (port 2050)
5. Record Playwright test demonstrating camera access over HTTPS
6. Upload to Blossom

### Phase 3: Update Runbook & conWRT

1. Add to `instructions.md`:
   ```sh
   tollgate-apply-ssl   # generates self-signed cert, enables HTTPS
   ```
2. Add to conWRT `net4sats` flow as a new step:
   ```python
   Step(
       kind="shell",
       title="Enable HTTPS with self-signed cert",
       detail="Generates self-signed cert for net4sats.lan and enables TLS on port 443",
       command="tollgate-apply-ssl",
   )
   ```
3. Update `instructions.md` with HTTPS verification steps

### Phase 4: Ship to Endo

1. Package the new release (with HTTPS merged) as v0.5.0-alpha4
2. Upload new blossom URLs
3. Update instructions.md with new artifact URLs
4. Give updated runbook to Endo for testing

## Human Approval Gates

| Gate | Who | When |
|---|---|---|
| 🔴 Rebase strategy | Human reviewer | Before rebasing — confirm: proxy all traffic or admin-only? |
| 🔴 PR #121 merge | Human reviewer | Before merging — review the rebased diff |
| 🔴 Runbook changes | Human reviewer | Before shipping to Endo |
| 🔴 Package release | Human reviewer | Before publishing v0.5.0-alpha4 |

## Tasks

### Phase 1 (Rebase + Merge)
- [ ] T1.1: Clone Amperstrand fork, checkout feat/https-reverse-proxy
- [ ] T1.2: Rebase onto v0.5.0-alpha3 tag, resolve uhttpd binding conflict
- [ ] T1.3: Open PR, tag human reviewer for approval
- [ ] T1.4: After merge, tag v0.5.0-alpha4

### Phase 2 (Lab Verification)
- [ ] T2.1: Wait for GL-MT6000 to be connected (cron reminder active)
- [ ] T2.2: Flash stock OpenWrt + install new release
- [ ] T2.3: Run tollgate-apply-ssl, verify HTTPS
- [ ] T2.4: Record Playwright test, upload to Blossom

### Phase 3 (Docs + Automation)
- [ ] T3.1: Add HTTPS step to instructions.md
- [ ] T3.2: Add HTTPS step to conWRT net4sats flow
- [ ] T3.3: Update configurationwizzard packaging (postinst runs tollgate-apply-ssl)

### Phase 4 (Ship)
- [ ] T4.1: Upload new blossom artifacts
- [ ] T4.2: Update artifact URLs in instructions.md + conWRT flow
- [ ] T4.3: Give runbook to Endo
