# Choosing a code-map tool for AI coding agents: what we found, and how to run your own eval

**Date:** 2026-09-07 · **Status:** bake-off complete, 7-night soak running
**Tools compared:** [graphify](https://github.com/Graphify-Labs/graphify) (PyPI: `graphifyy`) vs [ripwire](https://github.com/redhat-et/ripwire)
**Both Apache-2.0 licensed (graphify dual Apache/MIT).**

## TL;DR

We ran a 34-question, pre-registered bake-off between the two tools on our own
repos, with a plain `grep + read` baseline arm alongside. On our questions —
mostly "which file implements/owns X" — the results were:

| arm | strict file@10 | median answer size | median warm latency |
|---|---|---|---|
| **graphify** (code-only, query/explain) | **24/34 (70.6%)** | **6.6 KB** | 892 ms |
| **ripwire** (`--for`/`--callers`/`--impact`) | 15/34 (44.1%) | 9.7 KB | 328 ms |
| grep + read top file | 33/34 (97%)* | **~1.1 MB (≈160× more)** | 29 ms |

\* upper bound — see the honesty note below.

Three findings matter more than the leaderboard:

1. **A map tool sells cost, not accuracy.** Grepping is very accurate when you
   know the right keyword, but our baseline arm consumed a median 1.1 MB of
   context per answer. The map answers were 6.6–9.7 KB. That is the actual
   product: two orders of magnitude less context for the same answer.
2. **Self-published benchmarks are not transferable.** ripwire's own
   leaderboard has it beating graphify 58.3% to 31.7% (strict file@10,
   LocBench). On our repos, with our question shapes, graphifty won 24 to 15.
   Neither vendor's numbers lied — they measured different corpora and
   question types than ours. Measure on yours.
3. **The scariest failure mode was ours, not the tool's.** graphify's nightly
   index refresh had been timing out at 3600 s for weeks, which made the tool
   look sick. The culprit was its *optional* LLM semantic pass running through
   a rate-limited proxy. In code-only mode (tree-sitter AST, no LLM), the same
   job finished in **79 s for three repos**. Before blaming a tool, strip your
   deployment down to defaults and re-measure.

We wired graphify in (skill + MCP server + code-only nightly refresh) and kept
riprewire installed for a two-week soak: it has real edges — 3× lower query
latency, 2.7–5.6× faster indexing, zero persistent state, and answers that are
never stale (it re-parses per query).

---

## Why we ran this

We run a small fleet of self-hosted coding agents on a single box. Their most
repeated activity when picking up work in a repo is **orientation**: "where is
X handled", "which test covers Y", "what breaks if I change Z". Done naively
(grep a keyword, open the top files), this costs tens to hundreds of KB of
context per question and mis-fires often. Both graphify and ripwire promise to
replace that with a ranked map of the repo at a few KB per question.

Both projects publish benchmarks. Both benchmarks are self-run on their chosen
corpora. We did not trust either, so we built our own gold set and ran both
tools against it.

## Our setup (context, one paragraph)

Single Linux box, gcc 15.2, Python 3.11. Three evaluation repos chosen to span
the axes that matter:

| repo | language | size | character |
|---|---|---|---|
| infrastructure/bot | Python | ~600 files | actively developed daily, our agents' home turf |
| tollgate | Rust | 49 MB, 84 .rs files | clean, single crate family |
| balloon | Python + firmware + assets | 1.9 GB | messy: mixed languages, build artifacts, flight data |

Agent fleet details are out of scope here; the recipe below is repo-tool-only
and works anywhere you can run the two CLIs.

---

## The recipe (copy this)

This is exactly what we did, in the order that keeps it honest. Budget: one
evening for a ≤35-question eval, plus wall-clock for index builds.

### Step 1 — pick your repos

2–3 repos, spanning language, size, and cleanliness. Include at least one that
is actively developed — stale repos make every tool look equally good.

### Step 2 — write gold questions BEFORE running any tool

This is the whole ballgame. Write questions sourced from work you actually did
(recent commits, real bug hunts), with the answer files you know are right.
Do not run either tool first "to see what it finds" — you will anchor.

One JSON line per question (`gold_questions.jsonl`):

```json
{"id": "bot-01", "repo": "bot", "qtype": "where-is",
 "question": "Where is a tier alias resolved to a concrete provider and model?",
 "gold_files": ["flat_router.py"],
 "gold_symbols": ["resolve_tier"], "keywords": "resolve_tier",
 "query_symbol": "resolve_tier"}
```

- `qtype` ∈ orient / where-is / who-calls / blast-radius / tests-cover (2–4
  questions of each type per repo; we did 34 total across 3 repos)
- `gold_files` = the complete set of files a correct answer must name;
  `query_symbol` = a crisp symbol name when the question is really about one
- `keywords` = the term a grep-baseline agent would search; write it honestly
  but remember it partially leaks the gold answer (see Step 8 caveat)

Verify every entry mechanically before running any tool: every gold file must
exist at HEAD, every gold symbol must be findable by grep in its gold file.
We automated this check and it caught 2 errors before they could bias results.

### Step 3 — pre-register the decision metric

Written down before any tool runs; do not move goalposts after. Ours:

- **Primary: strict file@10** — all gold files for a question must appear
  among the first 10 distinct file paths in the tool's answer.
- **Tiebreak 1: bytes-to-answer** (roughly tokens×4) — lower wins.
- **Tiebreak 2: ops** — latency, index time, refresh cost, reliability.
- **Decision rule:** ≥10pp lead in strict file@10 wins outright; within 10pp,
  lower bytes wins; still tied, ops decides.

Sensitivity checks (computed after, reported alongside, never replacing the
pre-registered metric): did the tool name the gold file anywhere in its full
output even past rank 10?

### Step 4 — map each question type to each tool's designated command

Use each tool's documented best-suited mode for the question shape. Our map:

| qtype | ripwire | graphify | grep baseline |
|---|---|---|---|
| orient | `ripwire . --for="…"` | `graphify query "…"` | `rg -i <kw>` + read top file |
| where-is | `ripwire . --for="…"` | `graphify query "…"` | same |
| who-calls (symbol) | `ripwire . --callers=SYM` | `graphify explain SYM` | same |
| blast-radius (symbol) | `ripwire . --impact=SYM` | `graphify explain SYM` | same |
| tests-cover | `ripwire . --for="…"` | `graphify query "…"` | same |

Fix this map before you see any results, and document it.

### Step 5 — make crawl views identical

Both tools already respect `.gitignore` + `.git/info/exclude`. Use that: put
exclusion guards for big non-code dirs (backups, captures, data) in
`.git/info/exclude` of each eval repo so BOTH tools see the same file set.
You are comparing retrieval quality, not crawl resistance.

### Step 6 — code-only mode for everything

- graphify: index with `graphify <repo> --code-only && graphify cluster-only <repo>`,
  then query/explain. `--code-only` skips the optional LLM semantic pass over
  docs — deterministic, reproducible, zero tokens, zero API dependency.
- ripwire: has no LLM pass at all; just run it.

Record index timings (cold). Ours: graphify 25.5 s / 39.5 s / 127.5 s
(tollgate / bot / balloon); ripwire 2.7 s / 2.3 s / 22.7 s. Indexing is
riprewire's clear win, and it needs no persistent index file at all.

### Step 7 — run and score

Runner per question per arm: invoke, capture stdout, measure wall time,
extract file paths. Scoring extraction is ~20 lines of Python: regex the
output for repo-relative paths that actually exist, dedupe preserving order,
take the first 10, compare to gold. Run each query 3× and take the median warm
latency. Save every raw output — you will want to interrogate the misses.

### Step 8 — run an end-to-end agent test (the dress rehearsal)

Score single commands, then run the same questions through a real agent with
one tool cheat-line in the prompt ("here is the tool and its commands; answer
with file paths; you may retry once"). We ran 6 questions × 2 tools. Both
tools scored **5/6 correct** — agent iteration (query, read, adjust, retry)
recovers most single-query misses. Two consequences:

- your eval overstates the difference between tools for agent use;
- a tool's output *shape* (how easily an agent self-corrects from it) may
  matter as much as its top-10 ranking.

**Honesty note on the grep arm.** Our baseline grepped with keywords that
were, in many questions, partly derived from the gold answer (the file or
symbol name). That makes its 97% an upper bound; a real agent starts from
natural-language terms and needs several rounds. Its *cost* number is real,
though — and real agents do worse than one round.

---

## Findings

### Results

| arm | strict file@10 | any@10 | gold anywhere in output | median bytes | median warm ms |
|---|---|---|---|---|---|
| graphify | 24/34 | 24/34 | 25/34 | 6,604 | 892 |
| ripwire | 15/34 | 16/34 | 20/34 | 9,686 | 328 |
| grep | 33/34 | 33/34 | 34/34 | 1,088,731 | 29 |

Per question type (strict): graphify swept **tests-cover 5/5** vs ripwire 1/5;
where-is 15/24 vs 11/24; blast 1/1 vs 0/1; orient even; who-calls even.

### What each tool is actually like

**graphify** builds a persistent graph.json per repo (300–3000+ nodes) and
answers subgraph-shaped questions through BFS over it. It hit 70.6% with the
smallest answers. Its failure mode is *honest*: when asked to explain an
ambiguous name it exits with `Ambiguous: 'X' matches 2 nodes — retry with the
repo-relative path` (1/34 exits). Its test-file coverage answering comes from
community/edge structure — naming the right test file ranked 1st in 5/5 cases.
Costs: persistent index state, per-repo install of a graph, and answers
stale until refreshed (`--update`, 2.4 s incremental on the Rust repo).

**riprewire** re-parses the whole repo per query (2.3–23 s) and streams ranked
symbol XML. It has genuinely better latency for warm repeat queries (0.33 s),
builds no state, and can never be stale. It impressed on precise questions:
asked "who calls resolve_tier" it answered correctly at rank 1 with signature
context. Its weakness on our question shapes was **ranking file-level
answers**: for "which test file covers X" it *contained* the right file in its
output (a weaker "tail" section) but ranked it 11th–29th — first-appearance
order punished it. Zero crashes, and it disclosed parse ambiguity inline.

**Both** survived our hostile-case pass: a file with broken Python syntax
(both still extracted a node from it), empty dirs, unsupported languages, and
a fresh-clone freshness probe (commit a canary function → ripwire: next query
sees it; graphify: needs the explicit `--update`, which took 2.4 s).

### The deployment lesson (worth repeating)

graphify's nightly refresh had been dying at 3600 s for weeks on the 1.9 GB
messy repo, so the tool looked broken. Stripping the deploy to defaults
(`--code-only`) fixed the whole job:** 3 repos, 79 s total, 0 failures.** The
optional semantic pass (LLM over docs/PDFs) is a separate product with
separate costs — do not let it near your nightly unless you have budgeted for
it explicitly.

### Sizing the win

The whole point, quantified: replacing one naive grep-orientation round with
one graphify answer saves ~1 MB of context (≈250 K tokens). Run a handful of
orientation questions per task (conservative) and a map tool pays for its own
maintenance forever.

---

## Honest limitations of this eval

1. **Question-shape bias.** Our gold set is dominated by file-identification
   ("where / which file") because that is what our agents do most. That is
   graphify's home turf. ripwire's change-impact verbs (`--impact`,
   `--from-trace`, `--situ`, `--quality-delta`) were barely exercised — 1 blast
   question and 2 who-calls. If your workload is "what do I touch and what
   breaks", run a gold set shaped like that before concluding.
2. **Small E2E sample.** 6 questions × 2 tool-arms. Directionally interesting
   (both 5/6), statistically weak.
3. **Multi-file gold is hard for everyone.** When a correct answer spans
   sibling files, both tools listed partial sets (our blast question needed
   two files; graphify got one, ripwire got one).
4. **Single-box timings.** Latency numbers are from one loaded Linux box —
   treat as relative, not absolute.
5. **grep baseline asymmetry.** Per Step 8: accuracy upper bound; cost real.

## What we're still checking (open items)

- **7-night soak** (through ~Sep 14): nightly code-only refresh stays under
  budget; the two MCP servers we wired stay healthy inside real agent
  sessions; and **adoption** — do agents actually consult the map instead of
  grepping out of habit (the one assumption the bake-off cannot measure).
- **Day-14 keep-or-uninstall call for ripwire** (~Sep 21): its retention case
  is the zero-staleness + fast callers/impact niche; if we uninstall, it is
  because adoption never touched it.
- Possible later: a budget-capped experiment with graphify's semantic pass
  (excluded here) to see if it buys recall on docs-heavy repos.

## Recommendations if you adopt such a tool

1. **Run the mini-eval first** — 15–35 questions, half an evening, Step 1–8
   above. The per-question ranking you get on your repos will not match either
   vendor's leaderboard.
2. **Start code-only.** No LLM passes, no API keys, no timeouts (see the
   deployment lesson).
3. **Decide your freshness contract.** Persistent graph + scheduled refresh
  (graphify), or re-parse-per-query with zero state (ripwire). Large repos
  with slow query volume favor the former; frequently-changing repos with
  cheap parses favor the latter.
4. **Wire CLI + skill first; MCP later.** The CLI works in any agent that can
  run a shell. MCP (both tools ship a server) gives structured tool calls
  once you know agents will adopt the map.
5. **Keep a grep baseline arm in every future eval you run.** It calibrates
  what the tools must beat on cost, and what they cannot beat on raw accuracy.

## Appendix — versions and reproduction

- graphifyy **0.9.55** (+ `[mcp]` extra), installed via `uv tool install
  "graphifyy[mcp]"`; CLI `graphify`, MCP: `graphify-mcp <path/to/graph.json>`.
  (We started at 0.9.17; upgrading was part of fairness. Package is
  `graphifyy` — double-y — the CLI is `graphify`.)
- ripwire **@ 93c8eda** (2026-09-06 HEAD), Release build, gcc 15.2.0, cmake
  4.2.3. Built from source; its `install.sh` installs the binary and agent
  assets to `~/.local` and only touches agent configs if you explicitly opt
  in — audited and appreciated.
- Index timing table: graphify 25.5 s / 39.5 s / 127.5 s (tollgate / bot /
  balloon, extract + cluster); ripwire 2.7 s / 2.3 s / 22.7 s; graphify
  incremental `--update` 2.4 s.
- Harness layout (in our infra repo): `tools/bakeoff/gold_questions.jsonl`
  (34 questions), `runner.py` (3 arms, scoring, warm timings),
  `phase4_e2e.sh` (agent dress rehearsal), `results/` (per-question raw
  outputs, results.jsonl, results.md decision doc, fullrank sensitivity).
  The plan/replication detail for the full process lives in
  `plans/code-map-bakeoff-2026-09-07.md` (same repo, sibling to this doc).
- E2E agent answers were graded by checking the gold file name in the
  agent's final reply (tail 40 lines), full/partial/miss.

*Questions or want our gold-set JSONL to bootstrap yours? It is 34 lines of
JSON — everything you need is in this doc.*
