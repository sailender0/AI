# Developer Activity Tracker — Project Overview

**Last updated:** July 24, 2026  
**Status:** Active development  
**Author:** Sailender Reddy Lanka

---

## 1. What This Is

A personal web application that aggregates your developer activity across **GitHub, GitLab, Jira, and Microsoft Teams** into a single dashboard. It answers the question: *"What did I actually do today/this week?"*

Instead of switching between four tools to reconstruct your work, the app collects all events via webhooks and subscriptions, stores them in a database, and presents them in a unified timeline. An AI layer (`gpt-4.1-mini`) writes daily and weekly summaries and answers natural language questions about your activity.

**Primary use cases:**
- Daily standup prep — what did I do yesterday?
- Sprint retrospective — what did I ship this week?
- Performance review evidence — what have I built over the past month?
- "When did I do X?" — ask AI in plain English

---

## 2. Tech Stack

| Layer | Technology |
|---|---|
| Web framework | FastAPI (async Python) |
| Templating | Jinja2 (server-rendered HTML) |
| Frontend styling | Tailwind CSS, compiled ahead of time to `app/static/app.css` (`npm run css`) — no CDN compiler |
| Activity storage | MongoDB (event + device-telemetry documents) |
| Relational storage | PostgreSQL (users, roles, integrations, AI logs, chat history, devices, email prefs) |
| Session / cache | Redis |
| AI | Azure OpenAI — deployment set by `AZURE_OPENAI_DEPLOYMENT` (currently `gpt-4.1-mini`). Single seam at `app/ai/llm.py`; to change provider see [swap-llm-provider.md](swap-llm-provider.md) |
| Scheduling | APScheduler (in-process, persistent jobs) |
| Real-time | WebSockets (custom `ConnectionManager`) |
| Auth | Microsoft SSO (MSAL) + roles/permissions (`app/auth/rbac.py`) |
| Desktop agent | PyInstaller exe (`agent/`), device-token authed, built by `build.ps1` |
| Deployment | Docker Compose — `app`, `postgres`, `mongodb`, `redis` (+ `adminer` / `mongo-express` from the gitignored override) |

---

## 3. Architecture

### Data Flow — How Events Get In

```
GitHub push / PR
    → GitHub App webhook → POST /webhook/github
        → normalizer.py  (strip PII, extract fields)
            → MongoDB  activity_events collection
                → WebSocket broadcast → browser updates live

Jira ticket update
    → Atlassian webhook → POST /webhook/jira
        → normalizer.py
            → MongoDB

Teams message sent
    → Microsoft Graph subscription → POST /webhook/teams
        → normalizer.py
            → MongoDB

GitLab commit / MR
    → GitLab webhook → POST /webhook/gitlab
        → normalizer.py
            → MongoDB
```

### MongoDB — `activity_events` Document Shape

```json
{
  "_id": ObjectId,
  "profile_id": "string",
  "source": "github | gitlab | jira | teams",
  "event_type": "commit | push | pr_opened | pr_merged | issue_updated | comment | meeting | ...",
  "occurred_at": ISODate,
  "title": "string",
  "repo": "org/repo-name",
  "sha": "abc1234",
  "files": ["auth.py", "models.py"],
  "branch": "feature/auth",
  "raw": { ... }
}
```

### PostgreSQL Tables

| Table | Purpose |
|---|---|
| `profiles` | User accounts (linked to Microsoft identity). Also holds `role` (`user`/`manager`/`admin`), `permissions` (JSON list), and `manager_id` (self-FK — who a user reports to; `ON DELETE SET NULL`). |
| `integrations` | Per-user connector tokens, webhook IDs, sync status. Single-table inheritance — `source` column is the polymorphic discriminator. Subclasses: `TeamsIntegration`, `GitHubIntegration`, `GitLabIntegration`, `JiraIntegration`. |
| `linked_identities` | Maps provider account IDs (e.g. Jira account_id, GitHub org) to a profile |
| `summaries` | AI-generated daily / weekly summary text |
| `query_logs` | Every Ask AI question + filters used + answer |
| `chat_conversations` | Multi-turn conversation sessions |
| `chat_messages` | Individual messages within a conversation |
| `devices` | Registered desktop-agent machines (name, platform, last_seen) |
| `device_tokens` | SHA-256 hashes of agent bearer tokens, one-to-many per device |
| `email_preferences` | Scheduled email digests — `kind` (my_day/my_activity/analytics/standup), frequency, local hour, weekday |
| `alembic_version` | Alembic migration head tracking (managed automatically) |

### MongoDB Collections

