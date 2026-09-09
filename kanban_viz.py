#!/usr/bin/env python3
"""kanban_viz.py — Fleet-wide kanban throughput + per-task cost/token plots.

Aggregates every board under ~/.hermes/kanban/boards/*/kanban.db into:

  V1 kanban-burnup.png        cumulative scheduled (created) vs completed,
                              shaded gap = still open (pending).
  V2 kanban-state-stacked.png per-day stacked area: queued (not started) and
                              active (started, unfinished) work + completed line.
  V3 kanban-throughput.png    completed per week, stacked by top-N boards.
  V4 kanban-cost-per-task.png top tasks by $ and by tokens from the
                              task_cost_rollup (full history, survives the 48h
                              attribution purge).

Also writes kanban-ascii.txt for the daily Signal digest.

Lifecycle semantics:
  * "work" task  = any task whose status is not archived/cancelled (abandoned).
  * "completed"  = status in {done, completed}; timed by completed_at, falling
    back to started_at then created_at (covers the few done tasks with no
    completed_at).
  * queued(day)  = work task created by that day, not started yet.
  * active(day)  = work task started by that day, not yet completed.

Usage:
  python3 kanban_viz.py [--outdir DIR] [--days N]
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np

KANBAN_ROOT = Path.home() / ".hermes" / "kanban" / "boards"
ATTR_DB = Path.home() / ".hermes" / "bot" / "burn_attribution.db"
DEFAULT_OUTDIR = Path.home() / ".hermes" / "viz"

TOP_N_BOARDS = 8
TOP_N_TASKS = 15
DAY_S = 86400.0

# Status semantics
SUCCESS = {"done", "completed"}
# Abandoned/closed-without-completion — excluded from the lifecycle entirely.
ABANDONED = {"archived", "cancelled"}

# Board color palette (deterministic by name; reuse price_viz hues).
_BOARD_COLORS = [
    "#053061", "#4393c3", "#a6d96a", "#1b7837", "#7fbc41", "#b2182b",
    "#fddbc7", "#8c510a", "#762a83", "#01665e", "#f768a1", "#f4a582",
    "#e78ac3", "#ffd92f", "#e5c494", "#377eb8", "#4daf4a", "#984ea3",
    "#ff7f00", "#a65628",
]

# Skip metadata dirs that are not real boards.
_SKIP_DIRS = {"_archived", "archive", "*.db", "_archived"}


def _read_board_json(board_dir: Path) -> dict:
    try:
        return json.loads((board_dir / "board.json").read_text())
    except Exception:
        return {}


def _to_epoch(v) -> float | None:
    """Normalize a board timestamp to unix epoch seconds.

    Boards store created_at/started_at/completed_at inconsistently: some as
    unix epoch numbers, some as ISO strings ('2026-08-18T13:35:28Z') or as
    'YYYY-MM-DD HH:MM:SS' text. Naive datetimes are treated as UTC.
    """
    if v is None:
        return None
    if isinstance(v, (int, float)):
        # guard against numeric strings that are actually years/epoch
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            pass
        s = s.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            # last-ditch: space-separated
            try:
                dt = datetime.strptime(v.strip(), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return None


def scan_boards(root: Path = KANBAN_ROOT) -> tuple[list[dict], dict]:
    """Return (tasks, board_meta) for every board DB under root.

    task row: {board, id, title, status, created_at, started_at, completed_at}
    board_meta: slug -> {slug, name, archived}
    """
    tasks: list[dict] = []
    board_meta: dict[str, dict] = {}
    for dbf in sorted(root.glob("*/kanban.db")):
        slug = dbf.parent.name
        if slug in _SKIP_DIRS:
            continue
        meta = _read_board_json(dbf.parent)
        board_meta[slug] = {
            "slug": slug,
            "name": meta.get("name") or slug,
            "archived": bool(meta.get("archived", False)),
        }
        try:
            con = sqlite3.connect(f"file:{dbf}?mode=ro", uri=True)
            rows = con.execute(
                "SELECT id, title, status, created_at, started_at, completed_at "
                "FROM tasks"
            ).fetchall()
            con.close()
        except Exception:
            continue
        for tid, title, status, ca, sa, f in rows:
            if not status:
                continue
            tasks.append({
                "board": slug,
                "id": str(tid),
                "title": title or "",
                "status": str(status),
                "created_at": _to_epoch(ca),
                "started_at": _to_epoch(sa),
                "completed_at": _to_epoch(f),
            })
    return tasks, board_meta


# ── lifecycle helpers ─────────────────────────────────────────────────────────

def _finish_ts(t: dict) -> float | None:
    """Effective completion timestamp for a success task (see module docstring)."""
    if t["completed_at"] is not None:
        return t["completed_at"]
    if t["started_at"] is not None:
        return t["started_at"]
    return t["created_at"]


def daily_days(tasks: list[dict], end_ts: float | None = None) -> list[float]:
    """Day-start unix timestamps covering the work-task history, UTC midnights."""
    work = [t for t in tasks if t["status"] not in ABANDONED]
    starts = [t["created_at"] for t in work if t["created_at"] is not None]
    lo = int(min(starts) // DAY_S) * DAY_S if starts else \
        int((end_ts or time.time()) // DAY_S) * DAY_S
    hi = int((end_ts or time.time()) // DAY_S) * DAY_S
    days = []
    d = lo
    while d <= hi:
        days.append(d)
        d += DAY_S
    return days


def build_series(tasks: list[dict], days: list[float]) -> dict:
    """Per-day fleet series for the burn-up + state charts.

    Returns dict of numpy arrays aligned to ``days``:
      created (cumulative), completed (cumulative),
      queued, active, open (= queued+active).
    """
    n = len(days)
    created = np.zeros(n)
    completed = np.zeros(n)
    queued = np.zeros(n)
    active = np.zeros(n)

    for t in tasks:
        status = t["status"]
        if status in ABANDONED:
            continue
        ca = t["created_at"]
        if ca is None:
            continue
        c_idx = int((ca - days[0]) // DAY_S)
        if c_idx >= n:
            continue  # created after the window — invisible to this series
        c0 = max(0, c_idx)
        created[c0:] += 1
        if status in SUCCESS:
            f = _finish_ts(t)
            if f is not None:
                f_idx = int((f - days[0]) // DAY_S)
                if f_idx < n:
                    completed[max(0, f_idx):] += 1
            continue  # finished tasks do not sit in queued/active
        # open (in-progress / not started) task
        sa = t["started_at"]
        if sa is None or sa > days[-1] + DAY_S:
            queued[c0:] += 1
            continue
        s_idx = int((sa - days[0]) // DAY_S)
        act0 = max(0, s_idx, c0)  # active can never precede creation
        if c0 < act0 and act0 < n:
            queued[c0:act0] += 1
        if act0 < n:
            active[act0:] += 1
    open_ = queued + active
    return {
        "created": created,
        "completed": completed,
        "queued": queued,
        "active": active,
        "open": open_,
    }


def board_weekly_completed(tasks: list[dict]) -> dict[str, dict[int, int]]:
    """board -> {week_start_ts: completed_count}. Monday-anchored weeks."""
    out: dict[str, dict[int, int]] = {}
    for t in tasks:
        if t["status"] not in SUCCESS:
            continue
        f = _finish_ts(t)
        if f is None:
            continue
        # week start (Monday 00:00 UTC)
        dt = datetime.fromtimestamp(f, tz=timezone.utc)
        monday = (dt.timestamp() - dt.weekday() * DAY_S) // DAY_S * DAY_S
        out.setdefault(t["board"], {}).setdefault(monday, 0)
        out[t["board"]][monday] += 1
    return out


# ── cost layer ────────────────────────────────────────────────────────────────

def load_task_cost(attr_db: Path = ATTR_DB) -> list[dict]:
    """Per (board, task) lifetime $/tokens from task_cost_rollup."""
    if not attr_db.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{attr_db}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT board, task_id, SUM(cost), SUM(tokens) FROM task_cost_rollup "
            "WHERE board IS NOT NULL AND task_id IS NOT NULL "
            "GROUP BY board, task_id"
        ).fetchall()
        con.close()
    except Exception:
        return []
    return [{"board": r[0], "task_id": r[1],
             "cost": float(r[2] or 0), "tokens": float(r[3] or 0)} for r in rows]


# ── renderers ─────────────────────────────────────────────────────────────────

def _fmt_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _color_for(board: str, taken: set[str]) -> str:
    idx = sum(ord(c) for c in board) % len(_BOARD_COLORS)
    for off in range(len(_BOARD_COLORS)):
        c = _BOARD_COLORS[(idx + off) % len(_BOARD_COLORS)]
        if c not in taken:
            return c
    return "#333333"


def render_burnup(tasks: list[dict], days: list[float], outdir: Path,
                  title_note: str = "") -> Path:
    s = build_series(tasks, days)
    x = [datetime.fromtimestamp(d, tz=timezone.utc) for d in days]

    fig, ax = plt.subplots(figsize=(14, 6))
    ax.plot(x, s["created"], color="#1f77b4", linewidth=2, label="scheduled (created)")
    ax.plot(x, s["completed"], color="#2ca02c", linewidth=2, label="completed")
    ax.fill_between(x, s["completed"], s["created"], color="#d62728", alpha=0.15,
                    label=f"open (pending) — now {int(s['open'][-1])}")
    ax.set_ylabel("tasks (cumulative)")
    ax.set_title(f"Fleet Kanban Burn-up — scheduled vs completed{title_note}")
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(True, alpha=0.25)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    fig.autofmt_xdate()
    out = outdir / "kanban-burnup.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return out


def render_state_stacked(tasks: list[dict], days: list[float], outdir: Path,
                         title_note: str = "") -> Path:
    s = build_series(tasks, days)
    x = [datetime.fromtimestamp(d, tz=timezone.utc) for d in days]

    fig, ax = plt.subplots(figsize=(14, 6))
    ax.fill_between(x, 0, s["queued"], color="#f7a83e", alpha=0.55,
                    label="queued (created, not started)")
    ax.fill_between(x, s["queued"], s["queued"] + s["active"], color="#5b9bd5",
                    alpha=0.55, label="in-flight (started, not completed)")
    ax.plot(x, s["completed"], color="#2ca02c", linewidth=2,
            label="completed (cumulative)")
    ax.set_ylabel("tasks")
    ax.set_title(f"Kanban Work State over Time — queued vs in-flight{title_note}")
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(True, alpha=0.25)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    fig.autofmt_xdate()
    out = outdir / "kanban-state-stacked.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return out


def render_throughput(tasks: list[dict], outdir: Path,
                      top_n: int = TOP_N_BOARDS) -> Path:
    weekly = board_weekly_completed(tasks)
    if not weekly:
        # Degenerate: no data — still write an empty-labeled figure.
        fig, ax = plt.subplots(figsize=(14, 6))
        ax.text(0.5, 0.5, "No completed tasks in history", ha="center",
                va="center", transform=ax.transAxes)
        out = outdir / "kanban-throughput.png"
        fig.savefig(out, dpi=150, bbox_inches="tight")
        plt.close(fig)
        return out

    all_weeks = sorted({w for b in weekly.values() for w in b})
    totals = {b: sum(w.values()) for b, w in weekly.items()}
    top_boards = sorted(totals, key=lambda b: -totals[b])[:top_n]
    other_total = sum(totals.values()) - sum(totals[b] for b in top_boards)

    fig, ax = plt.subplots(figsize=(14, 6))
    x = np.arange(len(all_weeks))
    taken: set[str] = set()
    bottom = np.zeros(len(all_weeks))
    for b in top_boards:
        color = _color_for(b, taken)
        taken.add(color)
        y = np.array([weekly[b].get(w, 0) for w in all_weeks])
        ax.bar(x, y, bottom=bottom, color=color, width=0.9,
               label=f"{b} ({totals[b]})")
        bottom += y
    if other_total:
        weeks_total = np.array([
            sum(weekly[b].get(w, 0) for b in weekly) for w in all_weeks])
        ax.bar(x, weeks_total - bottom, bottom=bottom, color="#bdbdbd",
               width=0.9, label=f"other ({other_total})")
    ax.set_ylabel("tasks completed / week")
    ax.set_title("Completed Tasks per Week by Board")
    ax.set_xticks(x)
    ax.set_xticklabels([_fmt_date(w) for w in all_weeks], rotation=45, fontsize=8)
    ax.legend(loc="upper left", fontsize=8, ncol=2)
    ax.grid(True, axis="y", alpha=0.25)
    fig.tight_layout()
    out = outdir / "kanban-throughput.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return out


def render_cost_per_task(tasks_by_id: dict, cost_rows: list[dict], outdir: Path,
                         top_n: int = TOP_N_TASKS) -> Path:
    """V4: top tasks by $ and by tokens (lifetime, from task_cost_rollup)."""
    if not cost_rows:
        fig, ax = plt.subplots(figsize=(14, 5))
        ax.text(0.5, 0.5, "No task cost data yet (task_cost_rollup empty)",
                ha="center", va="center", transform=ax.transAxes)
        out = outdir / "kanban-cost-per-task.png"
        fig.savefig(out, dpi=150, bbox_inches="tight")
        plt.close(fig)
        return out

    def label(row):
        meta = tasks_by_id.get((row["board"], row["task_id"]))
        title = meta["title"] if meta else ""
        # title truncated, prefixed by board
        short = title[:34] if title else row["task_id"][:12]
        return f"{row['board']}: {short}"

    by_cost = sorted(cost_rows, key=lambda r: -r["cost"])[:top_n]
    by_tok = sorted(cost_rows, key=lambda r: -r["tokens"])[:top_n]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, max(5, 0.34 * top_n + 2)))
    ax1.barh(range(len(by_cost))[::-1], [r["cost"] for r in by_cost], color="#b2182b")
    ax1.set_yticks(range(len(by_cost))[::-1])
    ax1.set_yticklabels([label(r) for r in by_cost], fontsize=8)
    ax1.set_xlabel("$ (lifetime, attributed)")
    ax1.set_title("Top Tasks by Cost ($)")

    ax2.barh(range(len(by_tok))[::-1], [r["tokens"] / 1e6 for r in by_tok], color="#1f77b4")
    ax2.set_yticks(range(len(by_tok))[::-1])
    ax2.set_yticklabels([label(r) for r in by_tok], fontsize=8)
    ax2.set_xlabel("tokens (millions)")
    ax2.set_title("Top Tasks by Tokens")
    fig.tight_layout()
    out = outdir / "kanban-cost-per-task.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return out


def render_ascii(tasks: list[dict], days: list[float], cost_rows: list[dict],
                 board_meta: dict, top_n: int = TOP_N_BOARDS) -> str:
    s = build_series(tasks, days)
    open_ = int(s["open"][-1])
    queued = int(s["queued"][-1])
    inflight = int(s["active"][-1])
    done = int(s["completed"][-1])
    work = [t for t in tasks if t["status"] not in ABANDONED]
    boards_with_tasks = len({t["board"] for t in tasks})
    lines = [
        "📋 KANBAN FLEET",
        f"boards_with_tasks={boards_with_tasks} boards={len(board_meta)} "
        f"tasks={len(work)} (excl archived/cancelled)",
        f"completed={done}  open={open_}  queued={queued}  in-flight={inflight}",
        "",
    ]
    # boards by completed
    per_board = {}
    for t in tasks:
        if t["status"] in SUCCESS and _finish_ts(t) is not None:
            per_board[t["board"]] = per_board.get(t["board"], 0) + 1
    ranked = sorted(per_board.items(), key=lambda kv: -kv[1])[:top_n]
    lines.append("Top boards (completed lifetime):")
    for b, n in ranked:
        meta = board_meta.get(b, {})
        name = meta.get("name", b)
        lines.append(f"  {b:<22} {n:>5}")
    # board cost
    if cost_rows:
        bc = {}
        for r in cost_rows:
            bc[r["board"]] = bc.get(r["board"], 0.0) + r["cost"]
        brank = sorted(bc.items(), key=lambda kv: -kv[1])[:top_n]
        lines.append("\nTop boards ($ attributed):")
        for b, c in brank:
            lines.append(f"  {b:<22} ${c:>8.2f}")
        top_task = max(cost_rows, key=lambda r: r["cost"])
        tt_tokens = max(cost_rows, key=lambda r: r["tokens"])
        lines.append(f"  top task by $: {top_task['board']}/{top_task['task_id'][:10]} "
                     f"${top_task['cost']:.2f}")
        lines.append(f"  top task by tokens: {tt_tokens['board']}/"
                     f"{tt_tokens['task_id'][:10]} {tt_tokens['tokens']/1e6:.1f}M")
        lines.append("  (details: kanban-cost-per-task.png)")
    lines.append(f"\nwindow: {_fmt_date(days[0])} → {_fmt_date(days[-1])}")
    return "\n".join(lines)


def render_all(outdir: Path = None, days_back: int = 0) -> list[Path]:
    if outdir is None:
        outdir = DEFAULT_OUTDIR
    outdir.mkdir(parents=True, exist_ok=True)

    tasks, board_meta = scan_boards()
    end_ts = time.time()
    if days_back and days_back > 0:
        lo = int((end_ts - days_back * DAY_S) // DAY_S) * DAY_S
    else:
        lo = None
    days = daily_days(tasks, end_ts)
    if lo is not None:
        days = [d for d in days if d >= lo]
    note = "" if days_back == 0 else f" (last {days_back}d)"

    cost_rows = load_task_cost()
    tasks_by_id = {(t["board"], t["id"]): t for t in tasks}

    rendered = []
    try:
        rendered.append(render_burnup(tasks, days, outdir, note))
    except Exception as e:
        print(f"burnup: {e}", file=sys.stderr)
    try:
        rendered.append(render_state_stacked(tasks, days, outdir, note))
    except Exception as e:
        print(f"state-stacked: {e}", file=sys.stderr)
    try:
        rendered.append(render_throughput(tasks, outdir))
    except Exception as e:
        print(f"throughput: {e}", file=sys.stderr)
    try:
        rendered.append(render_cost_per_task(tasks_by_id, cost_rows, outdir))
    except Exception as e:
        print(f"cost-per-task: {e}", file=sys.stderr)

    ascii_text = render_ascii(tasks, days, cost_rows, board_meta)
    (outdir / "kanban-ascii.txt").write_text(ascii_text)
    rendered.append(outdir / "kanban-ascii.txt")
    return rendered


if __name__ == "__main__":
    outdir_arg = None
    days_arg = 0
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        if argv[i].startswith("--outdir="):
            outdir_arg = Path(argv[i].split("=", 1)[1])
        elif argv[i] == "--outdir" and i + 1 < len(argv):
            outdir_arg = Path(argv[i + 1]); i += 1
        elif argv[i].startswith("--days="):
            days_arg = int(argv[i].split("=", 1)[1])
        i += 1

    files = render_all(outdir=outdir_arg, days_back=days_arg)
    print(f"Rendered {len(files)} files:")
    for f in files:
        print(f"  {f}")
    print()
    print((DEFAULT_OUTDIR / "kanban-ascii.txt").read_text() if files else "(none)")
