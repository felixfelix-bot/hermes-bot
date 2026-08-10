# Proxy Migration Plan — Independent Proxies (Option B)

**Date:** 2026-07-30  
**Goal:** Both laptop (CobradorWave) and DQ05 run independent, up-to-date zai-proxy instances with full failover cascade. No SSH tunnel dependency.

## Background

The `reverse-tunnel-to-remote.service` on the laptop was hijacking port 9099 locally via `sshd-session`, routing all LLM traffic to DQ05's ancient 696-line zai_proxy.py (no external failover, z.ai key exhausted → 401 Authentication Failed). The laptop's own 3070-line proxy with DeepInfra/PPQ/OpenRouter failover was dead and couldn't bind the port.

## Checklist

### Phase 1: Stop tunnels and old proxy
- [x] 1.1 Stop laptop `reverse-tunnel-to-remote.service`
- [x] 1.2 Disable laptop `reverse-tunnel-to-remote.service`
- [x] 1.3 Stop DQ05 `reverse-tunnel.service`
- [x] 1.4 Disable DQ05 `reverse-tunnel.service`
- [x] 1.5 Stop DQ05 `zai-proxy.service` (old 696-line version)

### Phase 2: Update DQ05's proxy code
- [x] 2.1 Back up DQ05's old `zai_proxy.py`
- [x] 2.2 Copy laptop's `zai_proxy.py` (3070 lines) to DQ05
- [x] 2.3 Copy `model_matrix.json` to DQ05

### Phase 3: Update DQ05's API keys
- [x] 3.1 Back up DQ05's `.env`
- [x] 3.2 Overwrite DQ05's `.env` with laptop's keys (DEEPINFRA, PPQ, OPENROUTER, OLLAMA_CLOUD, ZAI)

### Phase 4: Fix DQ05's systemd unit
- [x] 4.1 Update `zai-proxy.service` to use venv python
- [x] 4.2 Add spend-cap drop-in
- [x] 4.3 `systemctl --user daemon-reload` on DQ05

### Phase 5: Restart laptop proxy
- [x] 5.1 Restart `zai-proxy.service` on laptop
- [x] 5.2 Verify laptop proxy responds (curl test) → 200 OK

### Phase 6: Start DQ05 proxy
- [x] 6.1 Start `zai-proxy.service` on DQ05
- [x] 6.2 Verify DQ05 proxy responds (curl test) → 200 OK

### Phase 7: Restart gateways
- [x] 7.1 Restart `hermes-gateway.service` on laptop
- [x] 7.2 Restart `hermes-gateway.service` on DQ05

### Phase 8: Verify end-to-end
- [x] 8.1 Laptop: proxy returns 200 OK with chat completion
- [x] 8.2 DQ05: proxy returns 200 OK with chat completion
- [x] 8.3 Both: `/quota` shows active key "ours" at 0%, "friend" at 29%
- [x] 8.4 Both: zero 401 errors in gateway logs

## Result

**Migration complete.** Both machines now run independent, up-to-date zai-proxy instances:
- Laptop (CobradorWave): 3070-line zai_proxy.py, Python 3.11.15, IPv4-only bind, full failover cascade
- DQ05: same 3070-line zai_proxy.py, Python 3.13.7, IPv4-only bind, full failover cascade
- Both share the same API keys (DEEPINFRA, PPQ, OPENROUTER, OLLAMA_CLOUD, ZAI)
- SSH reverse tunnels disabled on both machines
- DQ05's MRE/shadow/dispatch_gate modules disabled (no `src` module) — core proxy + external failover works fine
- Zero 401 errors after restart on both machines

## Post-Migration Notes

- Both machines share the same API keys (DeepInfra, PPQ, OpenRouter, Ollama Cloud) — spend from one counts against the other
- DQ05 has its own z.ai key (`038e51301df1...`) which will be overwritten with laptop's keys
- No tunnel needed — each machine is self-sufficient
- The `reverse-tunnel-to-remote.service` and DQ05's `reverse-tunnel.service` are disabled (not deleted) in case we need them later