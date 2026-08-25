# Plan: Capture 422 Error Response Body from NeuralWatt/DeepInfra

**Date:** 2026-08-23
**Status:** EXECUTING

## Problem

The proxy's `_try_external_failover` catches HTTP 422 from neuralwatt and deepinfra but never reads the response body. The 422 body contains the exact field name that's rejected, but it's lost. We need to capture it to know which field to strip.

## Changes

### C1: Log 422 response body at line 4408
When `he.code == 422`, call `he.read(4096)` and log the body before re-raising.

### C2: Log body keys for neuralwatt at line 4307
Log `list(body_json.keys())` before forwarding to neuralwatt, so we can correlate which field caused the 422.

## Checklist

- [ ] C1. Add 422 response body capture in `_try_external_failover` exception handler
- [ ] C2. Add body keys debug log for neuralwatt before forwarding
- [ ] C3. Compile check `zai_proxy.py`
- [ ] C4. Restart zai-proxy
- [ ] C5. Wait for / trigger a 422 and verify the body is captured in journal
- [ ] C6. Use captured info to fix the strip list
- [ ] C7. espeak-ng notification
