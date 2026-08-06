# ADR-0002: Proactive delivery of standups & summaries

**Status:** Accepted, §4 amended · **Date:** 2026-07-02 · **Amended:** 2026-07-24

> **Amendment (2026-07-24).** The MVP (§3) shipped as written. The extension
> point in §4 did **not**: email landed as `app/delivery/email_delivery.py` plus
> an `email_preferences` table, with **no** `dispatch.py` and no `deliver()`
> seam. That was the right call and this ADR's own §2 predicted it — a
> dispatcher fanning out to a single push channel is still an abstraction with
> one caller. §4 stands as the shape to adopt when a *second* push channel
> (webhook or Teams) actually exists, not as a description of the code today.

## Context

Standups and summaries generate on demand — the user has to open the app to
see them. The product's highest-frequency job (daily standup prep) needs the
content to come *to* the user. Delivery has several possible channels (Teams,
email, browser push, outgoing webhook, the desktop agent), and they are not
alternatives — they are sinks for the same generated text.

Constraint: **Microsoft Teams is unavailable** — no org permission for the bot
registration / Graph consent it requires (Graph scopes are already stubbed out
in `oauth.py`). So the existing `app/delivery/teams_delivery.py` cannot be the
first working channel.

## Decision

**Ship an agent-toast MVP first; defer the multi-channel seam until a second
(push) channel actually exists.**

1. **Delivery is channel-based, one content source → many sinks.** The
   long-term shape is a single `deliver(profile, content)` seam that fans out
   to the user's enabled channels, with per-user preferences. `teams_delivery.py`
   becomes one channel behind it.

2. **But do not build the seam yet.** The first channel — the desktop agent —
   is **pull-based**: the agent already polls the backend, so it *fetches* the
   standup; nothing dispatches server-side. A `deliver()` seam only earns its
   place with a **push** channel (email/webhook/Teams). Building a dispatcher
   with one pull implementation is an abstraction with one caller — the trap
   ADR-0001 warns against.

3. **Agent-toast MVP:**
   - **Scheduled generation** — reuse the hourly-job + local-hour guard pattern
     (`_is_scheduled_time`, ADR-0001's scheduler fix). A `"standup"` case fires
     at each profile's local standup hour (default 09:00), runs the existing
     `_generate()`, stores the standup, and sets `delivery_pending=true`
     (Mongo is schemaless — no migration).
   - **Device-authed endpoints** — `GET /api/agent/standup/pending` and
     `POST /api/agent/standup/ack`, using the existing `_get_device` bearer auth
     (the agent has a device token, not the session cookie).
   - **Toast** — the agent's existing poll loop calls `/pending`; on a hit it
     shows a Windows toast and opens the webview to My Day, then acks.

4. **Extension point (explicit):** the *first push channel* (email) introduces
   `app/delivery/dispatch.py` (`deliver()`) + a `delivery_prefs` table
   (channel, enabled, config). The agent path stays pull; email/webhook/Teams
   register as push channels. The scheduled job — the only caller — changes in
   one place.

## Consequences

- Zero external permissions for the MVP — no bot registration, no Graph consent.
  It rides on the desktop agent already being built.
- Proactive delivery lands at the *correct local time* for free, on top of the
  ADR-0001 timezone + scheduler work.
- Adding email/webhook later is ~one `send()` each, once the seam exists.

## Non-goals (for the MVP)

- The `deliver()` seam and `delivery_prefs` table — deferred to the first push
  channel.
- Email, browser push, outgoing webhook, Teams — later channels.
- Per-user standup-hour UI — default 09:00 local for now.

## Rollout

1. Scheduled standup generation + `delivery_pending` flag (+ tests on the new
   `_is_scheduled_time("standup", …)` case).
2. `/pending` + `/ack` device endpoints (device-auth tests).
3. Agent poll + toast (manual smoke).
4. Email channel — introduces the `deliver()` seam + prefs; webhook/Teams follow.
