# ADR-0001: Timezone handling

**Status:** Accepted · **Date:** 2026-07-02

## Context

The app is time-heavy — daily/weekly summaries, standups, per-day KPIs, focus
blocks, "yesterday"/"this week" queries — and is moving toward multiple users in
different timezones. Two conversion mechanisms had grown up side by side:

1. `ZoneInfo(profile.timezone)` — IANA name, used server-side.
2. `tz_offset` — the browser's numeric minute offset, sent per request.

They disagreed, and day-boundary math was reimplemented in several places. The
result was off-by-one bugs: e.g. "yesterday" resolving to the wrong date when
`profile.timezone` was unset, because a numeric offset and a stale profile tz
both silently fall back to UTC.

## Decision

**Store UTC, key local time on a per-user IANA timezone, convert only at the
boundaries, through one helper.**

1. **Storage is UTC, always.** Every datetime in Mongo/Postgres is UTC. The one
   exception is `claude_usage.date`, a local-date *string* computed on the
   agent machine (see Consequences).

2. **Per-user timezone is an IANA name** (`"Asia/Kolkata"`), never a numeric
   offset. A fixed offset is wrong across DST and for historical dates.
   **`tz_offset` is banned** and will be removed.

3. **Source of truth:**
   - Interactive requests carry the browser IANA tz
     (`Intl.DateTimeFormat().resolvedOptions().timeZone`) and **write it back to
     `profile.timezone` if changed.**
   - Background jobs (scheduler, summaries, standups) read the stored
     `profile.timezone`. The write-back keeps that copy fresh.

4. **A "day" is local.** `today`/`yesterday`/`this week` are anchored at the
   user's local midnight, then converted to a UTC range for querying. All
   day-boundary math goes through one helper — no feature computes bounds inline.

5. **One conversion layer:** `app/services/timezone.py`. Convert at ingest
   (→UTC), at query/compute (UTC↔local via IANA), and at display (→local). The
   middle of the system only ever sees UTC.

6. **Scheduler is timezone-aware by filtering, not by multiple crons.** The
   daily job runs hourly and generates for each user whose local hour is the
   target hour. (Per-user scheduled jobs are deferred until scale demands them.)

## Consequences

- DST "just works": a local day may be 23–25h, and `zoneinfo` handles it because
  we anchor on local-midnight-via-IANA and never hand-roll offsets.
- `tz_offset` plumbing is removed from routes, request bodies, and JS.
- `claude_usage.date` stays a local-date string for now (killing it needs a data
  migration; agent-tz == user-tz holds for single-user). Reads derive the same
  local date via the helper's `local_date()`.
- A user who changes zones self-heals on their next page load via the write-back.

## Non-goals

- Migrating `claude_usage` to UTC storage.
- Per-user cron jobs.