`activity_events` is the connector timeline (shape above). The desktop agent and
report features add: `device_heartbeats`, `local_commits`, `ai_tool_events`,
`claude_usage`, `vscode_extensions`, `tool_preferences`, `week_summaries`,
`standups`, `email_sends`, and `access_log` (cross-user report audit trail —
written by `rbac.report_target()` before any other user's data is read).

**Schema management:** Alembic (`alembic/`) handles all schema migrations. `scripts/migrate.py` runs on container startup — detects fresh DB (runs `create_all` + stamps head) vs existing DB (runs `alembic upgrade head`). Never run `create_all` manually.

### Real-Time Updates (`ws_manager.py`)

- `ConnectionManager` holds a dict of `profile_id → [WebSocket, ...]`
- When a webhook fires and a new event is stored, it broadcasts a `{type: "new_event"}` message to all open connections for that profile
- Browser JS in `onWSEvent` re-calls `loadStats()`, `loadChartForWeek()`, `loadTimeline()` — page updates without refresh
- Green **Live** dot in the top bar = connection is open

### AI Layer

Two distinct AI uses:

**1. Summaries** (`app/ai/summarizer.py`)
- `_summarise_profile()` — fetches events for a period from MongoDB, formats them, sends to GPT-4o, stores the text in PostgreSQL `summaries` table
- Called by: (a) APScheduler at 11 PM nightly for daily, Friday for weekly; (b) user clicking Generate on My Day
- Supports `specific_date` param so past-day summaries can be generated on demand

**Module layout.** `app/ai/query.py` is a thin facade — it mounts the sub-routers
and re-exports the helpers tests import. The implementation lives in:

| Module | Holds |
|---|---|
| `context.py` | Shared data-fetch + prompt-building helpers. No routes. |
| `chat.py` | Persistent chat conversations, `/ask/stream` SSE, email-an-answer |
| `tools.py` | Tool-calling prototype (`/api/chat/ask/tools`) |
| `insights.py` | Proactive agent bubble (`/api/agent/insights`) |

**2. Ask AI** (`app/ai/context.py`, `app/ai/chat.py`)
- `_gpt_parse_intent()` — sends the user's question + today's date to the model, gets back JSON `{date_from, date_to, source, event_type}`
- `_intent_to_filter()` — converts that JSON into a MongoDB time range filter
- `_map_event_type()` — normalises event type strings to internal names
- Full chat: history stored per-conversation in PostgreSQL, full history sent with each message for context
- **Period comparison** — "this week vs last week" / "this month vs last month" trigger `_token_comparison_block()`, which fetches **both** periods' Claude token totals (the single-window pipeline can't compare on its own) and injects a comparison block with the delta. The answer points users to the My Activity chart for the visual.

**AI data flow — two hardcoded layers.** The pipeline hardcodes both *what* to fetch (one date window + keyword gates for jira/standup/comparison) and *how* to answer (`instructions.txt` scripts each question type). Every new capability = a fetch branch **+** an instruction paragraph. The tool-calling path below is the experiment to remove that.

**Live Jira snapshot.** Most answers come from stored MongoDB events, but a
question containing a Jira-ish keyword (`jira`, `ticket`, `sprint`, `overdue`, …)
also triggers a live `jira_board.fetch_assigned()` call, injected as a
"currently assigned" block independent of the question's date range. It is
keyword-gated because it costs three Atlassian round-trips.

**3. Ask AI — tool-calling (experimental)** (`app/ai/tools.py`, `app/ai/llm.py`)
- Isolated path at `POST /api/chat/ask/tools` — non-streaming, no conversation persistence — added to A/B against the pre-fetched pipeline without touching the live streaming chat. Instead of Python pre-deciding what to fetch, the model is handed parameterized tools and chooses/composes them.
- `llm.answer_with_tools()` runs the **ask → model calls tool → run it → feed result back → ask again** loop, capped at 4 rounds.
- Four tools, each a thin wrapper over an existing fetch: `get_activity` (activity_events), `get_token_usage` (claude_usage, `group_by=repo|day`), `get_ai_tools` (active minutes per app), `get_focus_time` (heartbeats + local commits). All share one `period` param: `today` / `this_week` / `last_week` / `this_month` / `last_month` / `last_7_days`.
- **Comparison is composition, not a tool** — the model calls the same tool twice with different `period` values and diffs the results itself. This is how a ~4-tool set answers an unbounded set of "compare X to Y" questions.
- **Security:** `profile_id` and `tz` are injected server-side from the session (`Depends(require_profile)`), never from the model's tool args — the per-user boundary the pipeline relies on is preserved.
- **Data caveat:** token counts exist only for claude-code; cross-app comparison (`get_ai_tools`) is by active *time*, not tokens.

**4. Proactive agent bubble** (`app/ai/insights.py`)
- `GET /api/agent/insights` — the floating widget on every page. Not a chat: it
  returns a one-line LLM digest plus "attention cards" (overdue Jira, stale PRs,
  …), each a link to the page that resolves it. Server-caches the payload for
  10 minutes; `?fresh=1` bypasses. Full chat still lives at `/ai`.

**Token costs:**
- Priced from `AZURE_OPENAI_PRICE_IN` / `AZURE_OPENAI_PRICE_OUT` (USD per 1M
  tokens), **not** hardcoded — `llm._estimate_cost()` reads them. Defaults
  0.40 / 1.60 match `gpt-4.1-mini`. **Update both when you change the deployment
  or the provider** ([swap-llm-provider.md](swap-llm-provider.md)), or every
  logged cost is silently wrong.
- All calls log `prompt_tokens`, `completion_tokens`, `total_tokens`, and `$cost`.

### Scheduling (APScheduler)

