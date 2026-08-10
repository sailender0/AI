# ADR-0003: Event backfill for offline gaps

**Status:** Accepted · **Date:** 2026-07-09

## Context

Activity enters the system only through live webhooks (`app/webhooks/`). When
the app is offline — restarted, redeployed, or the machine was off — the
webhook fires into a void and that event is lost forever. Connectors do not
replay: GitHub retries for a short window, GitLab/Jira/Teams do not guarantee
redelivery. The result is silent holes in the timeline, and the product's core
promise ("what did I actually do?") becomes *wrong*, not merely incomplete.
This is the single highest-value gap in the app (see `overview.md` §8).

The fix is a **backfill**: pull recent history from each connector's REST API
and feed it through the same ingest path the webhooks use.

The one thing that makes this hard is **idempotency**. Backfill will overlap
with events that *did* arrive live, and a user may run it more than once. It
must not create duplicates.

## Decision

**Reconstruct events from REST and hand them to the existing `ingest()`, whose
dedup already solves the hard part. Do not build a parallel dedup or a
webhook-payload adapter.**

### 1. Dedup is already solved — lean on it entirely

`activity_events` has a unique index on
`(profile_id, source, source_event_id, event_type)` (`storage/mongodb.py`), and
`normalizer.is_duplicate()` checks Redis then Mongo before `ingest()` inserts.
So backfill's *only* correctness requirement is:

> produce events whose `source_event_id` + `event_type` **exactly match** what
> the live webhook produces for the same underlying object.

Get that right and `ingest()` deduplicates for free, re-runs are safe, and the
WebSocket broadcast fires for genuinely new events. Backfill adds **no new
dedup logic**.

### 2. Map REST → normalized event directly; do NOT fake webhook payloads

The existing `normalizer.py` parses *webhook-shaped* dicts (`raw["after"]`,
`raw["pull_request"]["id"]`). REST list-endpoints return different shapes.
Two ways to bridge:

- **A.** Reshape each REST item into a synthetic webhook payload and reuse
  `normalize()`. Rejected — it silently couples backfill to every field
  `_extract_*` reads; a normalizer change breaks backfill invisibly.
- **B.** A small `_to_event()` per source that builds the final normalized
  dict and calls `ingest()`. Chosen — the id/type mapping lives in one obvious
  place per source, and it reuses everything downstream of normalization.

The id/type conventions each `_to_event()` must reproduce:

| Source | object | `source_event_id` | `event_type` |
|---|---|---|---|
| GitHub | commit | commit `sha` | `commit` |
| GitHub | PR / issue | `id` | `pr_opened`/`pr_merged` (from `merged`), `issue_updated` |
| GitLab | commit | `sha` | `push` |
| GitLab | MR / issue / note | `id` | `merge_request`/`issue`/`note` |
| Jira | issue | issue `id` | `jira:issue_updated` |
| Jira | comment | comment `id` | `comment_created` |

These match `normalizer._extract_native_id` / `_map_event_type` exactly, so a
backfilled row collides with its live-webhook twin and is dropped.

### 3. GitHub granularity: per-commit (not per-push)

The live webhook records **one** `commit` event per push, keyed by the head
commit `sha` (`after`). REST `/commits` has no notion of a push. Backfill emits
**one event per commit**, keyed by each commit's `sha`. Consequence: the head
commit dedups cleanly against any live webhook event (same sha), and the
non-head commits in a historical push become new rows — *more* complete than
the webhook, not conflicting. This is the honest, simpler mapping; accept the
slight granularity difference.

### 4. Backfill reconstructs snapshots, not transitions — say so

Webhooks capture *transitions* (`pr_opened`, later `pr_merged` — two rows).
REST returns *current state* (one PR, now merged → one `pr_merged`). A PR
opened and merged entirely within an offline gap backfills as a single
`pr_merged`. This is correct for the product's question and dedups correctly
against live data, but it is **not** a byte-for-byte replay of webhook history.
Documented limitation, not a bug.

### 5. Trigger: manual button + one-shot on first connect. Not startup.

- `POST /api/backfill/{source}?days=30` — session-auth, **self-only**,
  `slowapi`-limited (`2/hour`), runs as a `BackgroundTask`. A "Backfill last 30
  days" button on each connector page.
- Auto-fire **once** right after a connector is first connected (the "installed
  today, want last month" case) — reuses the same runner.
- **Not** on startup: it would hammer four external APIs on every boot for data
  that is almost always already present. `run_startup_catchup()` stays scoped to
  regenerating *summaries*, not re-fetching events.

### 6. Teams is out of scope for v1

The Teams subscription resource (`me/messages`) has no clean historical list
via Graph for our delegated scope; a delta/history pull is a project of its own
and may not be permitted in the tenant at all. Ship GitHub + GitLab + Jira;
document Teams gaps as unfilled. Revisit only if a real need appears.

## Architecture

```
app/backfill/
  __init__.py
  github.py   gitlab.py   jira.py   # fetch (paged) + _to_event()  → ingest()
  runner.py                         # token → fetch → ingest loop, progress log
app/routes/backfill.py              # POST /api/backfill/{source}, self-only, rate-limited
```

Reused, no new dependencies:
- Auth: `oauth.get_valid_token(profile_id, source)` (GH/GL/Jira),
  `sso.acquire_delegated_token` (unused in v1). Repo/project discovery reuses
  the exact calls already in `registration.py`
  (`/orgs/{org}`, `/projects?membership=true`, `accessible-resources`).
- Ingest: `normalizer.ingest` / `is_duplicate` / the unique index / WS broadcast.
- HTTP: `httpx.AsyncClient`, same as `registration.py`.

Bounds: `days` capped server-side (≤ 90); per-source pagination; sequential
across repos with small concurrency; honor rate-limit headers (GitHub 5000/hr
is the binding constraint). On partial failure, ingest what succeeded and log
the rest — a gap-filler must be resumable, never all-or-nothing.

## Correctness check (the one test that matters)

A fixture pair per source: an equivalent webhook payload and the REST item for
the same object. Assert `_to_event(rest_item)` yields the **same**
`source_event_id` and `event_type` as `normalize(webhook_payload)`. That single
assertion is the whole feature's correctness — if it holds, dedup collides and
backfill is idempotent; if it drifts, we get duplicates. This test is pure and
runs offline (no live API), unlike the fetch layer, which needs live tokens and
is verified by manual smoke against a real connector.

## Consequences

- Timeline holes become fillable on demand; the core promise stops being wrong.
- Zero new infrastructure and zero new dependencies — it is fetch glue plus a
  per-source mapper on top of the existing ingest path.
- Re-running backfill is always safe (idempotent by construction).
- The fetch layer cannot be unit-tested in CI (no live connectors); its
  correctness rides on the pure mapper test + manual smoke. Accepted.

## Non-goals (v1)

- Teams backfill (§6).
- Exact transition-level replay (§4 — snapshots only).
- Backfill on startup, or any automatic periodic backfill.
- Backfilling further than the connectors' own list-API windows allow.

## Rollout

1. **Phase 1 — GitHub + GitLab.** Highest event volume, cleanest REST,
   sha-keyed dedup is exact. Route + per-connector button + the mapper tests.
2. **Phase 2 — Jira.** Adds the snapshot caveat and the comment sub-fetch.
3. **Phase 3 — Teams.** Only if Graph historical access proves both feasible
   and permitted; otherwise it stays a documented gap.
