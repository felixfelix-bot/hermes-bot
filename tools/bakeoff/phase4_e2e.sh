#!/usr/bin/env bash
export PATH=$HOME/.hermes/hermes-agent/venv/bin:$PATH
export HERMES_PROFILE=manager
QS="bot-01 bot-05 bot-06 tg-03 tg-10 bf-02"
for qid in $QS; do
  for arm in ripwire graphify; do
    Q=$(python3 -c "import json;rs=[json.loads(l) for l in open('gold_questions.jsonl')];q=[r for r in rs if r['id']=='$qid'][0];print(q['question'])" 2>/dev/null)
    REPO=$(python3 -c "import json;rs=[json.loads(l) for l in open('gold_questions.jsonl')];q=[r for r in rs if r['id']=='$qid'][0];print({'bot':'$HOME/.hermes/bot','tollgate':'$HOME/repos/tollgate-rs-spilman','balloon':'$HOME/repos/balloon-fresh'}[q['repo']])")
    if [ "$arm" = ripwire ]; then
      TOOLCMDS="riprewire maps a codebase: run 'riprewire <args> .' from the repo root. Use --for=\"<question>\" for task-shaped queries, --callers=SYM for who-calls, --impact=SYM for blast radius. Output is XML with p=\"file\" rows."
    else
      TOOLCMDS="graphify maps a codebase. From inside a repo with a graphify-out/graph.json: 'graphify query \"<question>\"' (subgraph answer), 'graphify explain SYMBOL' (one node's connections). Output lists NODE rows with src=file."
    fi
    OUT="results/e2e/${qid}.${arm}.txt"
    mkdir -p results/e2e
    timeout 240 hermes -z "You are in repo $REPO. $TOOLCMDS Answer this question by running ONE query with the tool, reading its output, and naming the answer file path(s). If the first query misses, adjust ONCE and re-run. Question: $Q Reply with ONLY the file path(s), one per line, no prose." > "$OUT" 2>&1
    echo "== $qid $arm rc=$?:"; tail -3 "$OUT" | head -3
  done
done
