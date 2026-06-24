# Developer Activity Tracker — Project Overview

**Last updated:** June 23, 2026  
**Status:** Active development  
**Author:** Sailender Reddy Lanka

---

## 1. What This Is

A personal web application that aggregates your developer activity across **GitHub, GitLab, Jira, and Microsoft Teams** into a single dashboard. It answers the question: *"What did I actually do today/this week?"*

Instead of switching between four tools to reconstruct your work, the app collects all events via webhooks and subscriptions, stores them in a database, and presents them in a unified timeline. An AI layer (GPT-4o) writes daily and weekly summaries and answers natural language questions about your activity.

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
| Frontend styling | Tailwind CSS (CDN) |
| Activity storage | MongoDB (event documents) |
| Relational storage | PostgreSQL (users, sessions, AI logs, chat history) |
| Session / cache | Redis |
| AI | Azure OpenAI — GPT-4o (`claude-sonnet-4-6` equivalent) |
| Scheduling | APScheduler (in-process, persistent jobs) |
| Real-time | WebSockets (custom `ConnectionManager`) |
| Auth | Microsoft SSO (MSAL) |
| Deployment | Local / self-hosted (Windows, runs as a process) |

---

## 3. Architecture

### Data Flow — How Events Get In

```
GitHub push / PR
    → GitHub App webhook → POST /webhooks/github
        → normalizer.py  (strip PII, extract fields)
            → MongoDB  activity_events collection
                → WebSocket broadcast → browser updates live

Jira ticket update
    → Atlassian webhook → POST /webhooks/jira
        → normalizer.py
            → MongoDB

Teams message sent
    → Microsoft Graph subscription → POST /webhooks/teams
        → normalizer.py
            → MongoDB

GitLab commit / MR
    → GitLab webhook → POST /webhooks/gitlab
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
| `profiles` | User accounts (linked to Microsoft identity) |
| `integrations` | Per-user connector tokens, webhook IDs, sync status. Single-table inheritance — `source` column is the polymorphic discriminator. Subclasses: `TeamsIntegration`, `GitHubIntegration`, `GitLabIntegration`, `JiraIntegration`. |
| `linked_identities` | Maps provider account IDs (e.g. Jira account_id, GitHub org) to a profile |
| `summaries` | AI-generated daily / weekly summary text |
| `query_logs` | Every Ask AI question + filters used + answer |
| `chat_conversations` | Multi-turn conversation sessions |
| `chat_messages` | Individual messages within a conversation |
| `alembic_version` | Alembic migration head tracking (managed automatically) |

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
- Called by: (a) APScheduler at 11 PM nightly for daily, Friday for weekly; (b) user clicking Generate on My Day or Digest
- Supports `specific_date` param so past-day summaries can be generated on demand

**2. Ask AI** (`app/ai/query.py`)
- `_gpt_parse_intent()` — sends the user's question + today's date to GPT-4o, gets back JSON `{date_from, date_to, source, event_type}`
- `_intent_to_filter()` — converts that JSON into a MongoDB time range filter
- `_map_event_type()` — normalises event type strings to internal names
- Full chat: history stored per-conversation in PostgreSQL, full history sent with each message for context

**Token costs (Azure OpenAI GPT-4o):**
- Input: $2.50 / 1M tokens
- Output: $10.00 / 1M tokens
- Typical daily summary: < $0.01
- Typical Ask AI query: ~$0.005
- All calls log token counts and cost to the app log

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

### Digest (Weekly Summary)
**Route:** `/digest`

- **Week navigation:** Left/right arrows across weeks.
- **Weekly summary card:** AI paragraph. Generate if missing, Regenerate to rewrite.
- **Activity breakdown:** Day-by-day list, expandable. Events with SHA/file tags for commits.
- **Auto-generation:** Every Friday at 11 PM if app is running.

### Ask AI
**Route:** `/ai`

- **Conversations panel:** Saved multi-turn chat sessions. + for new, trash to delete.
- **Chat interface:** User bubbles (right/dark), AI bubbles (left, streaming token by token). Full history sent with each message for context.
- **Streaming:** `/api/chat/conversations/{id}/ask/stream` — `StreamingResponse` with `text/event-stream`. Typing dots shown until first token; replaced by live-filling bubble. User message saved to DB before streaming starts; full AI message saved after stream completes.
- **Intent parsing:** GPT-4o extracts `date_from`, `date_to`, `source`, `event_type` from the question. Understands "last Thursday", "this week", "my Jira tickets".
- **Data source:** Activity events in MongoDB only — cannot fetch live data from GitHub/Jira/Teams.
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
│   └── rate_limit.py          — slowapi Limiter (200 req/min per IP, shared across all webhook routes)
├── routes/
│   ├── pages.py               — HTML page routes + WebSocket (/ws) — 10 routes, no DB dependency
│   ├── profile.py             — /api/me, /api/profile/timezone
│   ├── activity.py            — /api/events/recent, /api/stats, /api/week-stats, /api/day-data, /api/week-breakdown, /api/analytics/trend
│   ├── stats.py               — /api/github/stats, /api/jira/stats, /api/teams/stats, /api/gitlab/stats
│   ├── summaries.py           — GET/DELETE /api/summaries, POST /api/summaries/generate
│   └── exports.py             — /api/export/daily-pdf, /api/export/weekly-pdf
├── services/
│   └── activity_query.py      — Shared async query helpers used across route modules:
│                                 get_profile_tz(), week_bounds(), pct(), count(),
│                                 daily_counts(), top_items(), workspace_breakdown(), get_integrations()
├── ai/
│   ├── summarizer.py          — _summarise_profile(), daily/weekly logic, run_startup_catchup()
│   └── query.py               — Ask AI endpoints, _gpt_parse_intent(), chat conversations, /ask/stream SSE
├── auth/
│   ├── sso.py                 — Microsoft SSO, get_profile_from_session()
│   ├── oauth.py               — OAuth callbacks; _INTEGRATION_CLASSES factory for typed instantiation
│   └── github_app.py          — GitHub App setup
├── storage/
│   ├── mongodb.py             — activity_events() collection accessor
│   ├── postgres.py            — engine, AsyncSessionLocal, get_db() FastAPI dependency (one session/request)
│   └── models.py              — SQLAlchemy models: Profile, Integration (STI base) + TeamsIntegration /
│                                 GitHubIntegration / GitLabIntegration / JiraIntegration subclasses,
│                                 LinkedIdentity, Summary, QueryLog, ChatConversation, ChatMessage
├── webhooks/
│   ├── normalizer.py          — Strips PII, maps raw webhook payloads to standard shape
│   ├── registration.py        — auto_register_webhook(); sets sync_status=error on failure
│   ├── renewal.py             — APScheduler background jobs: renew Teams subscriptions, Jira webhooks, GitHub health
│   └── receivers/             — Per-connector webhook receivers (all rate-limited)
├── ws_manager.py              — WebSocket ConnectionManager, broadcast()
└── templates/
    ├── base.html              — Sidebar, theme toggle, Live dot, Help link
    ├── dashboard.html         — Overview page (stacked bar chart, week picker JS)
    ├── my_day.html            — My Day page (date nav, generate for any date)
    ├── digest.html            — Digest page
    ├── ai.html                — Ask AI page (conversations, streaming chat)
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
| **Event backfill** | Events from when the app was offline are never captured. No backfill mechanism exists. | User can manually Generate AI summary for days with events, but zero-event days will always show as empty. |
| **Scheduling dependency** | APScheduler only fires when the app is running. Startup catch-up regenerates yesterday + last week's summaries, but gaps older than 1 day require manual Generate. | Generate button on My Day / Digest lets user trigger for any date. |
| **Teams capture scope** | Only messages *you* send are captured. Other participants in a chat are not recorded. | By design (privacy). |
| **GitHub offline gaps** | If app is down when a push fires, the webhook is not replayed. | GitHub retries for a short window; no guaranteed delivery. |
| **No mobile layout** | UI is desktop-only. No responsive breakpoints. | Personal tool — desktop only. |
| **No multi-user isolation testing** | Only ever tested with a single user. Multi-user scenarios (shared repos) are untested. | N/A for now. |
| **Chart Y-axis normalisation** | Connector page charts normalise 0–1 relative to busiest day. 1 event all week shows as 1.0. | Tooltips show raw count. |
| **Redis dependency** | App requires Redis for sessions. If Redis is down, all sessions fail. No fallback. | Run Redis as a service alongside the app. |
| **Test suite** ✅ Done | 113 tests across normalizer, routes, summarizer, webhook processors, Teams backfill, and stats routes. Run with `pytest tests/ -q`. | — |
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
2. **GitLab — duplicate webhook prevention** — `GET /projects/{id}/hooks` called before `POST`; skips registration if URL already present. Safe to call Sync Webhooks repeatedly.
3. **Startup catch-up for missed summaries** — `run_startup_catchup()` fires on every boot via `asyncio.create_task`. Checks PostgreSQL for yesterday's daily and last week's weekly summary per profile; generates any that are missing.
4. **Ask AI streaming** — New `POST /api/chat/conversations/{id}/ask/stream` endpoint returns `text/event-stream`. Azure OpenAI called with `stream=True`. User message saved before streaming; AI message saved after. Frontend uses `ReadableStream` to render tokens as they arrive; typing dots shown until first token.
5. **Webhook rate limiting** — `slowapi` added (`200/minute` per IP). Applied to all 5 webhook endpoints. `RateLimitExceeded` handler returns HTTP 429. Shared `Limiter` in `app/middleware/rate_limit.py`.
6. **Connector error surfacing** — OAuth callback now detects empty/error token and redirects to `/{app}?error=<code>` instead of silently saving a broken token. `_register_jira` and `_register_gitlab` set `sync_status=error` on failure. `/api/me` now returns `integration_errors: {source: bool}` alongside `integrations`. GitLab, Jira, and Teams pages show orange warning banners for auth failures and webhook registration failures.
7. **Dashboard week picker** — Replaced 7D/1M/3M buttons with a calendar week picker. `setRangeDefault()`, `selectWeek()`, `togglePicker()`, `renderPicker()`.
8. **File names in activity timeline** — SHA chip + file name tags added to Dashboard and all connector page timelines.
9. **Generate for past days** — My Day now shows Generate for any day with events. Backend uses exact midnight-to-midnight window for `specific_date`.
10. **Token / cost logging** — All AI calls log `prompt_tokens`, `completion_tokens`, `total_tokens`, and `$cost`.
11. **Ask AI GPT intent parsing** — Replaced keyword-based parser with `_gpt_parse_intent()`. Understands natural language time references.
12. **Help page** — Built from scratch: master-detail layout, FAQ accordion, all four connector setup guides, page explanations with SVG mockups.

---

## 10. Next Possible Steps

> Not committed — just a parking lot for ideas.

- [ ] Add event backfill endpoint (replay missed webhooks from connector APIs)
- [ ] Add notification system (email/push when weekly summary is ready)
- [ ] Screenshot-based help page illustrations (replace SVG mockups with real screenshots)
- [ ] Mobile-responsive layout
- [ ] Redis session fallback (cookie-based fallback if Redis is unavailable)
- [ ] Integration tests for the connector pipelines (especially normalizer + period bounds)
- [ ] Multi-user isolation smoke test before onboarding second user
- [x] Export activity to CSV / PDF for performance review use — PDF export done (`/api/export/daily-pdf`, `/api/export/weekly-pdf`)
- [ ] Export to CSV
