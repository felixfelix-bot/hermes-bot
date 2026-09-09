# Kanban Fleet + Per-Task Cost Visualization — Plan

Date: 2026-09-09

## Problem

The fleet runs ~99 kanban boards (2,424 tasks since 2026-06-24) but there is no
single picture of throughput over time (scheduled / completed / pending /
in-flight) or of how much each task/board costs. Per-task cost/token attribution
exists in `burn_attribution.db` but is purged to a rolling 48h window, so it has
no history.

## Data sources

- Task lifecycle: per-board `~/.hermes/kanban/boards/*/kanban.db` table `tasks`
  (`created_at`, `started_at`, `completed_at`, `status`) + `board.json`
  (slug/name/archived).
- Per-task cost/tokens: `burn_attribution.db` table `attribution`
  (`board/task_id/profile/kind/cost_share/tokens_share`) — rolling 48h only.

## Solution

1. **Cumulative rollup** (`bot/burn_attribution.py`): add `task_cost_rollup`
   keyed `(day, board, task_id, profile, kind)` with `cost_usd`, `tokens`,
   UPSERTed from each run before the retention purge. Days outside the 48h
   window freeze; recent days get latest-wins refresh. One-time backfill with
   `burn_attribution.py --since 35d`.
2. **`bot/kanban_viz.py`** — mirrors `price_viz.py`: scans boards, builds daily
   time series (scheduled/completed/pending/in-flight + per-status buckets),
   reads the rollup for cost/tokens, renders PNGs + ASCII digest.
3. **Delivery**: hourly cron + append kanban ASCII/PNGs to the daily digest via
   `send-viz-signal.sh`.
4. **Tests**: `bot/tests/test_kanban_viz.py`.

## Decisions (confirmed with operator)

1. Cost/token history = cumulative rollup + 35d backfill.
2. Daily buckets, full history (~2.5 months).
3. Top-N boards + "other" collapsing.
4. PNGs + ASCII appended to the existing daily digest.

## Visualizations

1. `kanban-burnup.png` — cumulative scheduled vs completed, shaded gap = pending.
2. `kanban-state-stacked.png` — per-day stacked area of status buckets
   (pending / in-flight / blocked / review / done / archived).
3. `kanban-throughput.png` — completed per week stacked by top-N boards.
4. `kanban-cost-per-task.png` — bubble scatter ($/task, size=tokens, color=board)
   + top-N $/task & tokens/task bars.
5. ASCII digest block — top boards by completed/$/tokens; open & blocked counts.

## Checklist

- [ ] Write plan markdown (this file)
- [ ] `burn_attribution.py`: add `task_cost_rollup` table + UPSERT before purge
- [ ] Backfill rollup: run `burn_attribution.py --since 35d`
- [ ] `kanban_viz.py`: `scan_boards()` + daily time-series builders
- [ ] `kanban_viz.py`: `load_task_cost()` from `task_cost_rollup`
- [ ] `kanban_viz.py`: renderers burnup / state-stacked / throughput / cost-per-task
- [ ] `kanban_viz.py`: `render_ascii()` digest block + `render_all()` + `__main__`
- [ ] `send-viz-signal.sh`: append kanban ASCII + attach kanban PNGs to digest
- [ ] Crontab: hourly `kanban_viz.py` entry
- [ ] `test_kanban_viz.py`: time-series math, top-N collapsing, rollup idempotency,
      render_all graceful on empty data
- [ ] Run `kanban_viz.py`; verify 4 PNGs + ASCII render
- [ ] Run `test_kanban_viz.py` + viz regression tests
