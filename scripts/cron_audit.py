#!/usr/bin/env python3
"""
cron_audit.py — Audit all Hermes cron jobs for staleness, errors, and dead jobs.
Outputs a structured report. Designed to run as a no_agent cron job.

Stale job questions are routed to the appropriate Signal group based on
the job name/script keywords. This prevents context pollution by asking
about balloon jobs in balloon-hermes, tollgate jobs in tollgate-hermes, etc.

Exit codes:
  0 = all healthy (silent)
  1 = issues found (output delivered to cron destination)
"""

import json, os, sys, re
from datetime import datetime, timezone, timedelta

JOBS_FILE = os.path.expanduser("~/.hermes/profiles/manager/cron/jobs.json")
STALE_DAYS = 7
ERROR_THRESHOLD = 3  # consecutive errors before alerting

# Map cron job keywords → Signal group for routing questions
GROUP_MAP = {
    "balloon": "balloon-hermes",
    "meshcore": "balloon-hermes",
    "lr2021": "balloon-hermes",
    "tollgate": "tollgate-hermes",
    "plebeian": "plebeian-market-hermes",
    "plebean": "plebeian-market-hermes",
    "net4sats": "net4sats-MVP",
    "fips": "fips-exit-node-poc",
    "microfips": "microFIPS-esp32",
    "human-gate": "human-gate-hermes",
    "protein": "Protein-RNA interactome analysis",
    "vanity": "vanity-npub-hermes",
}

# Default group for infra/devops jobs
DEFAULT_GROUP = "infra-ops"


def route_to_group(job_name):
    """Determine which Signal group a job belongs to."""
    name_lower = job_name.lower()
    for keyword, group in GROUP_MAP.items():
        if keyword in name_lower:
            return group
    return DEFAULT_GROUP


def main():
    with open(JOBS_FILE) as f:
        data = json.load(f)
    jobs = data.get("jobs", [])

    now = datetime.now(timezone.utc)
    stale_threshold = now - timedelta(days=STALE_DAYS)

    findings = {
        "paused": [],
        "error": [],
        "stale": [],
        "never_ran": [],
        "one_shot_expired": [],
    }

    total = len(jobs)
    healthy = 0

    for j in jobs:
        name = j.get("name", "unnamed")
        job_id = j.get("job_id", "?")
        enabled = j.get("enabled", True)
        last_status = j.get("last_status")
        last_run_str = j.get("last_run_at")
        schedule = j.get("schedule", {})
        deliver = j.get("deliver", "local")
        script = j.get("script", "")
        no_agent = j.get("no_agent", False)

        # Parse last run
        last_run = None
        if last_run_str:
            try:
                last_run = datetime.fromisoformat(last_run_str.replace("Z", "+00:00"))
            except:
                pass

        # Parse schedule display
        sched_display = schedule.get("display", str(schedule)) if isinstance(schedule, dict) else str(schedule)

        # Check one-shot jobs
        is_one_shot = isinstance(schedule, dict) and schedule.get("kind") == "once"
        if is_one_shot:
            run_at = schedule.get("run_at", "")
            if last_run_str is None:
                # Check if the scheduled time has passed
                try:
                    scheduled_time = datetime.fromisoformat(run_at.replace("Z", "+00:00"))
                    if scheduled_time < now:
                        findings["one_shot_expired"].append({
                            "name": name, "id": job_id,
                            "scheduled": run_at,
                            "group": route_to_group(name),
                        })
                        continue
                except:
                    pass
                continue  # Future one-shot, skip

        if not enabled:
            findings["paused"].append({
                "name": name, "id": job_id,
                "schedule": sched_display,
                "script": script,
                "last_run": last_run_str or "never",
                "group": route_to_group(name),
            })
        elif last_status == "error":
            findings["error"].append({
                "name": name, "id": job_id,
                "schedule": sched_display,
                "script": script,
                "last_run": last_run_str or "?",
                "group": route_to_group(name),
            })
        elif last_run is None and not is_one_shot:
            # Recurring job that never ran (may be newly created)
            # Only flag if it's been around for a while — check creation
            findings["never_ran"].append({
                "name": name, "id": job_id,
                "schedule": sched_display,
                "script": script,
                "group": route_to_group(name),
            })
        elif last_run and last_run < stale_threshold:
            days_stale = (now - last_run).days
            findings["stale"].append({
                "name": name, "id": job_id,
                "schedule": sched_display,
                "script": script,
                "last_run": last_run_str,
                "days_stale": days_stale,
                "group": route_to_group(name),
            })
        else:
            healthy += 1

    # Build report
    issues = sum(len(v) for v in findings.values())
    if issues == 0:
        # All healthy — silent
        sys.exit(0)

    print(f"CRON AUDIT — {now.strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"Total: {total} jobs | Healthy: {healthy} | Issues: {issues}")
    print()

    if findings["error"]:
        print(f"🔴 ERROR STATE ({len(findings['error'])}):")
        for j in findings["error"]:
            print(f"  • {j['name']} (script={j['script']})")
            print(f"    last_run={j['last_run']} → ask in: {j['group']}")
        print()

    if findings["paused"]:
        print(f"⏸️  PAUSED — REMOVE CANDIDATES ({len(findings['paused'])}):")
        for j in findings["paused"]:
            print(f"  • {j['name']} (script={j['script']})")
            print(f"    last_run={j['last_run']} → ask in: {j['group']}")
        print()

    if findings["stale"]:
        print(f"📅 STALE >{STALE_DAYS} DAYS ({len(findings['stale'])}):")
        for j in findings["stale"]:
            print(f"  • {j['name']} — stale {j['days_stale']}d (script={j['script']})")
            print(f"    last_run={j['last_run']} → ask in: {j['group']}")
        print()

    if findings["never_ran"]:
        print(f"❓ NEVER RAN ({len(findings['never_ran'])}):")
        for j in findings["never_ran"]:
            print(f"  • {j['name']} (script={j['script']}, schedule={j['schedule']})")
            print(f"    → ask in: {j['group']}")
        print()

    if findings["one_shot_expired"]:
        print(f"爆竹 EXPIRED ONE-SHOTS ({len(findings['one_shot_expired'])}):")
        for j in findings["one_shot_expired"]:
            print(f"  • {j['name']} (scheduled={j['scheduled']})")
            print(f"    → ask in: {j['group']}")
        print()

    # Group routing summary
    all_issues = []
    for category in ["error", "paused", "stale", "never_ran", "one_shot_expired"]:
        all_issues.extend(findings[category])

    if all_issues:
        group_issues = {}
        for item in all_issues:
            g = item.get("group", DEFAULT_GROUP)
            if g not in group_issues:
                group_issues[g] = []
            group_issues[g].append(item["name"])

        print("📋 ROUTING SUMMARY — where to ask about each issue:")
        for group, names in sorted(group_issues.items()):
            print(f"  {group}: {', '.join(names)}")

    sys.exit(1)


if __name__ == "__main__":
    main()
