#!/usr/bin/env python3
"""Fetch ALL PPQ query history and aggregate spend. Writes to /tmp/ppq_full_history.json"""
import json, urllib.request, sys, time
from collections import defaultdict
import yaml

with open('/home/c03rad0r/.hermes/profiles/manager/config.yaml') as f:
    cfg = yaml.safe_load(f)
key = None
for fp in cfg.get('fallback_providers', []):
    if 'ppq' in str(fp.get('base_url','')):
        key = fp['api_key']
        break

daily = defaultdict(lambda: {'count': 0, 'cost': 0.0, 'input': 0, 'output': 0})
page = 1
total_fetched = 0
total_cost = 0
errors = 0

while page <= 871:
    url = f"https://api.ppq.ai/queries/history?limit=200&page={page}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        errors += 1
        if errors > 30:
            break
        time.sleep(2)
        page += 1
        continue

    queries = data.get('data', [])
    if not queries:
        break

    for q in queries:
        ts = q.get('timestamp', '')
        day = ts[:10] if ts else 'unknown'
        cost = float(q.get('price_in_usd', 0) or 0)
        daily[day]['count'] += 1
        daily[day]['cost'] += cost
        daily[day]['input'] += int(q.get('input_count', 0) or 0)
        daily[day]['output'] += int(q.get('output_count', 0) or 0)
        total_cost += cost
        total_fetched += 1

    page += 1

result = {
    'total_queries': total_fetched,
    'total_cost_usd': round(total_cost, 4),
    'pages_fetched': page - 1,
    'errors': errors,
    'daily': {day: dict(d) for day, d in sorted(daily.items())}
}
with open('/tmp/ppq_full_history.json', 'w') as f:
    json.dump(result, f, indent=2, default=str)
print(f"DONE: {total_fetched} queries, ${total_cost:.4f}, {page-1} pages, {errors} errors")
