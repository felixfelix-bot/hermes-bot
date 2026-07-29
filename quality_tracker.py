#!/usr/bin/env python3
"""quality_tracker — Model quality tracking via test pass/fail signals.

Stage 6 of cost-quality optimization: feeds binary test pass/fail signals
into a quality score per model. This is designed to integrate with the
existing burn_predictor.py Kalman filter as a new quality dimension.

The quality score is an Exponential Moving Average (EMA) of test pass rate,
which is mathematically equivalent to a 1D Kalman filter with fixed gain.

Usage:
    from quality_tracker import record_test_result, get_quality_score

    # After running tests on worker output:
    record_test_result("glm-4.5-flash", passed=True)
    record_test_result("glm-4.5-flash", passed=False)

    # When making routing decisions:
    score = get_quality_score("glm-4.5-flash")  # 0.0 to 1.0
    if score < 0.5:
        # Skip this model, use manager directly
        ...
"""
import json
import sqlite3
import time
from pathlib import Path
from collections import defaultdict


DB_PATH = Path.home() / ".hermes" / "bot" / "zai_usage.db"

# EMA decay factor — higher = faster adaptation to recent results.
# 0.3 means ~30% weight to the new result, 70% to historical average.
# With this factor, it takes ~10 consecutive failures to drop from 1.0 to <0.5.
EMA_ALPHA = 0.3

# Quality thresholds for routing decisions
QUALITY_EXCLUDE_THRESHOLD = 0.5   # Below this → skip model entirely
QUALITY_PENALTY_THRESHOLD = 0.7   # Below this → add cost penalty


def _get_db():
    """Get a connection to the usage DB."""
    conn = sqlite3.connect(str(DB_PATH), timeout=10, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS model_quality ("
        "model TEXT NOT NULL, "
        "quality_score REAL DEFAULT 1.0, "
        "total_tests INTEGER DEFAULT 0, "
        "passed_tests INTEGER DEFAULT 0, "
        "last_updated REAL, "
        "PRIMARY KEY (model))"
    )
    return conn


def record_test_result(model: str, passed: bool) -> float:
    """Record a test result and update the model's quality score.

    Args:
        model: The model name (e.g., "glm-4.5-flash")
        passed: True if tests passed, False if they failed

    Returns:
        Updated quality score (0.0 to 1.0)
    """
    try:
        conn = _get_db()
        row = conn.execute(
            "SELECT quality_score, total_tests, passed_tests FROM model_quality WHERE model=?",
            (model,)
        ).fetchone()

        if row:
            old_score, total, passed_count = row
            # EMA update: new_score = alpha * measurement + (1-alpha) * old_score
            measurement = 1.0 if passed else 0.0
            new_score = EMA_ALPHA * measurement + (1 - EMA_ALPHA) * old_score
            new_score = max(0.0, min(1.0, new_score))
            new_total = total + 1
            new_passed = passed_count + (1 if passed else 0)
        else:
            # First result — initialize with it
            new_score = 1.0 if passed else 0.7  # optimistic start
            new_total = 1
            new_passed = 1 if passed else 0

        conn.execute(
            "INSERT INTO model_quality (model, quality_score, total_tests, passed_tests, last_updated) "
            "VALUES (?,?,?,?,?) ON CONFLICT(model) DO UPDATE SET "
            "quality_score=excluded.quality_score, "
            "total_tests=excluded.total_tests, "
            "passed_tests=excluded.passed_tests, "
            "last_updated=excluded.last_updated",
            (model, new_score, new_total, new_passed, time.time())
        )
        conn.commit()
        conn.close()
        return new_score
    except Exception:
        return 1.0  # fail-open: assume good quality on error


def get_quality_score(model: str) -> float:
    """Get the current quality score for a model (0.0 to 1.0).

    Returns 1.0 if no data exists (optimistic default for untested models).
    """
    try:
        conn = _get_db()
        row = conn.execute(
            "SELECT quality_score FROM model_quality WHERE model=?", (model,)
        ).fetchone()
        conn.close()
        return row[0] if row else 1.0
    except Exception:
        return 1.0


def get_quality_stats(model: str) -> dict:
    """Get detailed quality stats for a model."""
    try:
        conn = _get_db()
        row = conn.execute(
            "SELECT quality_score, total_tests, passed_tests, last_updated "
            "FROM model_quality WHERE model=?", (model,)
        ).fetchone()
        conn.close()
        if not row:
            return {"model": model, "quality_score": 1.0, "total_tests": 0,
                    "passed_tests": 0, "pass_rate": 0.0, "status": "untested"}
        score, total, passed, last_ts = row
        pass_rate = passed / total if total > 0 else 0.0
        if score < QUALITY_EXCLUDE_THRESHOLD:
            status = "excluded"
        elif score < QUALITY_PENALTY_THRESHOLD:
            status = "penalized"
        else:
            status = "healthy"
        return {
            "model": model,
            "quality_score": round(score, 3),
            "total_tests": total,
            "passed_tests": passed,
            "pass_rate": round(pass_rate, 3),
            "status": status,
        }
    except Exception as e:
        return {"model": model, "error": str(e)}


def should_route_to_model(model: str) -> tuple[bool, str]:
    """Check if a model should be used based on quality score.

    Returns (should_use, reason).
    """
    score = get_quality_score(model)
    if score < QUALITY_EXCLUDE_THRESHOLD:
        return False, f"quality score {score:.2f} < {QUALITY_EXCLUDE_THRESHOLD} (excluded)"
    return True, f"quality score {score:.2f}"


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: quality_tracker.py <model> [--record pass|fail] [--stats]")
        sys.exit(1)

    model = sys.argv[1]
    if "--record" in sys.argv:
        idx = sys.argv.index("--record")
        result = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else "pass"
        passed = result.lower() in ("pass", "passed", "true", "1")
        score = record_test_result(model, passed)
        print(f"Recorded {'PASS' if passed else 'FAIL'} for {model}. New score: {score:.3f}")
    elif "--stats" in sys.argv:
        stats = get_quality_stats(model)
        print(json.dumps(stats, indent=2))
    else:
        score = get_quality_score(model)
        should, reason = should_route_to_model(model)
        print(f"{model}: score={score:.3f}, should_route={should}, {reason}")
