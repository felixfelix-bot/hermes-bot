#!/usr/bin/env python3
"""session_archiver — tiered-session-lifecycle Phase 1 (2026-09-07).

Lossless warm-archive + lossy digests for sessions older than a retention
cutoff, BEFORE hermes prune deletes them.

Flow per profile:
  1. candidates: sessions older than --older-than-days, not yet in
     <profile>/archive/manifest.jsonl
  2. export full messages  ->  <profile>/archive/YYYY-MM.jsonl (one line per
     session; previous months are zstd'd+verified by --compact-months)
  3. digests (sessions with >= --min-messages-for-digest messages):
     LLM summary via the local proxy (tier alias = cheapest healthy lane);
     smaller sessions get a zero-cost metadata digest
  4. manifest row per session (the prune gate: prune only sessions that
     exist in the manifest)
Exit codes: 0 = complete (or dry-run), 3 = export failure (do NOT prune).

Source modes:
  native        : profile state.db with sessions/messages tables
  salvage       : a .recover lost_and_found DB (rootpgno/nfield layout) —
                  used for the 2026-09-07 manager recovery archive
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PROXY_URL = "http://127.0.0.1:9099/v1/chat/completions"
DIGEST_MODEL = "tier/coding-worker"
DIGEST_PROMPT = (
    "You are archiving an AI agent session for future search. Produce a "
    "compact digest under 150 words: (1) what the session set out to do, "
    "(2) key decisions and outcomes, (3) files/paths/repos touched, (4) "
    "anything durable worth remembering (bugs found, resolutions, "
    "contacts). Plain text, no markdown headers.")

MSG_COLS = ["session_id", "role", "content", "tool_call_id", "tool_calls",
            "tool_name", "timestamp", "token_count", "finish_reason",
            "reasoning"]


def san(v):
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    if isinstance(v, str):
        try:
            v.encode("utf-8")
            return v
        except UnicodeEncodeError:
            return v.encode("utf-8", errors="replace").decode(
                "utf-8", errors="replace")
    return v


def open_ro(path):
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=10)


def load_manifest(manifest_path: Path) -> set:
    done = set()
    if manifest_path.exists():
        with open(manifest_path) as f:
            for line in f:
                try:
                    done.add(json.loads(line)["session_id"])
                except Exception:
                    pass
    return done


def candidates_native(db, cutoff_ts):
    q = ("SELECT s.id, s.source, s.title, MIN(m.timestamp), MAX(m.timestamp), "
         "COUNT(m.id) FROM sessions s LEFT JOIN messages m ON m.session_id = s.id "
         "WHERE COALESCE(s.ended_at, (SELECT MAX(timestamp) FROM messages "
         "WHERE session_id = s.id), s.started_at) < ? "
         "GROUP BY s.id HAVING COUNT(m.id) > 0")
    for row in db.execute(q, (cutoff_ts,)):
        yield {"id": row[0], "source": row[1], "title": row[2],
               "t0": row[3], "t1": row[4], "n": row[5]}


def candidates_salvage(db, cutoff_ts):
    q = ("SELECT c1, MIN(c7), MAX(c7), COUNT(*) FROM lost_and_found "
         "WHERE rootpgno = 5 AND nfield IN (18, 23) AND c7 < ? "
         "GROUP BY c1")
    for row in db.execute(q, (cutoff_ts,)):
        yield {"id": san(row[0]), "source": "salvage", "title": None,
               "t0": row[1], "t1": row[2], "n": row[3]}


def messages_native(db, sid):
    q = "SELECT %s FROM messages WHERE session_id = ? ORDER BY timestamp" \
        % ",".join(MSG_COLS)
    for row in db.execute(q, (sid,)):
        yield {c: san(v) for c, v in zip(MSG_COLS, row)}


def messages_salvage(db, sid):
    q = ("SELECT c1,c2,c3,c4,c5,c6,c7,c8,c9,c10 FROM lost_and_found "
         "WHERE rootpgno = 5 AND nfield IN (18, 23) AND c1 = ? ORDER BY c7")
    for row in db.execute(q, (sid,)):
        yield {c: san(v) for c, v in zip(MSG_COLS, row)}


def make_digest(sess, msgs):
    parts = []
    total = 0
    for m in msgs:
        content = m.get("content") or ""
        snippet = content[:1600]
        total += len(snippet)
        parts.append("%s: %s" % (m.get("role", "?"), snippet))
        if total > 30000:
            keep = parts[: len(parts) // 2] + ["…[middle elided]…"] + parts[-8:]
            parts = keep
            break
    transcript = "\n".join(parts)[:30000]
    body = json.dumps({
        "model": DIGEST_MODEL,
        "messages": [
            {"role": "system", "content": DIGEST_PROMPT},
            {"role": "user", "content": transcript}],
        "max_tokens": 400, "stream": False,
    }).encode()
    req = urllib.request.Request(PROXY_URL, data=body,
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        out = json.loads(r.read())
    text = out["choices"][0]["message"].get("content")
    if not text:
        raise RuntimeError("empty digest response")
    return text.strip()[:6000]


def month_of(ts):
    t = time.localtime(ts or time.time())
    return time.strftime("%Y-%m", t)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--source-db", help="override source DB path")
    ap.add_argument("--source-mode", choices=["native", "salvage"],
                    default="native")
    ap.add_argument("--older-than-days", type=float, default=30)
    ap.add_argument("--min-messages-for-digest", type=int, default=5)
    ap.add_argument("--max-digests", type=int, default=0,
                    help="0 = unlimited; cap for pilots")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-digests", action="store_true")
    args = ap.parse_args()

    profile_dir = Path.home() / ".hermes" / "profiles" / args.profile
    src = args.source_db or str(profile_dir / "state.db")
    archive_dir = profile_dir / "archive"
    manifest_path = archive_dir / "manifest.jsonl"
    digests_path = archive_dir / "digests.jsonl"
    if args.dry_run:
        db = open_ro(src)
        cutoff = time.time() - args.older_than_days * 86400
        cands = list(candidates_native(db, cutoff) if args.source_mode ==
                     "native" else candidates_salvage(db, cutoff))
        done = load_manifest(manifest_path) if manifest_path.exists() else set()
        todo = [c for c in cands if c["id"] not in done]
        print("profile=%s mode=%s candidates=%d already_archived=%d todo=%d"
              % (args.profile, args.source_mode, len(cands), len(done),
                 len(todo)))
        for c in todo[:10]:
            print("  would archive: %s msgs=%d %s"
                  % (c["id"], c["n"], (c.get("title") or "")[:60]))
        return 0

    archive_dir.mkdir(exist_ok=True)
    db = open_ro(src)
    cutoff = time.time() - args.older_than_days * 86400
    gen_c = candidates_native if args.source_mode == "native" else candidates_salvage
    gen_m = messages_native if args.source_mode == "native" else messages_salvage
    done = load_manifest(manifest_path)
    cands = [c for c in gen_c(db, cutoff) if c["id"] not in done]
    print("archiving %d sessions (%d already in manifest)"
          % (len(cands), len(done)), flush=True)

    month_files, digests_done, failures = {}, 0, 0
    for i, c in enumerate(cands):
        sid = c["id"]
        try:
            msgs = list(gen_m(db, sid))
            if not msgs:
                raise RuntimeError("no messages")
            mon = month_of(c["t0"])
            fh = month_files.get(mon)
            if fh is None:
                fh = open(archive_dir / ("%s.jsonl" % mon), "a",
                          encoding="utf-8")
                month_files[mon] = fh
            row = {"session_id": sid, "exported_at": time.time(),
                   "source": c.get("source"), "title": c.get("title"),
                   "message_count": len(msgs), "messages": msgs}
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()
        except Exception as e:
            failures += 1
            print("EXPORT FAIL %s: %s" % (sid, e), file=sys.stderr, flush=True)
            continue
        digest, digest_err = None, None
        if not args.no_digests and c["n"] >= args.min_messages_for_digest:
            if args.max_digests and digests_done >= args.max_digests:
                digest_err = "capped"
            else:
                try:
                    meta = [m for m in msgs if m.get("role") == "assistant"]
                    digest = make_digest(c, msgs)
                except Exception as e:
                    digest_err = str(e)[:200]
        elif not args.no_digests:
            first_user = next((m.get("content") or "")[:200]
                              for m in msgs if m.get("role") == "user")
            digest = ("[meta] %s | %d msgs | %s" %
                      (c.get("title") or c.get("source") or sid, c["n"],
                       first_user))[:600]
        if digest:
            digests_done += 1
            with open(digests_path, "a", encoding="utf-8") as df:
                df.write(json.dumps({
                    "session_id": sid, "started_at": c["t0"],
                    "ended_at": c["t1"], "profile": args.profile,
                    "digest": digest, "generated_at": time.time()},
                    ensure_ascii=False) + "\n")
        with open(manifest_path, "a", encoding="utf-8") as mf:
            mf.write(json.dumps({
                "session_id": sid, "started_at": c["t0"], "ended_at": c["t1"],
                "message_count": c["n"], "archived_to": "%s.jsonl" % mon,
                "digest": bool(digest),
                "digest_error": digest_err,
                "exported_at": time.time()}, ensure_ascii=False) + "\n")
        if (i + 1) % 50 == 0:
            print("  %d/%d (digests=%d failures=%d)"
                  % (i + 1, len(cands), digests_done, failures), flush=True)
    for fh in month_files.values():
        fh.close()
    print("DONE archived=%d digests=%d failures=%d"
          % (len(cands) - failures, digests_done, failures))
    return 3 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
