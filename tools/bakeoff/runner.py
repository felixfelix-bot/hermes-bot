#!/usr/bin/env python3
"""bake-off runner: ripwire vs graphify vs grep-baseline on our gold set.

Fairness contract (fixed before any results were seen):
- ripwire:      where-is/orient/blast/tests  -> `--for=<question>`
                who-calls (query_symbol)      -> `--callers=<sym>`
- graphify:     all -> `query <question>`; who-calls -> `explain <sym>`
- grep arm:     `rg -i <keywords>` (top 200 lines) + whole top-hit file

All arms run from the same repo roots with identical crawl views (git
ignore rules + .git/info/exclude guards). Code-only semantics everywhere:
no LLM passes in any arm.
"""
import json, os, re, subprocess, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPOS = {
    "bot": Path.home() / ".hermes/bot",
    "tollgate": Path.home() / "repos/tollgate-rs-spilman",
    "balloon": Path.home() / "repos/balloon-fresh",
}
RAW = HERE / "results/raw"
RAW.mkdir(parents=True, exist_ok=True)

RIPEXE = os.environ.get("RIPWIRE_BIN", str(Path.home() / ".local/bin/ripwire"))
GFEXE = "graphify"

PATH_RE = re.compile(r"[\w\-./\\]+\.(?:py|rs|ts|tsx|js|md|yaml|yml|toml|json|sh)")


def run(cmd, cwd, timeout):
    t0 = time.monotonic()
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        ms, out, rc = (time.monotonic() - t0) * 1000, p.stdout, p.returncode
        err = p.stderr[:300]
    except subprocess.TimeoutExpired:
        ms, out, rc, err = timeout * 1000, "", -99, "TIMEOUT"
    return {"ms": round(ms), "bytes": len(out.encode()), "out": out, "rc": rc, "err": err}


def extract_files(text, root):
    """distinct existing repo-relative paths, order of first appearance"""
    seen, files = set(), []
    for m in PATH_RE.finditer(text):
        f = m.group(0).replace("\\", "/").lstrip("./")
        if f in seen or f.startswith("graphify-out"):
            continue
        if (root / f).exists():
            seen.add(f)
            files.append(f)
    return files


def score(gold, files):
    top = files[:10]
    hits = [g for g in gold if g in top]
    return {
        "any10": bool(hits),
        "strict10": bool(hits) and len(hits) == len(gold),
        "first_rank": (top.index(hits[0]) + 1) if hits else None,
        "n_listed": len(top),
    }


def arm_ripwire(q, root):
    sym = q.get("query_symbol")
    if q["qtype"] == "who-calls" and sym:
        cmd = [RIPEXE, ".", f"--callers={sym}"]
    elif q["qtype"] == "blast-radius" and sym:
        cmd = [RIPEXE, ".", f"--impact={sym}"]
    else:
        cmd = [RIPEXE, ".", f"--for={q['question']}"]
    return run(cmd, root, 120), cmd


def arm_graphify(q, root):
    sym = q.get("query_symbol")
    if q["qtype"] == "who-calls" and sym:
        cmd = [GFEXE, "explain", sym]
    elif q["qtype"] == "blast-radius" and sym:
        cmd = [GFEXE, "explain", sym]
    else:
        cmd = [GFEXE, "query", q["question"]]
    return run(cmd, root, 180), cmd


def arm_grep(q, root):
    cmd = ["rg", "-i", "--no-heading", "-n", "-m", "50", q["keywords"], "."]
    r = run(cmd, root, 120)
    # naive follow-through: read the top-hit file whole
    counts = {}
    for line in r["out"].splitlines()[:200]:
        m = re.match(r"([^\s:]+):\d+", line)
        if m:
            f = m.group(1)
            counts[f] = counts.get(f, 0) + 1
    if counts:
        top = max(counts, key=counts.get)
        try:
            r["bytes"] += (root / top).stat().st_size
            with open(root / top, errors="replace") as fh:
                r["out"] += "\n<<<naive-read " + top + ">>>\n" + fh.read()
        except OSError:
            pass
    return r, cmd


ARMS = {"ripwire": arm_ripwire, "graphify": arm_graphify, "grep": arm_grep}


def main():
    qs = [json.loads(l) for l in open(HERE / "gold_questions.jsonl") if l.strip()]
    warmups = int(os.environ.get("WARMUP runs", "2").split()[-1]) if "WARMUP runs" in os.environ else 2
    results = []
    for q in qs:
        root = REPOS[q["repo"]]
        for name, fn in ARMS.items():
            r, cmd = fn(q, root)
            # warm timings (median of 3) — only if the cold run succeeded
            times = [r["ms"]]
            if r["rc"] == 0:
                for _ in range(2):
                    r2, _ = fn(q, root)
                    times.append(r2["ms"])
            times.sort()
            files = extract_files(r["out"], root)
            row = {
                "id": q["id"], "repo": q["repo"], "qtype": q["qtype"], "arm": name,
                "rc": r["rc"], "cold_ms": r["ms"], "warm_ms": times[len(times)//2],
                "bytes": r["bytes"], "cmd": " ".join(cmd[:4]) + " ...",
                **score(q["gold_files"], files),
            }
            results.append(row)
            (RAW / f"{q['id']}.{name}.txt").write_text(
                f"CMD: {' '.join(cmd)}\nRC: {r['rc']}  ERR: {r['err'][:200]}\n\n{r['out'][:60000]}")
            print(f"{q['id']:8s} {name:9s} rc={r['rc']:4d} strict={row['strict10']} "
                  f"rank={row['first_rank']} bytes={r['bytes']:7d} warm_ms={row['warm_ms']}")
    with open(HERE / "results/results.jsonl", "w") as fh:
        for row in results:
            fh.write(json.dumps(row) + "\n")
    print(f"\nwrote {len(results)} rows -> results/results.jsonl")


if __name__ == "__main__":
    main()
