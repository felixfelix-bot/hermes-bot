#!/usr/bin/env python3
"""model_selector - ContextVM for LLM model selection based on price + benchmarks.

Queries PPQ for pricing, LiveBench/LMSYS for benchmarks. Recommends best
bang-for-buck model given a task type and budget.

Endpoints:
  GET /models/prices      -> all PPQ prices
  GET /models/benchmarks  -> all benchmark scores
  GET /recommend?task=<type>&budget=<usd> -> {model, provider, reason}
  GET /health             -> liveness

Task types: coding, reasoning, math, general
"""
from __future__ import annotations
import json, sqlite3, threading, time, urllib.request, urllib.error, hashlib, subprocess
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

# Config
HOME = Path.home()
PORT = 7779
CACHE_DB = HOME / ".hermes" / "bot" / "model_cache.db"
PRICE_TTL = 3600      # 1 hour
BENCHMARK_TTL = 86400  # 24 hours
DEFAULT_BUDGET = 0.10  # $0.10 default budget

PPQ_BASE_URL = "https://api.ppq.ai/v1"
LIVEBENCH_URL = "https://livebench.ai/api/leaderboard"
LMSYS_URL = "https://lmsys.org/chatbot-arena-leaderboard/api/data"

# Benchmark weights per task type
TASK_BENCHMARKS = {
    "coding": {
        "swe_bench": 0.5,
        "humaneval": 0.3,
        "bfcl": 0.2,
    },
    "reasoning": {
        "gpqa": 0.6,
        "arc_agi": 0.4,
    },
    "math": {
        "aime": 0.7,
        "gsm8k": 0.3,
    },
    "general": {
        "livebench_overall": 0.6,
        "mmlu": 0.4,
    },
}

# Model tiers for escalation - providers checked in priority order within each tier
# Priority: z.ai (free) → PPQ (pay) → OpenRouter (future) → routstr (future) → Ollama (free, local)
MODEL_TIERS = {
    0: {  # Free quota (z.ai)
        "models": ["glm-5.2", "glm-5.1", "glm-5", "glm-4.7"],
        "providers": ["z.ai", "ppq"],  # z.ai first (free), then PPQ
        "max_cost_input": 0,
        "use_case": "All tasks when quota available",
        "is_free": True,
    },
    1: {  # Ultra-cheap + Good Coding
        "models": [
            "deepseek-v4-flash", "deepseek-v4-flash",  # $0.09, 4,097 prompts/$
            "codestral",  # Mistral coding model
            "minimax-m2.5", "minimax-m2", "minimax-m2.1",  # $0.16, 2,063 prompts/$
            "glm-4.7-flash",  # $0.06
        ],
        "providers": ["z.ai", "ppq"],
        "max_cost_input": 1.00,
        "use_case": "Daily coding, autocomplete, grunt work",
    },
    2: {  # Mid-tier
        "models": [
            "deepseek-v4-pro",  # $0.46, 847 prompts/$
            "haiku-4.5", "claude-haiku",
            "glm-5", "glm-4.6",  # z.ai models
        ],
        "providers": ["z.ai", "ppq"],
        "max_cost_input": 2.00,
        "use_case": "Complex code, refactoring",
    },
    3: {  # Premium
        "models": [
            "sonnet-4.6", "claude-sonnet",
            "gpt-5", "codex",
            "glm-5.2",  # PPQ version
        ],
        "providers": ["ppq"],
        "max_cost_input": 10.00,
        "use_case": "Architecture, multi-file logic, planning",
    },
    4: {  # Deep Reasoning
        "models": [
            "opus-4.8", "claude-opus",
            "o1", "o3", "o1-mini",
            "reasoning",
        ],
        "providers": ["ppq"],
        "max_cost_input": 50.00,
        "use_case": "Stuck on hard problems, deep logic",
    },
    5: {  # Local fallback
        "models": ["qwen2.5-coder", "llama", "mistral", "codellama"],
        "providers": ["ollama"],
        "max_cost_input": 0,
        "use_case": "Final fallback when all APIs exhausted",
        "is_free": True,
    },
}

# MiniMax models available on PPQ - add scoring heuristics
MINIMAX_MODELS = ["minimax-m2.5", "minimax-m2", "minimax-m2.1", "minimax-m2.7", "minimax-m3", "minimax-m1"]

