#!/usr/bin/env python3
"""Dynamic context length governor.

Detects the proxy's actually-served model from zai_usage.db, looks up
its maximum context from model_context_registry.json, and updates
``model.context_length`` in config via ``hermes config set``.

**Multi-profile**: iterates over ALL profiles in ~/.hermes/profiles/*/
and applies the same logic to each one.

Zero LLM cost: detection is purely from the local SQLite DB.
Safety: if any step fails, config is left unchanged (backward-compatible).
Changes take effect at next session start, NOT mid-session.

Runs on cron (every 15 min), chained BEFORE the compression governors so
the growth governor picks up the fresh context_length.

State: ~/.hermes/bot/dynamic_context_state.json
Registry: ~/.hermes/bot/model_context_registry.json
Config: model.context_length (applied via ``hermes config set``)
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BOT_DIR = Path.home() / ".hermes" / "bot"
DB_PATH = BOT_DIR / "zai_usage.db"
REGISTRY_PATH = BOT_DIR / "model_context_registry.json"
STATE_FILE = BOT_DIR / "dynamic_context_state.json"
PROFILES_DIR = Path.home() / ".hermes" / "profiles"
# Backward compat: tests and old callers reference CONFIG_PATH for manager
CONFIG_PATH = PROFILES_DIR / "manager" / "config.yaml"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MINIMUM_CONTEXT_LENGTH = 128_000  # Safety floor — never set below this
PROXY_URL = "http://localhost:9099/v1/chat/completions"

# 413 safety valve: if more than this many 413 errors in past hour,
# reduce context_length to 90% of the registry value.
ERROR_413_THRESHOLD = 3
ERROR_413_REDUCTION = 0.90

# Family-level fallbacks for model names that don't match any registry
# key directly.  These represent the conservative default for each family.
FAMILY_FALLBACKS: dict[str, int] = {
    "glm": 200_000,
    "kimi": 128_000,
    "deepseek": 1_000_000,
}

# Safe fallback when config can't be read
DEFAULT_CONTEXT_LENGTH = 200_000


# ---------------------------------------------------------------------------
# Profile discovery
# ---------------------------------------------------------------------------

def discover_profiles(profiles_dir: Path | None = None) -> list[str]:
    """Return all profile directory names that have a ``config.yaml``.

    Scans *profiles_dir* (default: ``~/.hermes/profiles``) and returns
    sorted names of subdirectories that contain ``config.yaml``.
    Returns an empty list if the directory doesn't exist or is empty.
    """
    if profiles_dir is None:
        profiles_dir = PROFILES_DIR
    if not profiles_dir or not profiles_dir.is_dir():
        return []
    return sorted(
        entry.name
        for entry in profiles_dir.iterdir()
        if entry.is_dir() and (entry / "config.yaml").exists()
    )


def profile_config_path(profile_name: str) -> Path:
    """Return the ``config.yaml`` path for a given profile name."""
    return PROFILES_DIR / profile_name / "config.yaml"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def load_registry(registry_path: Path | None = None) -> dict[str, int]:
    """Load the model context registry JSON.

    Returns an empty dict on any failure.
    """
    if registry_path is None:
        registry_path = REGISTRY_PATH
    try:
        text = registry_path.read_text()
        reg = json.loads(text)
        if isinstance(reg, dict):
            return reg
        print(f"[ctx-governor] registry not a JSON object: {type(reg)}")
    except FileNotFoundError:
        pass  # Expected when running standalone without a registry
    except Exception as e:
        print(f"[ctx-governor] registry load failed: {e}")
    return {}


# ---------------------------------------------------------------------------
# Model detection
# ---------------------------------------------------------------------------

def detect_active_model(
    db_path: Path | None = None,
    allow_probe: bool = False,
    proxy_url: str = PROXY_URL,
) -> str | None:
    """Detect the currently active (served) model.

    Resolution chain:
    1. Query zai_usage.db for the most recent successful API call's model
       (this is the post-tier-rewrite model that the proxy actually served).
    2. If DB is missing/empty and ``allow_probe`` is True, send a minimal
       1-token probe request to the proxy and read the response ``model`` field.
    3. If both fail, return ``None`` (caller should leave config unchanged).
    """
    # --- Primary: query the DB -----------------------------------------------
    if db_path is None:
        db_path = DB_PATH
    if db_path and db_path.exists():
        conn = None
        try:
            conn = sqlite3.connect(str(db_path), timeout=5)
            row = conn.execute(
                "SELECT model FROM api_calls "
                "WHERE status_code = 200 AND model IS NOT NULL "
                "ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            if row and row[0]:
                return row[0]
        except Exception as e:
            print(f"[ctx-governor] DB query failed: {e}")
        finally:
            if conn:
                conn.close()

    # --- Fallback: probe the proxy -------------------------------------------
    if allow_probe:
        model = _probe_proxy(proxy_url)
        if model:
            return model

    return None


def _probe_proxy(proxy_url: str = PROXY_URL) -> str | None:
    """Send a minimal 1-token completion request to detect the served model.

    Returns the model name from the response JSON, or ``None`` on failure.
    Cost: 1 prompt token + 1 completion token (effectively free).
    """
    try:
        import urllib.request

        probe_body = json.dumps({
            "model": "glm-5.3",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
            "stream": False,
        }).encode()

        req = urllib.request.Request(
            proxy_url,
            data=probe_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        return data.get("model")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Context length lookup
# ---------------------------------------------------------------------------

def get_model_context_length(
    model_name: str,
    registry_path: Path | None = None,
) -> int | None:
    """Look up the maximum context length for a model name.

    Resolution order:
    1. **Exact match** in the registry (e.g., ``"glm-5.3"`` → 1 000 000).
    2. **Prefix match** — the longest registry key that is a prefix of the
       model name (e.g., ``"glm-5.3"`` matches ``"glm-5.3-chat"``).
    3. **Family fallback** — extract the family (text before the first ``-``)
       and check ``FAMILY_FALLBACKS`` (e.g., ``"glm"`` → 200 000).
    4. Return ``None`` if no match found (caller should leave config unchanged).
    """
    if registry_path is None:
        registry_path = REGISTRY_PATH
    registry = load_registry(registry_path)
    if not registry:
        return None

    # 1. Exact match
    if model_name in registry:
        return registry[model_name]

    # 2. Prefix match (longest key first to prefer specificity)
    for key in sorted(registry, key=len, reverse=True):
        if model_name.startswith(key):
            return registry[key]

    # 3. Family fallback
    family = model_name.split("-")[0].lower()
    if family in FAMILY_FALLBACKS:
        return FAMILY_FALLBACKS[family]

    return None


# ---------------------------------------------------------------------------
# 413 error rate
# ---------------------------------------------------------------------------

def check_413_rate(
    db_path: Path | None = None,
    model: str | None = None,
    hours: int = 1,
) -> int:
    """Count 413 (payload too large) errors in the last ``hours`` hours.

    Optionally filter by ``model`` name.
    Returns 0 if the DB is missing or the query fails.
    """
    if db_path is None:
        db_path = DB_PATH
    if not db_path or not db_path.exists():
        return 0

    cutoff = time.time() - hours * 3600
    conn = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        if model:
            row = conn.execute(
                "SELECT COUNT(*) FROM api_calls "
                "WHERE model = ? AND status_code = 413 AND ts >= ?",
                (model, cutoff),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM api_calls "
                "WHERE status_code = 413 AND ts >= ?",
                (cutoff,),
            ).fetchone()
        return int(row[0]) if row else 0
    except Exception as e:
        print(f"[ctx-governor] 413 rate query failed: {e}")
        return 0
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------------
# Config interaction
# ---------------------------------------------------------------------------

def get_current_context_length(config_path: Path | None = None) -> int:
    """Read the current ``model.context_length`` from config.yaml.

    If *config_path* is None, uses CONFIG_PATH (manager, backward compat).
    Falls back to 200 000 (safe default for glm-5.2) on any failure.
    """
    if config_path is None:
        config_path = CONFIG_PATH
    try:
        import yaml

        cfg = yaml.safe_load(config_path.read_text())
        ctx = (cfg or {}).get("model", {}).get("context_length")
        if isinstance(ctx, int) and ctx > 0:
            return ctx
    except Exception:
        pass
    return DEFAULT_CONTEXT_LENGTH


def set_context_length(new_value: int, profile_name: str = "manager") -> bool:
    """Set ``model.context_length`` via ``hermes config set``.

    - *profile_name* selects which profile to update (default: manager).
    - Only applies if the value differs from the current config value.
    - Safety: never sets below ``MINIMUM_CONTEXT_LENGTH`` (128 000).
    - Returns ``True`` if config was updated, ``False`` otherwise (no-op,
      hermes failure, or exception).
    """
    # Safety floor
    if new_value < MINIMUM_CONTEXT_LENGTH:
        new_value = MINIMUM_CONTEXT_LENGTH

    config_path = profile_config_path(profile_name)
    current = get_current_context_length(config_path)
    if new_value == current:
        return False  # No change needed

    try:
        result = subprocess.run(
            [
                "hermes",
                "--profile", profile_name,
                "config", "set",
                "model.context_length", str(new_value),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return True
        print(f"[ctx-governor] hermes config set failed for {profile_name}: {result.stderr}")
        return False
    except Exception as e:
        print(f"[ctx-governor] config set exception for {profile_name}: {e}")
        return False


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------

def save_state(state: dict) -> None:
    """Persist governor state to the JSON state file."""
    STATE_FILE.write_text(json.dumps(state, indent=2))


def load_state() -> dict:
    """Load governor state, or return defaults if file is missing."""
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception:
        pass
    return {
        "last_detected_model": None,
        "last_detected_ctx": None,
        "last_check": None,
        "413_count_1h": 0,
        "reduced_to_90pct": False,
    }


# ---------------------------------------------------------------------------
# Per-profile processing
# ---------------------------------------------------------------------------

def process_profile(profile_name: str) -> dict:
    """Run the governor logic for a single profile.

    Steps:
    1. Detect active model from zai_usage.db (global — same DB for all profiles).
    2. Look up context_length from the registry.
    3. Check 413 error rate — reduce to 90% if >3 in past hour.
    4. Compare to the current config value.
    5. Set via ``hermes config set`` if different.

    Returns a result dict (does NOT print JSON — caller does that).
    """
    config_path = profile_config_path(profile_name)

    # 1. Detect active model
    model = detect_active_model(db_path=DB_PATH, allow_probe=False)
    current_ctx = get_current_context_length(config_path)

    if model is None:
        return {
            "profile": profile_name,
            "detected_model": None,
            "registry_ctx": None,
            "current_ctx": current_ctx,
            "new_ctx": None,
            "applied": False,
            "413_count": 0,
        }

    # 2. Look up context length from registry
    registry_ctx = get_model_context_length(model)
    if registry_ctx is None:
        return {
            "profile": profile_name,
            "detected_model": model,
            "registry_ctx": None,
            "current_ctx": current_ctx,
            "new_ctx": None,
            "applied": False,
            "413_count": 0,
        }

    # 3. Check 413 error rate
    count_413 = check_413_rate(db_path=DB_PATH, model=model, hours=1)

    # 4. If >3 413 errors, reduce to 90% of registry value (safety margin)
    new_ctx = registry_ctx
    reduced = False
    if count_413 > ERROR_413_THRESHOLD:
        new_ctx = int(registry_ctx * ERROR_413_REDUCTION)
        reduced = True
        print(
            f"[ctx-governor] 413 pressure ({count_413} errors in 1h) "
            f"— reducing to 90%: {new_ctx}"
        )

    # 5. Apply if different from current config
    applied = set_context_length(new_ctx, profile_name)

    if applied:
        print(f"[ctx-governor] updated {profile_name}: "
              f"context_length {current_ctx} → {new_ctx}")
    else:
        print(f"[ctx-governor] {profile_name}: no change "
              f"(ctx={current_ctx})")

    return {
        "profile": profile_name,
        "detected_model": model,
        "registry_ctx": registry_ctx,
        "current_ctx": current_ctx,
        "new_ctx": new_ctx,
        "applied": applied,
        "413_count": count_413,
        "reduced_to_90pct": reduced,
    }


# ---------------------------------------------------------------------------
# Main entry point (cron)
# ---------------------------------------------------------------------------

def main(profiles: list[str] | None = None) -> dict:
    """Cron entry point — iterates over ALL profiles.

    If *profiles* is None, discovers all profiles under
    ``~/.hermes/profiles/``.  Falls back to ``["manager"]`` if
    discovery finds nothing.

    Returns an aggregate dict and prints JSON for cron logs.
    """
    if profiles is None:
        profiles = discover_profiles()
    if not profiles:
        profiles = ["manager"]

    updated: list[str] = []
    skipped: list[dict] = []
    results: list[dict] = []
    last_state: dict | None = None

    for profile_name in profiles:
        cfg = profile_config_path(profile_name)
        if not cfg.exists():
            print(f"[ctx-governor] skipping {profile_name}: no config.yaml")
            skipped.append({"profile": profile_name, "reason": "no config.yaml"})
            continue

        try:
            result = process_profile(profile_name)
            results.append(result)

            if result.get("applied"):
                updated.append(profile_name)
            else:
                skipped.append({
                    "profile": profile_name,
                    "reason": "no change or model not detected",
                })

            # Track state from last successfully processed profile
            if result.get("detected_model"):
                last_state = {
                    "last_detected_model": result["detected_model"],
                    "last_detected_ctx": result.get("new_ctx"),
                    "last_check": datetime.now(timezone.utc).isoformat(),
                    "413_count_1h": result.get("413_count", 0),
                    "reduced_to_90pct": result.get("reduced_to_90pct", False),
                }
        except Exception as e:
            print(f"[ctx-governor] error processing {profile_name}: {e}")
            skipped.append({"profile": profile_name, "reason": f"error: {e}"})

    # Persist state (from last successfully processed profile)
    if last_state:
        try:
            save_state(last_state)
        except Exception as e:
            print(f"[ctx-governor] save_state failed: {e}")

    aggregate = {
        "profiles_processed": len(results),
        "profiles_updated": updated,
        "profiles_skipped": skipped,
        "profile_results": results,
    }
    print(json.dumps(aggregate, indent=2))
    return aggregate


if __name__ == "__main__":
    main()
