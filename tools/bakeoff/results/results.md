# Bake-off results: ripwire vs graphify vs grep-baseline (2026-09-07)

Pre-registered decision metric: **strict file@10** on 34 gold questions; tiebreak bytes; then ops.

| arm | strict file@10 | any@10 | rc=0 | median bytes | median warm latency |
|---|---|---|---|---|---|
| ripwire | 15/34 (44%) | 16/34 | 34/34 | 9,686 | 328 ms |
| graphify | 24/34 (71%) | 24/34 | 33/34 | 6,604 | 892 ms |
| grep | 33/34 (97%) | 33/34 | 34/34 | 1,088,731 | 29 ms |

Index build (cold, code-only):
| repo | graphify extract+cluster | ripwire parse |
|---|---|---|
| tollgate | 6.5s + 19.0s = 25.5s | 2.7s |
| bot (python) | 19.3s + 20.2s = 39.5s | 2.3s |
| balloon (1.9GB messy) | 71.9s + 55.6s = 127.5s | 22.7s |

Per-question-type (strict):
- where-is: ripwire 11/24, graphify 15/24, grep 23/24
- who-calls: ripwire 1/2, graphify 1/2, grep 2/2
- blast-radius: ripwire 0/1, graphify 1/1, grep 1/1
- tests-cover: ripwire 1/5, graphify 5/5, grep 5/5
- orient: ripwire 2/2, graphify 2/2, grep 2/2

Sensitivity checks (post-hoc, not decision metrics):
- gold-anywhere-in-output (incl. ripwire's weak 'tail' section): graphify 25/34, ripwire 20/34, grep 34/34
- ripwire tests-cover ceiling: even scoring all 5 tests-cover questions via its specialist `--affected` mode, ripwire reaches at most 20/34 (58.8%) < graphify 24/34 (70.6%) — decision cannot flip
- graphify's single rc=1 (tg-09): 'Ambiguous: matches 2 nodes' — an honest disambiguation prompt, recoverable by design

Freshness (Phase 3): ripwire re-parses per query — a commit committed one second before a query is found by the next query. graphify needs an explicit `--update` (2.35s incremental on tollgate; 6s cold).
Robustness (Phase 3): both survive syntax-error files (both extracted a node from broken Python), empty dirs, unsupported languages (.ml) without crashing; ripwire discloses parse-health/unindexed counts, graphify skips silently-or-errors legibly.

## DECISION
**WINNER: graphify** — pre-registered rule satisfied: +26.5pp strict file@10 AND lower median bytes (6.6KB vs 9.7KB).
runner-up notes (ripwire): 2.7x lower warm latency (328ms vs 892ms), no persistent index state, no nightly refresh needed at all; place first for pure who-calls symbol lookups. Its ranked-symbol model answers 'what do I touch' questions with signature detail; on this corpus's file-identification-shaped questions it ranked test files 11-29th.
grep baseline: 33/34 accurate BUT median ~1MB+ per answer (~160x graphify's bytes) — confirms the point of having a map tool at all.

## Versions
- graphifyy 0.9.55 (PyPI, uv tool) — upgraded from installed 0.9.17 for fairness
- ripwire @ 93c8eda (2026-09-06 HEAD, Release build, gcc 15.2)