# z.ai API config
ZAI_UPSTREAM = "https://api.z.ai/api/coding/paas/v4"
ZAI_QUOTA_URL = "https://api.z.ai/api/monitor/usage/quota/limit"

# Provider config
PROVIDERS = {
    "ppq": {
        "base_url": "https://api.ppq.ai/v1",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "models_endpoint": "/models",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "models_endpoint": "/models",
    },
    "routstr": {
        "base_url": "https://api.routstr.com/v1",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "models_endpoint": "/models",
    },
}

# z.ai keys (loaded from .env)
def _load_zai_keys():
    """Load z.ai API keys from .env."""
    keys = {}
    for ep in [Path.home()/".hermes/profiles/manager/.env", Path.home()/".hermes/.env"]:
        if ep.exists():
            for line in ep.read_text(errors="ignore").splitlines():
                line = line.strip()
                if line.startswith("ZAI_API_KEY=") and "friend" not in keys:
                    keys["friend"] = line.split("=",1)[1].split("#")[0].strip().strip("'").strip('"')
                elif line.startswith("ZAI_OUR_KEY=") and "ours" not in keys:
                    keys["ours"] = line.split("=",1)[1].split("#")[0].strip().strip("'").strip('"')
    return keys

ZAI_KEYS = _load_zai_keys()
ZAI_THRESHOLD = 80  # Use PPQ when z.ai quota > 80%

# Quota cache
zai_quota_cache = {}  # name -> (pct, timestamp)

