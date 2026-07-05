---
name: nostr-event-cleanup
description: >
  Delete Nostr events (Kind 5 deletion requests) from ALL relays including
  indexers. Critical for removing accidentally published data from ngit/gitworkshop.
  Covers: finding events by kind/tag across multiple relays, signing deletion
  events with nak, verifying deletion worked.
version: 1.0.0
author: hermes-orchestration
tags: [nostr, ngit, gitworkshop, deletion, nip-09, cleanup]
---

# Nostr Event Cleanup

## When to Use

- Accidentally published a repo/event with sensitive data (names, keys)
- Need to delete ngit/gitworkshop repository announcement events
- User sees stale repos on gitworkshop.dev that were "deleted" but still visible
- Any Kind 5 deletion request needs to reach ALL relays, not just the main one

## Key Insight: Multiple Relay Types

Nostr events propagate across different relay types:

| Relay | Purpose | Who queries it |
|-------|---------|----------------|
| `wss://relay.ngit.dev` | ngit storage relay | ngit CLI operations |
| `wss://index.ngit.dev` | ngit INDEXER relay | **gitworkshop.dev reads THIS** |
| `wss://nos.lol` | General Nostr relay | Many clients |
| `wss://gitnostr.com` | Another ngit relay | gitnostr.com web UI |
| `wss://relay.damus.io` | General relay (often down) | damus client |

**The #1 mistake:** Publishing Kind 5 deletions only to `relay.ngit.dev` and
forgetting the indexer (`index.ngit.dev`) and general relays (`nos.lol`).
gitworkshop.dev reads from `index.ngit.dev`, so events there persist even after
deletion from the storage relay.

## Step 1: Find the Events to Delete

Use `nak req` or a Python websocket script to find events by kind and author.

```bash
# Find all Kind 30617 (repo announcement) events from a specific pubkey
nak req -k 30617 \
  -a <pubkey-hex> \
  -l 30 \
  wss://relay.ngit.dev
```

For finding events by name/tag across multiple relays, use Python websockets:

```python
import json, websocket

def query_relay(relay_url, filters, timeout_ms=5000):
    ws = websocket.create_connection(relay_url, timeout=10)
    sub_id = 'cleanup_query'
    ws.send(json.dumps(["REQ", sub_id, filters]))
    events = []
    while True:
        try:
            ws.settimeout(timeout_ms / 1000.0)
            msg = ws.recv()
            data = json.loads(msg)
            if data[0] == "EVENT" and data[1] == sub_id:
                events.append(data[2])
            elif data[0] == "EOSE":
                break
        except:
            break
    try:
        ws.send(json.dumps(["CLOSE", sub_id]))
        ws.close()
    except:
        pass
    return events

filters = {
    "kinds": [30617],
    "authors": ["<pubkey-hex>"],
    "#d": ["repo-name-to-delete"],
    "limit": 5
}

for relay in ["wss://index.ngit.dev", "wss://relay.ngit.dev", "wss://nos.lol",
              "wss://gitnostr.com", "wss://relay.damus.io"]:
    events = query_relay(relay, filters)
    if events:
        print(f"{relay}: FOUND {len(events)} events")
        for ev in events:
            print(f"  id: {ev['id']}")
```

## Step 2: Sign and Publish Kind 5 Deletion Events

Kind 5 is the NIP-09 deletion request. It references the event ID to delete.

### Finding the nsec

Check these locations:
1. `~/.gitconfig` under `[nostr]` section (ngit stores it here)
2. KeePass database (`keepassxc-cli show ~/secrets/openrouter.kdbx "Nostr Key"`)
3. Environment variables (`NOSTR_SECRET_KEY`)
4. `~/.config/nostr/secret.hex`

```bash
# Check gitconfig first
grep -A3 '\[nostr\]' ~/.gitconfig
```

### Publishing deletions to ALL relays

```bash
SEC="nsec1..."  # From gitconfig or KeePass

# Delete one event — publish to ALL relays that might have it
nak event --sec "$SEC" -k 5 \
  -t e=<event-id-to-delete> \
  -t k=<kind-of-event> \
  -c "Reason for deletion" \
  wss://index.ngit.dev wss://relay.ngit.dev wss://nos.lol wss://gitnostr.com
```

**CRITICAL:** Always include `wss://index.ngit.dev` in the relay list —
gitworkshop.dev reads from it.

### Using Amber (NIP-46) for signing

If the nsec belongs to a phone wallet (not available locally), use a bunker URL:

```bash
nak event --sec "bunker://<pubkey>?relay=wss://nostr.oxtr.dev&secret=<secret>" \
  -k 5 -t e=<event-id> -t k=<kind> \
  wss://index.ngit.dev wss://nos.lol
```

See the `nip46-signer` skill for setting up Amber bunker signing.
Note: Amber has a stale session bug — the first call after fresh session works.
Batch all signing operations into one session.

## Step 3: Verify Deletion

Re-query all relays to confirm events are gone:

```bash
# Quick check with nak
nak req -k 30617 -a <pubkey-hex> "#d"=["repo-name"] wss://index.ngit.dev
```

Or use the Python script from Step 1 to check all relays.

### gitworkshop.dev verification

gitworkshop.dev is a **single-page app (SPA)** — it returns HTTP 200 for ANY
URL because the HTML shell is always served. The actual repo data is fetched
client-side via WebSocket. To verify:

1. Open the URL in a browser with Amber connected
2. The page will show "repository not found" if the event is deleted
3. `curl -s URL` always returns 200 HTML — this is NOT meaningful

The definitive check: query the relay directly (Step 1 script). If the event
is gone from `wss://index.ngit.dev`, gitworkshop.dev will show nothing.

## Step 4: Clean up residual git data (for repos)

For repository announcements (Kind 30617), the git data itself may persist on
GRASP servers even after the announcement event is deleted. Force-push empty
content to overwrite:

```bash
# Create empty repo and force-push to wipe remote history
mkdir /tmp/empty-repo && cd /tmp/empty-repo
git init
git commit --allow-empty -m "cleanup"
git remote add origin https://relay.ngit.dev/<npub>/<repo-name>.git
git push -f origin main
```

## Relay Checklist for Deletion

Always publish Kind 5 to ALL of these:

```
wss://index.ngit.dev       # gitworkshop.dev reads THIS
wss://relay.ngit.dev        # ngit storage
wss://nos.lol               # general Nostr
wss://gitnostr.com          # gitnostr.com web UI
wss://relay.damus.io        # general (often CF 503)
```

## Common Pitfalls

1. **Only deleting from relay.ngit.dev** — gitworkshop.dev uses index.ngit.dev
2. **Trusting HTTP status codes on SPA sites** — always check relay directly
3. **Forgetting to include `k` tag** — some relays need `-t k=<kind>` to know
   what kind of event is being deleted
4. **Amber stale sessions** — batch all signing into one session
5. **Events on other people's relays** — events may propagate to relays you
   don't control. Kind 5 is a REQUEST, not a command. Most relays honor it,
   but some may not.
