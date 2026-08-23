#!/usr/bin/env python3
"""Fix the masked PPQ_API_KEY in .env by reading the real key from config.yaml."""
import yaml
from pathlib import Path

env_path = Path.home() / '.hermes' / '.env'
config_path = Path.home() / '.hermes' / 'profiles' / 'manager' / 'config.yaml'

# Get real key from config
with open(config_path) as f:
    cfg = yaml.safe_load(f)
real_key = None
for fp in cfg.get('fallback_providers', []):
    if 'ppq' in str(fp.get('base_url', '')):
        real_key = fp['api_key']
        break

if not real_key:
    print("ERROR: Could not find PPQ key in config.yaml")
    exit(1)

print(f"Real key: {len(real_key)} chars")

# Read .env and fix the PPQ_API_KEY line
lines = env_path.read_text().splitlines()
fixed = False
for i, line in enumerate(lines):
    if line.startswith('PPQ_API_KEY'):
        old_key = line.split('=', 1)[1].strip().strip("'\"")
        print(f"Old .env key: '{old_key}' ({len(old_key)} chars)")
        if len(old_key) < 20:  # It's masked/truncated
            lines[i] = f"PPQ_API_KEY='{real_key}'"
            fixed = True
            print(f"FIXED line {i+1}")
        else:
            print("Key looks OK already")
        break

if fixed:
    env_path.write_text('\n'.join(lines) + '\n')
    print("Written to .env")
else:
    print("No fix needed")