def _fetch_zai_quota(key_name):
    """Fetch z.ai quota for a key."""
    key = ZAI_KEYS.get(key_name)
    if not key:
        return 100  # No key = assume exhausted
    
    try:
        req = urllib.request.Request(ZAI_QUOTA_URL, headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        pcts = [L.get("percentage", 0) for L in data["data"]["limits"] if L.get("type") == "TOKENS_LIMIT"]
        return max(pcts) if pcts else 100
    except Exception:
        return 100  # Error = assume exhausted

def get_zai_quota():
    """Get current z.ai quota status."""
    global zai_quota_cache
    
    now = time.time()
    
    # Check cache (5 min TTL)
    if not zai_quota_cache or now - zai_quota_cache.get("_ts", 0) > 300:
        zai_quota_cache = {
            name: (_fetch_zai_quota(name), now)
            for name in ZAI_KEYS
        }
        zai_quota_cache["_ts"] = now
    
    return {k: v[0] for k, v in zai_quota_cache.items() if k != "_ts"}

def is_zai_available():
    """Check if z.ai quota is available (under threshold)."""
    quota = get_zai_quota()
    # Check if any key is under threshold
    for name, pct in quota.items():
        if pct < ZAI_THRESHOLD:
            return True
    return False

def get_best_zai_key():
    """Get the z.ai key with most quota available."""
    quota = get_zai_quota()
    available = {n: v for n, v in quota.items() if v < ZAI_THRESHOLD}
    if not available:
        return None
    return min(available, key=lambda n: available[n])

lock = threading.Lock()

# Database setup
def _init_db():
    db = sqlite3.connect(str(CACHE_DB), check_same_thread=False)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS prices (
            id INTEGER PRIMARY KEY,
            model_id TEXT,
            provider TEXT,
            input_rate REAL,
            output_rate REAL,
            context_window INTEGER,
            data_json TEXT,
            fetched_at REAL,
            UNIQUE(model_id, provider)
        );
        CREATE TABLE IF NOT EXISTS benchmarks (
            id INTEGER PRIMARY KEY,
            model_id TEXT,
            benchmark_name TEXT,
            score REAL,
            data_json TEXT,
            fetched_at REAL,
            UNIQUE(model_id, benchmark_name)
        );
        CREATE INDEX IF NOT EXISTS idx_prices_model ON prices(model_id);
        CREATE INDEX IF NOT EXISTS idx_benchmarks_model ON benchmarks(model_id);
    """)
    db.commit()
    return db

_db = None

def get_db():
    global _db
    if _db is None:
        _db = _init_db()
    return _db

# Data fetching
def _fetch_ppq_prices():
    """Fetch pricing from PPQ /v1/models endpoint."""
    try:
        req = urllib.request.Request(f"{PPQ_BASE_URL}/models")
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        models = []
        for m in data.get("data", []):
            model_id = m.get("id", "")
            if not model_id:
                continue
            # Parse pricing (PPQ format: input_per_1M_tokens, output_per_1M_tokens)
            pricing = m.get("pricing", {})
            input_rate = float(pricing.get("input_per_1M_tokens", pricing.get("input", 0)) or 0)
            output_rate = float(pricing.get("output_per_1M_tokens", pricing.get("output", 0)) or 0)
            context = m.get("context_length", m.get("context_window", 0)) or 0
            
            models.append({
                "model_id": model_id,
                "provider": "ppq",
                "input_rate": input_rate,
                "output_rate": output_rate,
                "context_window": context,
                "data_json": json.dumps(m),
            })
        return models
    except Exception as e:
        print(f"Error fetching PPQ prices: {e}")
        return []

def _fetch_livebench():
    """Fetch benchmarks from LiveBench."""
    try:
        req = urllib.request.Request(LIVEBENCH_URL)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        
        results = []
        # Parse LiveBench format (varies, but typically has model scores)
        if isinstance(data, dict) and "models" in data:
            for m in data["models"]:
                model_id = m.get("model", m.get("id", ""))
                for bench_name, score in m.items():
                    if bench_name in ("model", "id", "rank"):
                        continue
                    if isinstance(score, (int, float)):
                        results.append({
                            "model_id": model_id,
                            "benchmark_name": f"livebench_{bench_name}",
                            "score": float(score),
                            "data_json": json.dumps(m),
                        })
        elif isinstance(data, list):
            for m in data:
                model_id = m.get("model", m.get("id", ""))
                for bench_name, score in m.items():
                    if bench_name in ("model", "id", "rank"):
                        continue
                    if isinstance(score, (int, float)):
                        results.append({
                            "model_id": model_id,
                            "benchmark_name": f"livebench_{bench_name}",
                            "score": float(score),
                            "data_json": json.dumps(m),
                        })
        return results
    except Exception as e:
        print(f"Error fetching LiveBench: {e}")
        return []

def _fetch_lmsys():
    """Fetch Elo ratings from LMSYS Arena."""
    try:
        req = urllib.request.Request(LMSYS_URL)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        
        results = []
        # LMSYS format varies, but typically has Elo scores
        if isinstance(data, dict) and "models" in data:
            for m in data["models"]:
                model_id = m.get("model", m.get("name", ""))
                elo = m.get("elo", m.get("rating", 0))
                if isinstance(elo, (int, float)):
                    results.append({
                        "model_id": model_id,
                        "benchmark_name": "lmsys_elo",
                        "score": float(elo),
                        "data_json": json.dumps(m),
                    })
        elif isinstance(data, list):
            for m in data:
                model_id = m.get("model", m.get("name", ""))
                elo = m.get("elo", m.get("rating", 0))
                if isinstance(elo, (int, float)):
                    results.append({
                        "model_id": model_id,
                        "benchmark_name": "lmsys_elo",
                        "score": float(elo),
                        "data_json": json.dumps(m),
                    })
        return results
    except Exception as e:
        print(f"Error fetching LMSYS: {e}")
        return []

# Cache management
def _store_prices(prices):
    """Store prices in DB."""
    db = get_db()
    now = time.time()
    for p in prices:
        db.execute("""
            INSERT OR REPLACE INTO prices 
            (model_id, provider, input_rate, output_rate, context_window, data_json, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (p["model_id"], p["provider"], p["input_rate"], p["output_rate"],
              p["context_window"], p["data_json"], now))
    db.commit()

def _store_benchmarks(benchmarks):
    """Store benchmarks in DB."""
    db = get_db()
    now = time.time()
    for b in benchmarks:
        db.execute("""
            INSERT OR REPLACE INTO benchmarks
            (model_id, benchmark_name, score, data_json, fetched_at)
            VALUES (?, ?, ?, ?, ?)
        """, (b["model_id"], b["benchmark_name"], b["score"], b["data_json"], now))
    db.commit()

def _get_cached_prices():
    """Get prices from cache (if fresh)."""
    db = get_db()
    cutoff = time.time() - PRICE_TTL
    rows = db.execute(
        "SELECT model_id, provider, input_rate, output_rate, context_window FROM prices WHERE fetched_at > ?",
        (cutoff,)
    ).fetchall()
    return [{"model_id": r[0], "provider": r[1], "input_rate": r[2], 
             "output_rate": r[3], "context_window": r[4]} for r in rows]

def _get_cached_benchmarks():
    """Get benchmarks from cache (if fresh)."""
    db = get_db()
    cutoff = time.time() - BENCHMARK_TTL
    rows = db.execute(
        "SELECT model_id, benchmark_name, score FROM benchmarks WHERE fetched_at > ?",
        (cutoff,)
    ).fetchall()
    return [{"model_id": r[0], "benchmark_name": r[1], "score": r[2]} for r in rows]

def _refresh_prices():
    """Refresh price cache."""
    prices = _fetch_ppq_prices()
    if prices:
        _store_prices(prices)
    return prices

def _refresh_benchmarks():
    """Refresh benchmark cache."""
    benchmarks = _fetch_livebench() + _fetch_lmsys()
    if benchmarks:
        _store_benchmarks(benchmarks)
    return benchmarks

# Background refresh loop
def _refresh_loop():
    while True:
        try:
            _refresh_prices()
            _refresh_benchmarks()
        except Exception as e:
            print(f"Refresh error: {e}")
        time.sleep(300)  # Every 5 min

# Scoring
def _get_benchmark_score(model_id, task_type):
    """Get weighted benchmark score for a model + task type."""
    db = get_db()
    weights = TASK_BENCHMARKS.get(task_type, TASK_BENCHMARKS["general"])
    
    total_score = 0
    total_weight = 0
    
    # Try multiple model ID variants
    variants = [model_id, model_id.lower(), model_id.split("/")[-1]]
    
    for bench_name, weight in weights.items():
        score = None
        for v in variants:
            row = db.execute(
                "SELECT score FROM benchmarks WHERE model_id LIKE ? AND benchmark_name = ?",
                (f"%{v}%", bench_name)
            ).fetchone()
            if row:
                score = row[0]
                break
        
        if score is not None:
            total_score += score * weight
            total_weight += weight
    
    return total_score / total_weight if total_weight > 0 else 0

def _calculate_bang_for_buck(model, task_type):
    """Calculate value-for-money score."""
    model_lower = model["model_id"].lower()
    
    # Skip non-text models (audio, image generation, video)
    skip_keywords = ["lyria", "whisper", "tts", "audio", "video", "image-gen", "dall-e", "midjourney"]
    if any(kw in model_lower for kw in skip_keywords):
        return 0
    
    bench_score = _get_benchmark_score(model["model_id"], task_type)
    
    cost_per_1m = model["input_rate"] + model["output_rate"]
    if cost_per_1m == 0:
        # Free preview models - give modest score but don't dominate
        return 100
    
    # If no benchmark data, use heuristics based on model name
    if bench_score == 0:
        # Coding task heuristics
        if task_type == "coding":
            if "codex" in model_lower or "coder" in model_lower:
                return 200 / cost_per_1m
            elif "minimax" in model_lower:
                return 120 / cost_per_1m  # M2.5: 2,063 prompts/$
            elif "nano" in model_lower:
                return 100 / cost_per_1m
            elif "mini" in model_lower:
                return 80 / cost_per_1m
            elif "haiku" in model_lower:
                return 90 / cost_per_1m
            elif "flash" in model_lower:
                return 85 / cost_per_1m
            elif "gpt-5" in model_lower:
                return 70 / cost_per_1m
            elif "claude" in model_lower:
                return 75 / cost_per_1m
            elif "glm" in model_lower:
                return 80 / cost_per_1m
            elif "deepseek" in model_lower:
                if "pro" in model_lower:
                    return 120 / cost_per_1m  # Pro: better coding
                return 150 / cost_per_1m  # Flash: best bang-buck
            else:
                return 50 / cost_per_1m
        
        # Reasoning task heuristics
        elif task_type == "reasoning":
            if "opus" in model_lower or "o1" in model_lower or "o3" in model_lower:
                return 200 / cost_per_1m
            elif "sonnet" in model_lower:
                return 150 / cost_per_1m
            elif "minimax" in model_lower:
                return 100 / cost_per_1m
            else:
                return 60 / cost_per_1m
        
        # Math task heuristics
        elif task_type == "math":
            if "o1" in model_lower or "o3" in model_lower:
                return 200 / cost_per_1m
            elif "deepseek" in model_lower:
                return 150 / cost_per_1m
            elif "minimax" in model_lower:
                return 100 / cost_per_1m
            else:
                return 60 / cost_per_1m
        
        # Default: inverse cost heuristic
        return 50 / cost_per_1m
    
    return bench_score / cost_per_1m

def recommend_model(task_type="general", budget=None, priority="balanced", 
                    execution_mode=None, error_retry_count=0):
    """Recommend best model for task type within budget."""
    db = get_db()
    
    # Get cached prices
    prices = _get_cached_prices()
    if not prices:
        prices = _refresh_prices()
    
    if not prices:
        return {"error": "No pricing data available"}
    
    # Score each model
    scored = []
    for p in prices:
        score = _calculate_bang_for_buck(p, task_type)
        if score > 0:
            cost_per_1k = (p["input_rate"] + p["output_rate"]) / 1000
            scored.append({
                "model_id": p["model_id"],
                "provider": p["provider"],
                "score": score,
                "cost_per_1k": cost_per_1k,
                "input_rate": p["input_rate"],
                "output_rate": p["output_rate"],
                "context_window": p["context_window"],
            })
    
    # Sort by score
    scored.sort(key=lambda x: x["score"], reverse=True)
    
    # Filter by budget (if specified)
    if budget is not None:
        scored = [s for s in scored if s["cost_per_1k"] <= budget]
    
    if not scored:
        return {"error": f"No models within budget ${budget}"}
    
    # ── Execution Mode Adjustment ────────────────────────────────────────
    # Plan mode: prefer reasoning-capable models (Claude, o1, GPT codex)
    # Execute mode: prefer fast, cheap models (Haiku, Flash, Nano)
    if execution_mode == "plan":
        # Boost reasoning models
        for s in scored:
            model_lower = s["model_id"].lower()
            if any(m in model_lower for m in ["opus", "o1", "o3", "sonnet", "codex"]):
                s["score"] *= 1.5
            elif any(m in model_lower for m in ["nano", "mini", "flash", "haiku"]):
                s["score"] *= 0.5
        scored.sort(key=lambda x: x["score"], reverse=True)
    
    elif execution_mode == "execute":
        # Boost fast models
        for s in scored:
            model_lower = s["model_id"].lower()
            if any(m in model_lower for m in ["nano", "mini", "flash", "haiku"]):
                s["score"] *= 1.5
            elif any(m in model_lower for m in ["opus", "o1", "o3"]):
                s["score"] *= 0.5
        scored.sort(key=lambda x: x["score"], reverse=True)
    
    # ── Error Escalation ──────────────────────────────────────────────────
    # retry_count 0: standard model
    # retry_count 1: upgrade to Sonnet/GPT-5 level
    # retry_count 2+: upgrade to Opus/o1 reasoning level
    escalation_tier = 0
    if error_retry_count >= 2:
        escalation_tier = 2
        # Force reasoning model
        reasoning_models = [s for s in scored if any(m in s["model_id"].lower() 
                          for m in ["opus", "o1", "o3", "sonnet", "claude"])]
        if reasoning_models:
            scored = reasoning_models
    elif error_retry_count >= 1:
        escalation_tier = 1
        # Upgrade to mid-tier
        mid_models = [s for s in scored if any(m in s["model_id"].lower()
                     for m in ["sonnet", "gpt-5", "claude", "codex"])]
        if mid_models:
            scored = mid_models
    
    # Return top recommendation
    best = scored[0]
    
    # Get provider config
    provider_config = PROVIDERS.get(best["provider"], {})
    
    return {
        "model": best["model_id"],
        "provider": best["provider"],
        "base_url": provider_config.get("base_url", ""),
        "score": best["score"],
        "cost_per_1k": best["cost_per_1k"],
        "context_window": best["context_window"],
        "execution_mode": execution_mode,
        "error_retry_count": error_retry_count,
        "escalation_tier": escalation_tier,
        "reason": f"Best bang-for-buck for {task_type}" + 
                  (f" (plan mode)" if execution_mode == "plan" else "") +
                  (f" (execute mode)" if execution_mode == "execute" else "") +
                  (f", escalated tier {escalation_tier}" if escalation_tier > 0 else ""),
        "alternatives": [
            {"model": s["model_id"], "provider": s["provider"], "score": s["score"]}
            for s in scored[1:4]
        ]
    }

# HTTP Handler
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"ok")
        
        elif self.path == "/models/prices":
            prices = _get_cached_prices()
            if not prices:
                prices = _refresh_prices()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(json.dumps(prices, indent=2).encode())
        
        elif self.path == "/models/benchmarks":
            benchmarks = _get_cached_benchmarks()
            if not benchmarks:
                benchmarks = _refresh_benchmarks()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(json.dumps(benchmarks, indent=2).encode())
        
        elif self.path.startswith("/recommend"):
            # Parse query params
            import urllib.parse
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            
            task_type = params.get("task", ["general"])[0]
            budget_str = params.get("budget", [None])[0]
            priority = params.get("priority", ["balanced"])[0]
            
            budget = float(budget_str) if budget_str else None
            
            result = recommend_model(task_type, budget, priority)
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(json.dumps(result, indent=2).encode())
        
        # System monitoring endpoints
        elif self.path == "/system/resources":
            self._handle_system_resources()
        
        elif self.path.startswith("/system/processes"):
            self._handle_system_processes()
        
        # Session tools endpoints
        elif self.path.startswith("/sessions/"):
            self._handle_session_tools()
        
        elif self.path.startswith("/kanban/"):
            self._handle_kanban()
        
        elif self.path == "/workers/profiles":
            self._handle_workers()
        
        elif self.path == "/quota":
            self._handle_quota()
        
        # Budget endpoint
        elif self.path == "/budget":
            self._handle_budget()
        
        else:
            self.send_response(404)
            self.send_header("Connection", "close")
            self.end_headers()
    
    def _handle_system_resources(self):
        """Get system resources (CPU, RAM, Disk, Swap)."""
        try:
            result = _run_in_threadpool(_get_system_resources)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(json.dumps(result, indent=2).encode())
        except Exception as e:
            self._send_error(500, str(e))
    
    def _handle_system_processes(self):
        """Get top resource-hogging processes."""
        import urllib.parse
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        
        sort_by = params.get("sort_by", ["memory"])[0]
        limit = int(params.get("limit", [5])[0])
        
        try:
            result = _run_in_threadpool(_get_top_processes, sort_by, limit)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(json.dumps(result, indent=2).encode())
        except Exception as e:
            self._send_error(500, str(e))
    
    def _handle_session_tools(self):
        """Proxy to session_tools CLI."""
        import urllib.parse
        parsed = urllib.parse.urlparse(self.path)
        action = parsed.path.replace("/sessions/", "")
        params = urllib.parse.parse_qs(parsed.query)
        
        try:
            result = _run_in_threadpool(_call_session_tool, action, params)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(json.dumps(result, indent=2).encode())
        except Exception as e:
            self._send_error(500, str(e))
    
    def _handle_kanban(self):
        """Get kanban board status."""
        import urllib.parse
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        repo = params.get("repo", [None])[0]
        
        try:
            if repo:
                result = _run_in_threadpool(_call_session_tool, "kanban", {"repo": repo})
            else:
                result = {"error": "Missing repo parameter"}
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(json.dumps(result, indent=2).encode())
        except Exception as e:
            self._send_error(500, str(e))
    
    def _handle_workers(self):
        """Get worker profiles."""
        try:
            result = _run_in_threadpool(_call_session_tool, "profiles")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(json.dumps(result, indent=2).encode())
        except Exception as e:
            self._send_error(500, str(e))
    
    def _handle_quota(self):
        """Get model quota status."""
        try:
            result = _run_in_threadpool(_call_session_tool, "quota")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(json.dumps(result, indent=2).encode())
        except Exception as e:
            self._send_error(500, str(e))
    
    def _handle_budget(self):
        """Get current PPQ budget status."""
        try:
            result = _run_in_threadpool(_get_budget_status)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(json.dumps(result, indent=2).encode())
        except Exception as e:
            self._send_error(500, str(e))
    
    def _send_error(self, code, message):
        """Send error response."""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(json.dumps({"error": message}).encode())