- Runs **in-process** — only fires when the app is running
- **11 PM nightly** (user's local timezone) → daily summary for each profile
- **11 PM Friday** → weekly summary for each profile
- **Startup catch-up** (`run_startup_catchup()`) — runs once on every boot via `asyncio.create_task`. Generates yesterday's daily summary and last week's weekly summary for each profile if they don't already exist. Covers the most common offline gap (app restarted the next morning).
- **Remaining gap:** if app is offline for multiple consecutive days, only yesterday is regenerated. User must click Generate manually on My Day for older dates.

### Authentication

- Microsoft SSO (MSAL) — single sign-on via Azure AD
- On login: Microsoft identity is linked to a `profile` row; Teams Graph subscription is registered automatically
- Session stored in Redis

### Authorization — roles & permissions (`app/auth/rbac.py`)

Three roles, ranked: **user** (own data only, each feature gated by an
admin-toggleable permission) → **manager** (plus: list/report/edit-permissions for
their **direct reports only**, `Profile.manager_id == manager.id`) → **admin**
(plus: any user, change roles, assign managers). (`supervisor` was renamed to
`manager` — the `add_manager_and_attendance` migration updates live rows; historical
audit-log rows keep their literal `supervisor` value.)

- `ALL_PERMISSIONS` = `email_report`, `export_my_day`, `export_analytics`,
  `email_ai_answer`, `consolidated_report`. The first four are granted by default.
  Only **admin** implicitly holds all; a **manager** is gated by their own list too
  (an admin can restrict a manager's own features), but keeps team powers — seeing/
  editing/reporting on their direct reports — through role checks, not permissions.
- **Manager scope is direct reports only** — a manager sees nobody until an admin
  assigns reports. `visible_profiles()` returns the row-set (admin→all, manager→self
  +reports, user→self); `can_edit_permissions()` answers who may edit whose perms.
- **Permission inheritance is by copy, not live union.** A manager's own permission
  list is a **team template**: copied onto a report when the admin assigns them
  (`PATCH …/manager`), and applied in bulk via `POST …/managers/{id}/team-permissions`
  (grant=add, revoke=remove). Per-user edits (admin any, manager their reports) then
  fine-tune each report's own list — removal works because it's per-user truth.
- **Consolidated report** (`/report`, `consolidated_report` perm): a people × days
  grid of event counts, >=3/day = present, last column = days present over all
  calendar days. Row-scoped by role (user→self, manager→reports, admin→anyone);
  cross-user views/downloads audited. (Replaced the earlier AI-narrative version.)
- **`report_target()` is the choke point.** Every report route calls it, and it
  answers "may I run this?" and "for whom?" together — so the permission check
  and the cross-user audit entry can never drift apart. Cross-user reads write
  to `access_log` *before* the data is read.
- **Bootstrap:** `ADMIN_EMAILS` (comma-separated, in `.env`) promotes matching
  emails to `role=admin` at **every** login. Promote-only — it never demotes, so
  it doubles as lockout recovery.

---

## 4. Connectors

### GitHub ✅ Active
- Connection: GitHub App OAuth → installs on user's account/org
- Webhook: `push`, `pull_request`, `pull_request_review` events
- Captures: commit title, 7-char SHA, changed file names, branch, repo, timestamp
- Does NOT capture: code diffs, full file contents
- Permissions: `read:org`, `read:user`, `repo` (read-only)

### GitLab ✅ Active
- Connection: GitLab OAuth → `api`, `read_user` (`api` scope required for webhook registration)
- Webhook: `push`, `merge_request`, `issues`, `note`, `pipeline`, `tag_push` events — one event per commit on push
- Captures: commit title + SHA + changed files, MR/issue title + state, comment text, pipeline status, tag name
- Dedup guard: checks existing hooks before registering — safe to call Sync Webhooks repeatedly
- Does NOT capture: code diffs, full file contents, MR descriptions
- **Connected header**: Sync Webhooks (re-registers without reconnecting) + Disconnect buttons
- **Error state**: if webhook registration fails, `sync_status` is set to `error` and an orange banner is shown on the GitLab page

### Jira ✅ Active
- Connection: Atlassian OAuth → `read:jira-work`, `read:jira-user`, `manage:jira-webhook`
- Webhook: `issue:updated`, `comment_created`, `issue:created` events
- Captures: ticket key, title, event type, field changes, timestamp
- Does NOT capture: ticket description body, attachments
- **Error state**: if webhook POST to Atlassian fails, `sync_status` is set to `error` and an orange banner with a reconnect link is shown on the Jira page

### Microsoft Teams ✅ Active (auto-connect)
- Connection: automatic via Microsoft SSO login — no separate Connect button
- Uses Microsoft Graph subscription (auto-renews)
- Captures: messages **you send** in chats and channels, meetings you attend
- Does NOT capture: messages from other participants, full message body
- If disconnected: sign out and back in to refresh the Microsoft token
- **Error state**: if subscription creation fails, an orange banner is shown on the Teams page

### All webhook endpoints
- Rate-limited to **200 requests/minute per IP** via `slowapi`. Returns HTTP 429 if exceeded. Applies to `/webhook/github`, `/webhook/gitlab`, `/webhook/jira`, `/webhook/teams`, `/webhook/teams/lifecycle`.

---

## 5. Pages

### Dashboard (Overview)
**Route:** `/` or `/dashboard`

- **KPI cards (×4):** total events per tool for the selected week, with % change vs last week
- **Stacked bar chart:** Mon–Sun, each bar segmented by tool colour (orange=GitHub, cyan=Jira, purple=Teams, green=GitLab). Click a bar → My Day for that date. Hover → tooltip breakdown.
- **Week picker:** Current Week button + Pick Week calendar (Mon–Sun rows). Loads chart + KPI cards for selected week.
- **Activity timeline:** Today tab (events grouped by tool + type) / History tab (flat chronological list). Both show SHA chips and file name tags on commits.
- **Live:** WebSocket keeps chart + timeline updated in real time when events arrive.

### My Day
**Route:** `/my-day?date=YYYY-MM-DD`

- **Date navigation:** Left/right arrows, one day at a time. TODAY badge for current date.
- **AI summary:** GPT-4o paragraph. Generate button appears when a day has events and no summary. Works for today AND any past date. Sends `specific_date` to backend so the exact midnight-to-midnight window is used.
- **KPI cards (×4):** per-tool event count for the selected day.
- **Activity timeline:** Events sorted by time, grouped by tool. Commits show SHA chip + file name tags.
- **Status:** Fully working including past-day generation.

### Analytics
**Route:** `/analytics`

- Cross-connector trend view backed by `GET /api/analytics/trend`.
- Exportable to PDF/CSV; the export routes are gated by the `export_analytics`
  permission and go through `report_target()`.

### My Activity (desktop agent data)
**Route:** `/my-activity`, plus `/my-activity/ai-tools`

- Focus blocks, AI-tool active time, and Claude Code token usage — all sourced
  from the desktop agent, not from connectors.
- `is_desktop` gate: true when the logged-in profile has a `devices` row, so the
  page follows the login, not the browser — any browser on any machine shows the
  data once an agent is registered. The AI-tools sub-page redirects back to
  `/my-activity` for profiles with no agent.
- Week tab carries the token comparison card (two bars + ▲/▼ % delta).

### Email Report
**Route:** `/email` — requires the `email_report` permission, else redirects to `/`.

- Preview / send a report to your own inbox now, or schedule a recurring digest
  (`email_preferences`: kind, frequency, local hour, weekday).
- Delivery is delegated Microsoft Graph `Mail.Send` — self-only, no admin consent.

### Admin
**Route:** `/admin` — manager or admin only, else redirects to `/`.

- User list (row-scoped: a manager sees only their reports), role + per-permission
  toggles. Admins can also assign managers ("Assign people to a manager" search),
  edit any user's permissions, and apply/revoke a manager's team template. A manager
  can edit permissions of their own reports only.
- Backed by `/api/admin/users` (+ `PATCH .../role`, `PATCH .../permissions`,
  `DELETE .../{id}`).

### Ask AI
**Route:** `/ai`

- **Conversations panel:** Saved multi-turn chat sessions. + for new, trash to delete.
- **Chat interface:** User bubbles (right/dark), AI bubbles (left, streaming token by token). Full history sent with each message for context.
- **Streaming:** `/api/chat/conversations/{id}/ask/stream` — `StreamingResponse` with `text/event-stream`. Typing dots shown until first token; replaced by live-filling bubble. User message saved to DB before streaming starts; full AI message saved after stream completes.
- **Intent parsing:** GPT-4o extracts `date_from`, `date_to`, `source`, `event_type` from the question. Understands "last Thursday", "this week", "my Jira tickets".
- **Data source:** Stored MongoDB activity events, plus a keyword-gated live Jira
  "assigned to me" snapshot. GitHub/GitLab/Teams are stored events only.
- **Email this answer:** each AI reply carries an ✉ button — previews the exact
  report-styled HTML in a sandboxed iframe, then sends it to your own inbox.
  Gated by the `email_ai_answer` permission.
- **Status:** Scope picker UI was added then removed. Backend `scope` field kept as "today" silent fallback.

### GitHub Page
**Route:** `/github`

- **Today / This Week toggle:** Filters KPI cards to today vs. full week.
- **KPI cards:** Pull Requests, Commits, Reviews, Issues for the period.
- **Line chart with tabs:** Commits / PRs / Issues / Reviews. Each tab shows normalised 0–1 trend for that event type across Mon–Sun.
- **Week Activity panel:** Lists individual events (SHA chip, file tags, timestamp). Top Repos tab ranks repositories by event count.
- **Recent Activity:** Today (grouped by event type, coloured left border) / History (flat list).

### GitLab Page
**Route:** `/gitlab`

- **Connected header:** `● Connected` badge + Sync Webhooks + Disconnect buttons
- **KPI cards (×6):** Commits, Merge Requests, Issues, Comments, Pipelines, Tags — each in its own colour (green, indigo, amber, cyan, purple, pink)
- **Chart tabs (×5):** Commits / MRs / Issues / Comments / Pipelines
- **Event colour coding:** green=commit, indigo=merge_request, amber=issue, cyan=note, purple=pipeline, pink=tag_push — applied to chart lines, badges, and timeline left borders
- **Orange warning banner:** shown if webhook registration failed; directs user to Sync Webhooks

### Jira Page
**Route:** `/jira`

Same layout. Tabs: Created / Updated / Comments. Cyan colour. Week Activity panel shows ticket key + title + event type.

### Teams Page
**Route:** `/teams`

Same layout. Tabs: Messages / Meetings. Purple colour. Only shows activity you generated.

### Help Page
**Route:** `/help`

- **Layout:** Two-panel master-detail. Left nav (220px, scrollable) + right content (full width, scrollable).
- **Left nav sections:**
  - FAQ (default on load)
  - New User: Connect GitHub, Connect GitLab, Connect Jira, Connect Teams
  - Pages: Dashboard, My Day, Digest, Ask AI, GitHub, GitLab, Jira, Teams
- **FAQ:** Accordion — question visible, answer expands on click (`<details>/<summary>`).
- **New User panels:** Numbered step-by-step connection guides + what gets captured + permissions for each connector.
- **Page panels:** Full explanation of every section on each page — KPI cards, chart (with SVG line chart mockup using exact app colours), Week Activity/Top Repos panel, Recent Activity section. Each section has annotated callout list.
- **Charts in help:** SVG line charts (not bar charts) to match the actual connector pages. Dashboard help uses a stacked bar chart mockup.

---

## 6. UI Conventions

| Element | Behaviour |
|---|---|
| SHA chip | 7-char hash badge in tool colour, shown on commit events across all pages |
| File name tags | Small tags listing changed files on commit events |
| Week picker | Calendar with Mon–Sun rows, selectable. Current Week button always resets to present. |
| Today / This Week toggle | On connector pages — filters KPI counts to today vs full week |
| Left border on activity groups | Coloured by tool — orange, cyan, purple, green |
| Live dot | Green dot in top bar = WebSocket open |
| Generate / Regenerate | Shown when a day/week has events. Hidden when no events. |
| % change badges | Green = up vs last week, Red = down, Grey = flat or no prior data |

---

## 7. File Structure (Key Files)

```
alembic/                       — Alembic migration environment
├── env.py                     — Async migration runner (asyncpg); imports all models for autogenerate
├── script.py.mako             — Migration file template
└── versions/                  — Generated migration files
scripts/
└── migrate.py                 — Container startup script: detects DB state, runs create_all or upgrade head
app/
├── main.py                    — FastAPI app, lifespan, scheduler wiring, /health, /setup/github-app
├── config.py                  — Settings (env vars, Azure endpoints)
├── middleware/
│   ├── rate_limit.py          — slowapi Limiter (200 req/min per IP, shared across all webhook routes)
│   └── request_id.py          — Per-request correlation id for the app log
├── routes/                    — HTTP ONLY: parse the request, authorize, call a service,
│   │                            shape the response. Nothing outside main.py may import
│   │                            from here (enforced by tests/test_layering.py).
│   ├── pages.py               — HTML page routes + WebSocket (/ws) — 10 routes, no DB dependency
│   ├── profile.py             — /api/me, /api/profile/timezone
│   ├── activity.py            — /api/events/recent, /api/stats, /api/week-stats, /api/day-data, /api/week-breakdown, /api/analytics/trend
│   ├── stats.py               — /api/github/stats, /api/jira/stats, /api/teams/stats, /api/gitlab/stats
│   ├── summaries.py           — GET/DELETE /api/summaries, POST /api/summaries/generate
│   ├── exports.py             — /api/export/daily-pdf, /api/export/weekly-pdf (+ CSV)
│   ├── standup.py             — /api/standup/* + run_standup_job() scheduler entry
│   ├── email.py               — /api/email/preview, /send, /preferences + run_email_digest_job()
│   ├── backfill.py            — POST /api/backfill/{source}, self-only, 2/hour
│   ├── admin.py               — /api/admin/users (+ role / permissions / delete)
│   └── agent/                 — Desktop-agent surface, device-token authed
│       ├── analytics.py       — /agent/activity/today, /week, /token-comparison
│       ├── ingest.py          — heartbeat, commit, ai-event, claude-usage, standup pending/ack
│       ├── pair.py            — /register, /devices, /status
│       └── _base.py           — _get_device() bearer auth shared by the above
├── services/                  — Business logic. The only layer that talks to storage for
│                                feature data; imported by routes, ai/, and the schedulers.
│   ├── activity_query.py      — Shared async query helpers:
│   │                             get_profile_tz(), week_bounds(), pct(), count(), daily_counts(),
│   │                             top_items(), workspace_breakdown(), get_integrations(),
│   │                             find_events(), serialize_event(), trend_rows(), week_source_stats()
│   ├── device_analytics.py    — Focus blocks, AI-tool active time, Claude tokens:
│   │                             build_activity_today/week(), tool_active_minutes/periods(),
│   │                             aggregate_claude(), period_ranges(), token_total()
│   ├── standup.py             — generate(): the standup for a date, cached in `standups`
│   ├── report_data.py         — fetch_report(kind, ...): the data behind every emailed/exported
│   │                             report; one dispatch table shared by exports.py and email.py
│   ├── email_report.py        — HTML rendering per report kind; escapes first (render_chat, _md_html)
│   ├── export_pdf.py          — fpdf2 PDF rendering for the export routes
│   ├── timezone.py            — THE conversion layer: day_bounds, local_date, now_local (ADR-0001)
│   └── jira_board.py          — fetch_assigned(): live "assigned to me" + connection health probe
├── ai/
│   ├── summarizer.py          — _summarise_profile(), daily/weekly logic, run_startup_catchup()
│   ├── query.py               — Facade: mounts the sub-routers, re-exports helpers for tests
│   ├── context.py             — Shared fetch + prompt-building helpers (no routes)
│   ├── chat.py                — Chat conversations, /ask/stream SSE, email-an-answer
│   ├── tools.py               — Tool-calling prototype (/api/chat/ask/tools)
│   ├── insights.py            — Proactive agent bubble (/api/agent/insights)
│   └── llm.py                 — Azure OpenAI client, answer_with_tools(), token/cost logging
├── backfill/                  — REST gap-filler; see ADR-0003
│   ├── github.py gitlab.py jira.py  — paged fetch + _to_event() → ingest()
│   └── runner.py              — token → fetch → ingest loop, progress log
├── delivery/
│   ├── email_delivery.py      — Delegated Graph Mail.Send (self-only)
│   └── teams_delivery.py      — Dormant; blocked on org consent (ADR-0002)
├── auth/
│   ├── sso.py                 — Microsoft SSO, get_profile_from_session(), ADMIN_EMAILS promotion
│   ├── rbac.py                — Roles/permissions; report_target() is THE authorization choke point
│   ├── oauth.py               — OAuth callbacks; _INTEGRATION_CLASSES factory for typed instantiation
│   ├── token_store.py         — Fernet (key derived from SECRET_KEY) or Key Vault token storage
│   └── github_app.py          — GitHub App setup
├── storage/
│   ├── mongodb.py             — Collection accessors (activity_events, claude_usage, access_log, …)
│   ├── postgres.py            — engine, AsyncSessionLocal, get_db() FastAPI dependency (one session/request)
│   ├── redis_client.py        — Shared async Redis client (sessions, dedup)
│   └── models.py              — SQLAlchemy models: Profile (role + permissions), Integration (STI base)
│                                 + TeamsIntegration / GitHubIntegration / GitLabIntegration /
│                                 JiraIntegration, LinkedIdentity, Summary, QueryLog,
│                                 ChatConversation, ChatMessage, Device, DeviceToken, EmailPreference
├── webhooks/
│   ├── normalizer.py          — Strips PII, maps raw webhook payloads to standard shape
│   ├── registration.py        — auto_register_webhook(); sets sync_status=error on failure
│   ├── renewal.py             — APScheduler background jobs: renew Teams subscriptions, Jira webhooks, GitHub health
│   └── receivers/             — Per-connector webhook receivers (all rate-limited)
├── ws_manager.py              — WebSocket ConnectionManager, broadcast()
└── templates/
    ├── base.html              — Sidebar, theme toggle, Live dot, Help link, agent bubble
    ├── homepage.html          — Logged-out marketing page served at /
    ├── dashboard.html         — Overview page (stacked bar chart, week picker JS)
    ├── my_day.html            — My Day page (date nav, generate for any date)
    ├── analytics.html         — Cross-connector trend page
    ├── my_activity.html       — Desktop-agent data (focus blocks, AI tools, tokens)
    ├── agent_ai_tools.html    — /my-activity/ai-tools detail, desktop-only
    ├── email.html             — Email report preview + schedule (email_report permission)
    ├── admin.html             — Access console (supervisor read-only, admin can edit)
    ├── ai.html                — Ask AI page (conversations, streaming chat, email-an-answer)
    ├── github.html            — GitHub connector page
    ├── gitlab.html            — GitLab connector page (6 KPI, 5 chart tabs, error banners)
    ├── jira.html              — Jira connector page (error banners)
    ├── teams.html             — Teams connector page (subscription error banner)
    └── help.html              — Help page (master-detail, FAQ accordion, SVG charts)
```

---

## 8. Known Gaps / Incomplete / Rough Edges

| Area | Issue | Workaround / Note |
|---|---|---|
| **Event backfill** ✅ Done | ~~Events from when the app was offline are never captured.~~ | `POST /api/backfill/{source}?days=30` (GitHub/GitLab/Jira) replays REST history through the same `ingest()`, idempotent by construction — see ADR-0003. **Teams is still unfilled** (no clean Graph history for our scope). |
| **Scheduling dependency** | APScheduler only fires when the app is running. Startup catch-up regenerates yesterday + last week's summaries, but gaps older than 1 day require manual Generate. | Generate button on My Day lets user trigger for any date. |
| **Teams capture scope** | Only messages *you* send are captured. Other participants in a chat are not recorded. | By design (privacy). |
| **GitHub offline gaps** | If app is down when a push fires, the webhook is not replayed. | GitHub retries for a short window; no guaranteed delivery. |
| **No mobile layout** | UI is desktop-only. No responsive breakpoints. | Personal tool — desktop only. |
| **No multi-user isolation testing** | Only ever tested with a single user. Multi-user scenarios (shared repos) are untested. | `tests/test_isolation_pg.py` covers per-profile query scoping; real multi-user use is still unexercised. |
| **Key Vault token path unused** ✅ Done | ~~`_keyvault_encrypt` stores each token as a new secret and never deletes it — unbounded growth, no rotation.~~ | Removed. `AZURE_KEYVAULT_URL` was never set in any environment, so the branch had never run; `app/auth/token_store.py` is Fernet-only and `azure-keyvault-secrets`/`azure-identity` are dropped. A future key store needs envelope encryption designed in from the start. |
| **`SECRET_KEY` re-keys stored tokens** | The Fernet key is derived from `SECRET_KEY`, so changing it makes every stored connector token undecryptable. | Rotating it forces all users to reconnect GitHub/GitLab/Jira. There is no re-encrypt script. |
| **Chart Y-axis normalisation** | Connector page charts normalise 0–1 relative to busiest day. 1 event all week shows as 1.0. | Tooltips show raw count. |
| **Redis dependency** | App requires Redis for sessions. If Redis is down, all sessions fail. No fallback. | Run Redis as a service alongside the app. |
| **Test suite** ✅ Done | 504 passing (4 skipped) across normalizer, routes, services, RBAC, backfill mapping, email rendering, agent ingest, and layering. Run with `pytest tests/ -q`. The 4 skips are the DB-backed integration tests; they need a non-dev Postgres/Mongo — see the `pg_session` docstring in `tests/conftest.py`. | — |
| **Integration tests never run in CI** | `ci.yml` has no Postgres or Mongo service and never sets `REQUIRE_DB`, so all 4 DB-backed tests skip there permanently — including the cross-tenant chat guard. | Verified manually against a throwaway `activity_tracker_test` DB. Wiring CI needs service containers + `python scripts/migrate.py` + `REQUIRE_DB=1`. |
| **Help page screenshots** | Help page uses HTML/SVG mockups. Mockups may drift as UI evolves. | Update SVG mockups when major UI changes are made. |
| **Connector error surfaces** ✅ Done | ~~GitLab / Jira / Teams error states not surfaced.~~ | OAuth failures redirect with `?error=`; webhook failures set `sync_status=error` and show orange banners. |
| **Webhook rate limiting** ✅ Done | ~~No rate limiting on webhook endpoints.~~ | `slowapi` 200 req/min per IP on all `/webhook/*` routes. |
| **Ask AI streaming** ✅ Done | ~~Responses appear all at once.~~ | SSE streaming via `/ask/stream`; tokens render as they arrive. |
| **Startup catch-up** ✅ Done | ~~Missed summaries silently skipped.~~ | `run_startup_catchup()` regenerates yesterday + last week on every boot. |

---

## 9. Recent Changes

1. **Alembic migrations** — Schema changes are now managed via Alembic (`alembic/env.py` with async SQLAlchemy). `scripts/migrate.py` runs on container startup: fresh DB → `create_all` + `alembic stamp head`; existing DB → `alembic upgrade head`. Docker Compose `command` updated to run the script before starting uvicorn. Never use `create_all` directly for schema changes — write an Alembic migration instead.
2. **Single DB session per request** — Route handlers receive `db: AsyncSession = Depends(get_db)`. `get_db()` yields one session per request and closes it cleanly. `AsyncSessionLocal()` is intentionally used in three places where FastAPI DI is unavailable: APScheduler jobs (`renewal.py`, summarizer), background tasks (`registration.py`), and the OAuth callback (`oauth.py`) — which runs outside the normal request dependency chain. Do not use `AsyncSessionLocal()` in new route handlers; always use `Depends(get_db)`.
3. **Dashboard god file split** — `routes/dashboard.py` (~600 lines) deleted. Routes reorganised by domain into 6 files: `pages.py` (HTML + WebSocket), `profile.py` (user identity), `activity.py` (timeline/stats), `stats.py` (per-connector KPIs), `summaries.py` (AI summaries), `exports.py` (PDF). Shared query logic extracted to `services/activity_query.py`.
4. **Integration model — single-table inheritance** — `Integration` now has `__mapper_args__ = {"polymorphic_on": "source"}`. Four subclasses added: `TeamsIntegration`, `GitHubIntegration`, `GitLabIntegration`, `JiraIntegration` — each with a `polymorphic_identity` matching the `source` value. No DB schema change. `renewal.py` queries now use typed selects (`select(TeamsIntegration)` auto-filters to `source='teams_subscription'`). `oauth.py` uses `_INTEGRATION_CLASSES[app](profile_id=...)` factory instead of `Integration(source=app)`. `registration.py` creates `TeamsIntegration(profile_id=...)` directly.
5. **GitLab — full event type support** — Now captures and displays all 6 event types: commits, merge requests, issues, comments (notes), pipelines, tag pushes. Six KPI cards (3×2 grid), 5 chart tabs, per-type colour coding throughout. Disconnect + Sync Webhooks buttons in connected header.
6. **GitLab — duplicate webhook prevention** — `GET /projects/{id}/hooks` called before `POST`; skips registration if URL already present. Safe to call Sync Webhooks repeatedly.
7. **Startup catch-up for missed summaries** — `run_startup_catchup()` fires on every boot via `asyncio.create_task`. Checks PostgreSQL for yesterday's daily and last week's weekly summary per profile; generates any that are missing.
8. **Ask AI streaming** — New `POST /api/chat/conversations/{id}/ask/stream` endpoint returns `text/event-stream`. Azure OpenAI called with `stream=True`. User message saved before streaming; AI message saved after. Frontend uses `ReadableStream` to render tokens as they arrive; typing dots shown until first token.
9. **Webhook rate limiting** — `slowapi` added (`200/minute` per IP). Applied to all 5 webhook endpoints. `RateLimitExceeded` handler returns HTTP 429. Shared `Limiter` in `app/middleware/rate_limit.py`.
10. **Connector error surfacing** — OAuth callback now detects empty/error token and redirects to `/{app}?error=<code>` instead of silently saving a broken token. `_register_jira` and `_register_gitlab` set `sync_status=error` on failure. `/api/me` now returns `integration_errors: {source: bool}` alongside `integrations`. GitLab, Jira, and Teams pages show orange warning banners for auth failures and webhook registration failures.
11. **Dashboard week picker** — Replaced 7D/1M/3M buttons with a calendar week picker. `setRangeDefault()`, `selectWeek()`, `togglePicker()`, `renderPicker()`.
12. **File names in activity timeline** — SHA chip + file name tags added to Dashboard and all connector page timelines.
13. **Generate for past days** — My Day now shows Generate for any day with events. Backend uses exact midnight-to-midnight window for `specific_date`.
14. **Token / cost logging** — All AI calls log `prompt_tokens`, `completion_tokens`, `total_tokens`, and `$cost`.
15. **Ask AI GPT intent parsing** — Replaced keyword-based parser with `_gpt_parse_intent()`. Understands natural language time references.
16. **Help page** — Built from scratch: master-detail layout, FAQ accordion, all four connector setup guides, page explanations with SVG mockups.
17. **Token usage comparison (this vs last period)** — Ask AI now answers "this week vs last week" / "this month vs last month" for Claude tokens: `_token_comparison_block()` fetches both periods and reports the delta. New endpoint `GET /api/agent/token-comparison?granularity=week|month` backs a comparison card (two bars + ▲/▼ % delta) on the My Activity Week tab. Shared date math (`period_ranges`, `token_total` in `services/device_analytics.py`) is covered by `tests/test_token_comparison.py`.
18. **Ask AI tool-calling prototype** — Isolated `POST /api/chat/ask/tools` where the model chooses and composes 4 parameterized tools instead of receiving a pre-fetched context. `llm.answer_with_tools()` runs the tool loop; `profile_id`/`tz` injected server-side; comparison handled by calling a tool twice. Non-streaming, no persistence — a sandbox to measure latency/cost vs the pipeline before deciding whether to migrate. Period resolver covered by `tests/test_ai_tools_period.py`.
19. **Service layer extracted; routes are HTTP-only** — `app/ai/*` and `routes/email.py` used to import private helpers out of sibling route modules (`routes.agent.analytics._period_ranges`, `routes.exports._fetch_week_stats`, `routes.standup._generate`, `routes.stats.fetch_assigned`), which made route modules the de-facto service layer and forced five lazy imports in `email.py` to dodge an import cycle. That logic moved down into `services/device_analytics.py`, `services/standup.py`, `services/report_data.py` and `services/jira_board.py`; the triplicated event-serialization block in `routes/activity.py` became `activity_query.serialize_event()`. Dependencies now run routes -> services -> storage in one direction only, pinned by `tests/test_layering.py` (no module but `main.py` may import `app.routes`; no new import cycles). No behaviour change — same endpoints, same payloads.
20. **Event backfill (ADR-0003)** — `POST /api/backfill/{source}?days=30` for GitHub/GitLab/Jira. `app/backfill/{github,gitlab,jira}.py` map REST items straight to normalized events (no synthetic webhook payloads) and hand them to the existing `ingest()`, so the unique index on `(profile_id, source, source_event_id, event_type)` deduplicates for free and re-runs are safe. Self-only, `slowapi` 2/hour, runs as a `BackgroundTask`, also auto-fires once on first connect. Correctness rides on `tests/test_backfill_mapping.py`: the mapper must produce the same id/type as `normalize()` does for the same object. Teams is out of scope.
21. **Desktop agent + device telemetry** — PyInstaller tray app in `agent/`, built by `build.ps1`. Pairs via `POST /agent/register` and authenticates with a bearer token stored as a SHA-256 hash in `device_tokens`. Ingests heartbeats, local commits, AI-tool events, Claude Code token usage and VS Code extensions into Mongo. Surfaces as the My Activity pages (focus blocks, per-tool active minutes, token trends).
22. **Email reports** — `/email` page plus `POST /api/email/{preview,send}` and `GET/PUT /api/email/preferences`. Delivery is delegated Microsoft Graph `Mail.Send` — self-only, no admin consent needed. `services/email_report.py` renders each report kind to HTML (escaping first, since event titles and AI text are attacker-influenced); `report_data.fetch_report()` is the single dispatch table shared with the PDF/CSV exports. Scheduled digests run from `email_preferences` via `run_email_digest_job()`.
23. **Roles, permissions and the admin console** — `Profile.role` (`user`/`supervisor`/`admin`) + `Profile.permissions` (JSON). `app/auth/rbac.py` is the only place authorization is decided, and `report_target()` is the choke point every report route calls — it resolves "may I?" and "for whom?" together so the permission check and the `access_log` audit entry cannot drift. `/admin` console lists users and toggles roles/permissions (admin edits, supervisor reads). `ADMIN_EMAILS` promotes at every login — promote-only, so it doubles as lockout recovery.
24. **AI module split + email-an-answer** — `app/ai/query.py` went from ~740 lines to a facade; the implementation now lives in `context.py` (shared fetch/prompt helpers), `chat.py` (conversations, SSE, email-an-answer), `tools.py` (tool-calling prototype) and `insights.py` (agent bubble). Ask AI answers gained an ✉ button that previews the exact report-styled HTML in a sandboxed iframe before sending, gated by the `email_ai_answer` permission.
25. **Floating chat widget → proactive agent bubble** — the base-template widget no longer streams chat. It calls `GET /api/agent/insights` once on load (10-minute server cache, `?fresh=1` to bypass) and renders a one-line digest plus attention cards linking to the page that resolves each item. Full chat stays at `/ai`. An `[x-cloak]` rule was added so an Alpine load error can't strand the panel open.
26. **PR vs review double-counting fix** — `^pr_` matched `pr_review`, so every review incremented both the Pull Requests and Reviews KPIs. Now `^pr_(?!review$)` and `^pr_review$` in both `routes/stats.py` and `github.html`, and PR rows in the feed carry an event-type chip (`pr_opened` → "Opened").

---

## 10. Next Possible Steps

> Not committed — just a parking lot for ideas.

- [x] Add event backfill endpoint (replay missed webhooks from connector APIs) — done for GitHub/GitLab/Jira (ADR-0003); Teams still open
- [ ] Teams backfill — only if Graph historical access proves feasible and permitted
- [x] Add notification system (email/push when weekly summary is ready) — email digests done; browser push still open
- [ ] Screenshot-based help page illustrations (replace SVG mockups with real screenshots)
- [ ] Mobile-responsive layout
- [ ] Redis session fallback (cookie-based fallback if Redis is unavailable)
- [ ] Integration tests for the connector pipelines (especially normalizer + period bounds)
- [ ] Multi-user isolation smoke test before onboarding second user
- [ ] Decide on Ask AI tool-calling: measure latency/cost of `/api/chat/ask/tools` vs the pipeline; if it wins, port to SSE streaming + conversation persistence and retire the keyword gates
- [ ] Capture per-app token counts (only claude-code has them today) if cross-app token comparison is wanted
- [x] Export activity to CSV / PDF for performance review use — both done (`/api/export/{daily,weekly}-{pdf,csv}`)
- [ ] Re-encrypt script for `SECRET_KEY` rotation, so rotating it doesn't force every user to reconnect
- [ ] Envelope encryption for the Key Vault token path before it is ever enabled
