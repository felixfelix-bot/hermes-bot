# PLAN: Gateway Restart From Chat

## Problem
`hermes gateway restart` cannot be executed from inside the gateway process — SIGTERM propagates to child processes, killing the command before it completes.

## Solution: Deferred Restart Via One-Shot Cron

### Design
A wrapper script that schedules the gateway restart **outside** the current process tree:

1. The agent writes a one-shot cron entry (1 minute in the future)
2. The cron job runs `hermes gateway restart` from a **fresh shell** (not a child of the gateway)
3. The agent has 60 seconds to deliver its final response before the gateway dies

### Implementation

**Step 1: Create `~/scripts/gateway-restart.sh`**

```bash
#!/bin/bash
# Schedule a gateway restart with configurable delay
# Usage: gateway-restart.sh [delay_minutes]

DELAY=${1:-1}  # default: 1 minute
LABEL="gateway-restart-$(date +%s)"

# Write a one-shot cron entry
CRON_LINE="$(date -d "+${DELAY} minutes" '+%M %H %d %m *') ${LABEL}"$'\t'"hermes gateway restart > /dev/null 2>&1"

# Create a temp file with existing crontab + new entry
(crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -

echo "Gateway restart scheduled in ${DELAY} minute(s)."
echo "Cron label: ${LABEL}"
```

**Step 2: Create `~/scripts/gateway-restart-cancel.sh`**

```bash
#!/bin/bash
# Cancel a pending scheduled restart
crontab -l 2>/dev/null | grep -v "gateway-restart-" | crontab -
echo "Pending gateway restart cancelled."
```

**Step 3: Integrate as `hermes cron` scheduled job**

Alternatively — use `hermes cron create` with a 1-minute delay:
```bash
hermes cron create \
  --name "gateway-restart" \
  --script "~/scripts/gateway-restart-cron.sh" \
  --no-agent \
  --schedule "1m"
```

### Safety Considerations

| Concern | Mitigation |
|---|---|
| Agent session lost mid-response | 60s delay gives agent time to finish; message delivery completes before SIGTERM |
| Workers running | Kanban workers run as **separate processes** — gateway restart doesn't kill them. They reconnect on next heartbeat. |
| Cron entry left behind | Cleanup script (`gateway-restart-cancel.sh`) removes pending entries |
| Multiple restarts queued | Only the first fires; subsequent cron writes append more entries — use dedup label |
| `at` not installed | Cron-based approach works everywhere |

### Tasks

1. Create `~/scripts/gateway-restart.sh` with cron scheduling logic
2. Create `~/scripts/gateway-restart-cancel.sh` for cancel
3. Create `~/scripts/gateway-restart-cron.sh` for hermes cron integration
4. Test: schedule restart, verify cron entry written, verify gateway restarts after delay
5. Save as skill: `skill_manage(action='create', name='gateway-restart')`