# Thread pool for blocking operations
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="model_selector")

def _run_in_threadpool(func, *args, **kwargs):
    """Run a function in the thread pool and return the result."""
    return _executor.submit(func, *args, **kwargs).result(timeout=10)

# System monitoring helpers
def _get_system_resources() -> dict:
    """Get CPU, RAM, Swap, Disk usage."""
    import psutil, os
    
    cpu_pct = psutil.cpu_percent(interval=0.1)
    virtual_mem = psutil.virtual_memory()
    swap_mem = psutil.swap_memory()
    disk = psutil.disk_usage('/')
    
    return {
        "cpu": {
            "usage_percent": cpu_pct,
            "logical_cores": psutil.cpu_count(logical=True),
            "load_average": os.getloadavg() if hasattr(os, 'getloadavg') else "N/A"
        },
        "ram": {
            "total_gb": round(virtual_mem.total / (1024**3), 2),
            "available_gb": round(virtual_mem.available / (1024**3), 2),
            "used_gb": round(virtual_mem.used / (1024**3), 2),
            "usage_percent": virtual_mem.percent
        },
        "swap": {
            "total_gb": round(swap_mem.total / (1024**3), 2),
            "used_gb": round(swap_mem.used / (1024**3), 2),
            "free_gb": round(swap_mem.free / (1024**3), 2),
            "usage_percent": swap_mem.percent
        },
        "disk": {
            "total_gb": round(disk.total / (1024**3), 2),
            "used_gb": round(disk.used / (1024**3), 2),
            "free_gb": round(disk.free / (1024**3), 2),
            "usage_percent": disk.percent
        }
    }

