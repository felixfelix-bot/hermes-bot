# Tiered session lifecycle — plan + checklist

Date: 2026-09-07 (approved by operator after the state.db recovery +
retention discussion). Extends/completes the recovery work tracked in
`state-db-corruption-recovery-2026-09-06.md`.

## Objective
Stop the fleet's profile state DBs from growing unbounded (the fragility
that corrupted the manager twice), while never losing knowledge:
**hot = full fidelity, warm = lossless archive + lossy digests, cold =
archives rotate, digests forever.**

Approved parameters:
- Hot retention: 30 days (was 90)
- Digests: only sessions with >= 5 messages; input capped ~30K chars;
  generated on the cheapest healthy lane (`tier/coding-worker` alias)
- Cold archives: monthly zstd JSONL, deletable after 180d (manual policy
  for now — no auto-delete of archives)
- auto_prune stays ON at 90d as a NEVER-FIRES backstop (the daily sweep
  owns the 30d cut; nothing reaches 90d unarchived)

## Design

```
daily 03:30 (systemd user timer, lock-guarded):
  for profile in session-lifecycle-profiles.txt:
    1. session_archiver.py --profile <p> --older-than-days 30
       - candidates: sessions older than 30d NOT yet in manifest.jsonl
         (same age criterion as prune_sessions — see checklist mark)
       - export full messages  -> archive/<YYYY-MM>.jsonl (append, one
         jsonl line per session; previous months zstd'd + verified)
       - digests (>=5 msgs)    -> digests.jsonl (LLM via proxy,
         tier/coding-worker; <5 msgs get a zero-cost metadata digest)
       - manifest row per session: {session_id, archived_to, digest, ...}
       - exit != 0 if any EXPORT fails -> sweep stops (no prune this run)
    2. if exit 0: HERMES_PROFILE=<p> hermes sessions prune --older-than
       30d --yes   (FTS triggers clean index rows; transcript files
       swept by hermes itself, issue #3015 behavior)
    3. monthly (day 1): hermes sessions optimize-storage (FTS merge +
       VACUUM) — the DB shrinks only after vacuum
Failure rails: digest failure is retryable (3 attempts then proceeds —
export success is the prune gate, not digest success); auto_prune 90d
backstop catches strays; manifest makes everything idempotent.
```

Owner-monthly-zst rule: month files older than the current month are
compressed once (`zstd --rm`), originals verified against manifest line
counts first.

## Why these numbers (measured 2026-09-07)
- Manager recovered set: 237K msgs/491 MB <30d; 102K/174 MB 30-90d; no
  >90d. worker-base: 77% of content sits in the 30-90d bucket.
- FTS dominance: worker-base = 663 MB file for 76 MB content (trigram
  FTS ~7x content on JSON tool transcripts) -> pruning reclaims more
  than content math suggests (FTS shrinks proportionally + vacuum).
- Estimated fleet win at 30d retention: -3-4 GB workers + ~-2 GB manager
  after vacuum, plus the bloat growth rate stopped.

## Checklist
### Phase 0 — manager DB repair (PRECONDITION, manager mid-outage)
- [x] `hermes sessions repair --check-only` — DB "does not open cleanly"
- [x] `hermes sessions repair` — FAILED (sanctioned path could not
      salvage; "sessions not completely readable"). `sessions recover`
      --inspect-only + --output also failed ("required table sessions is
      not completely readable")
- [x] ROOT CAUSE IDENTIFIED (see below): >4 GB DB corruption in
      high-offset FTS surgery — every corrupted DB tonight was >=4.19 GB,
      every healthy one <1 GB. v2 (5.4 GB) and v3 (5.4 GB, built in
      isolation, UTF-8-verified-clean) both corrupted within ~4 min of
      gateway load; the daily 02:00 db_health_check snap was EXONERATED
      (corruption preceded it). v4 rebuilt at 1.89 GB (<=30d only,
      trigram index empty) has survived 8+ min with real traffic +
      quick_check ok — CONFIRMS the size-dependent trigger.
- [x] v4 = <=30d messages only (237,337 rows / 7,800 sessions), trigram
      triggers dropped (trigram left empty), regular FTS intact. Gateway
      restart; journal clean; voice test "v4-ok"; quick_check ok; real
      traffic flowing (+127 msgs / +3 sessions during soak)
- [x] Manager 30-90d slice preserved in salvage DB (state.db.new,
      lost_and_found) — archiver (session_archiver.py) built for the
      lossless jsonl export; partial run written to profiles/manager/
      archive/ (resumable, idempotent)

### Root-cause record (blocking, needs upstream fix)
- [ ] BOARD: >4 GB state.db corruption — hermes in-place FTS rebuild /
      high-offset page writes produce binary garbage in sqlite_master.
      Evidence: original 6.27 GB (20:27), self-heal 4.19 GB (23:53),
      v2 5.4 GB (01:14), v3 5.4 GB (01:53) — all malformed; workers
      (<1 GB) and root (13 MB) never corrupt. Suspect 32-bit file-offset
      overflow in the FTS surgery path. Mitigation in place: keep hot
      DBs <4 GB (this lifecycle plan's retention does exactly that).
- [ ] BOARD: upstream bug report with the full evidence trail

### Phase 1 — pipeline build
- [ ] Verify prune_sessions age criterion in hermes_state.py (ended_at
      vs started_at vs last-activity) and pin the archiver to it
- [ ] `session_archiver.py` in bot repo (export/zst, digests, manifest)
- [ ] `session-lifecycle-sweep.sh` + `session-lifecycle-profiles.txt`
      (pilot: treasurer, worker-pae, worker-data; manager commented out
      until pilot quality review)
- [ ] systemd user timer daily 03:30 (Persistent=true) + lock
- [ ] Unit-test the archiver against a scratch profile copy (hermetic —
      no live-DB coupling)

### Phase 2 — pilot + rollout
- [ ] Pilot dry-run (counts + sizes + digest samples for review)
- [ ] Pilot real run + prune on the 3 profiles; before/after DB sizes
- [ ] Digest quality review (operator spot-check)
- [ ] Add remaining worker profiles to the sweep list
- [ ] Manager added to sweep list (biggest single archive job: ~4K
      sessions; digest cost est <$10 — flag before first run)
- [ ] Flip the 17 configs still missing `auto_prune: true` (90d backstop)
- [ ] Weekly digest-cost sanity in cost-performance-escalation cron?

### Phase 3 — board follow-ups
- [ ] Board task: trigram-FTS bloat (~7x content) — upstream knob or
      trigram index off for worker profiles? (router-maintenance)
- [ ] Enable the `disk-cleanup` plugin (test/temp transcript junk —
      complementary, covers ephemeral files)
- [ ] 180d cold-archive rotation policy decision (manual for now)