def _get_top_processes(sort_by: str = "memory", limit: int = 5) -> list:
    """Get top resource-hogging processes."""
    import psutil
    
    processes = []
    for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent', 'memory_info']):
        try:
            info = proc.info
            info['memory_rss_mb'] = round(info['memory_info'].rss / (1024**2), 2) if info.get('memory_info') else 0
            processes.append(info)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    
    if sort_by.lower() == "cpu":
        processes = sorted(processes, key=lambda p: p['cpu_percent'] or 0, reverse=True)
    else:
        processes = sorted(processes, key=lambda p: p['memory_rss_mb'] or 0, reverse=True)
    
    return [
        {
            "pid": p["pid"],
            "name": p["name"],
            "cpu_percent": p["cpu_percent"],
            "memory_percent": round(p.get('memory_percent', 0), 2),
            "memory_rss_mb": p["memory_rss_mb"]
        }
        for p in processes[:limit]
    ]

# Session tools helpers
def _call_session_tool(action: str, params: dict = None) -> dict:
    """Call session_tools CLI."""
    cmd = ["python3", str(HOME / ".hermes" / "bot" / "session_tools.py"), action]
    
    if params:
        if "session_id" in params and params["session_id"]:
            cmd.extend(["--session", params["session_id"]])
        if "worktree" in params and params["worktree"]:
            cmd.extend(["--worktree", params["worktree"]])
        if "repo" in params and params["repo"]:
            cmd.extend(["--repo", params["repo"]])
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return {"error": result.stderr}
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"error": "JSON parse error"}
    except subprocess.TimeoutExpired:
        return {"error": "Timeout"}
    except Exception as e:
        return {"error": str(e)}

# Budget helpers
def _get_budget_status() -> dict:
    """Get current PPQ budget status."""
    budget_file = HOME / ".hermes" / "bot" / "ppq_usage.json"
    
    if not budget_file.exists():
        return {
            "daily_spend": 0.0,
            "daily_limit": 1.00,
            "remaining": 1.00,
            "reset_at": "Next midnight UTC"
        }
    
    try:
        data = json.loads(budget_file.read_text())
        return {
            "daily_spend": data.get("daily_spend", 0.0),
            "daily_limit": data.get("daily_limit", 1.00),
            "remaining": max(0.0, data.get("daily_limit", 1.00) - data.get("daily_spend", 0.0)),
            "reset_at": data.get("reset_at", "Next midnight UTC")
        }
    except Exception as e:
        return {"error": str(e)}


if __name__ == "__main__":
    # Init DB
    get_db()
    
    # Start background refresh
    t = threading.Thread(target=_refresh_loop, daemon=True)
    t.start()
    
    # Initial data fetch
    time.sleep(2)
    _refresh_prices()
    _refresh_benchmarks()
    
    print(f"model_selector on :{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
