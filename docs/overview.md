# Developer Activity Tracker — Project Overview

**Last updated:** June 2026  
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
| `connectors` | Per-user connector status (token, active, expires_at) |
| `summaries` | AI-generated daily / weekly summary text |
| `query_logs` | Every Ask AI question + filters used + answer |
| `chat_conversations` | Multi-turn conversation sessions |
| `chat_messages` | Individual messages within a conversation |

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
- **Implication:** if the app was offline at 11 PM, that day's summary is not auto-generated. User must click Generate manually on My Day.

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
- Connection: GitLab OAuth → `read_api`, `read_user`
- Webhook: `push`, `merge_request` events
- Captures: commit title, SHA, changed file names, MR title and state
- Does NOT capture: code diffs, full file contents

### Jira ✅ Active
- Connection: Atlassian OAuth → `read:jira-work`, `read:jira-user`, `manage:jira-webhook`
- Webhook: `issue:updated`, `comment_created`, `issue:created` events
- Captures: ticket key, title, event type, field changes, timestamp
- Does NOT capture: ticket description body, attachments

### Microsoft Teams ✅ Active (auto-connect)
- Connection: automatic via Microsoft SSO login — no separate Connect button
- Uses Microsoft Graph subscription (auto-renews)
- Captures: messages **you send** in chats and channels, meetings you attend
- Does NOT capture: messages from other participants, full message body
- If disconnected: sign out and back in to refresh the Microsoft token

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
- **Chat interface:** User bubbles (right/dark), AI bubbles (left). Full history sent with each message for context.
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

Same layout as GitHub page. Tabs: Commits / MR Opened / MR Merged. Green colour throughout.

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
app/
├── main.py                    — FastAPI app, middleware, router includes
├── config.py                  — Settings (env vars, Azure endpoints)
├── routes/
│   ├── dashboard.py           — All page routes + /api/summaries/generate + /help
│   └── ...
├── ai/
│   ├── summarizer.py          — _summarise_profile(), daily/weekly logic, token logging
│   └── query.py               — Ask AI endpoints, _gpt_parse_intent(), chat conversations
├── auth/
│   └── sso.py                 — Microsoft SSO, get_profile_from_session()
├── storage/
│   ├── mongodb.py             — activity_events() collection accessor
│   ├── postgres.py            — AsyncSessionLocal, engine
│   └── models.py              — SQLAlchemy models (Profile, Connector, Summary, QueryLog, Chat*)
├── webhooks/
│   ├── normalizer.py          — Strips PII, maps raw webhook payloads to standard shape
│   └── ...                    — Per-connector webhook receivers
├── scheduler.py               — APScheduler setup, nightly/weekly summary jobs
├── ws_manager.py              — WebSocket ConnectionManager, broadcast()
└── templates/
    ├── base.html              — Sidebar, theme toggle, Live dot, Help link
    ├── dashboard.html         — Overview page (stacked bar chart, week picker JS)
    ├── my_day.html            — My Day page (date nav, generate for any date)
    ├── digest.html            — Digest page
    ├── ai.html                — Ask AI page (conversations, chat)
    ├── github.html            — GitHub connector page
    ├── gitlab.html            — GitLab connector page
    ├── jira.html              — Jira connector page
    ├── teams.html             — Teams connector page
    └── help.html              — Help page (master-detail, FAQ accordion, SVG charts)
```

---

## 8. Known Gaps / Incomplete / Rough Edges

| Area | Issue | Workaround / Note |
|---|---|---|
| **Event backfill** | Events from when the app was offline are never captured. No backfill mechanism exists. | User can manually Generate AI summary for days with events, but zero-event days will always show as empty. |
| **Scheduling dependency** | APScheduler only fires when the app is running. If app is down at 11 PM, that day's auto-summary is skipped. | Generate button on My Day / Digest lets user manually trigger for any date. |
| **Teams capture scope** | Only messages *you* send are captured. Other participants in a chat are not recorded. | By design (privacy) — document this clearly, which the Help page does. |
| **GitHub offline gaps** | If app is down when a push fires, the webhook is not replayed. Event is permanently missed. | GitHub does retry webhooks for a short window, but no guaranteed delivery. |
| **No mobile layout** | UI is desktop-only. No responsive breakpoints. | N/A — personal tool, used on desktop. |
| **No multi-user isolation testing** | Only ever tested with a single user (profile). Multi-user scenarios (same org, shared repos) are untested. | N/A for now. |
| **GitLab / Jira / Teams "Connect" UI** | Connection flow pages exist but were not tested in this session. Status banners on those pages may not yet handle all error states gracefully. | Test the connect flows end-to-end per connector. |
| **Chart Y-axis normalisation** | Connector page charts normalise 0–1 relative to busiest day. If only 1 event exists all week, that day shows 1.0 — can look misleading. | Label tooltips show the raw count, which helps. |
| **Redis dependency** | App requires Redis for sessions. If Redis is down, all sessions fail. | Run Redis as a service. No fallback implemented. |
| **No test suite** | No unit or integration tests exist. | Manual testing only. |
| **Ask AI — no streaming** | AI responses appear all at once after the full completion. No token streaming. | Works fine for current response lengths (~2–4 sentences). |
| **Help page screenshots** | Help page uses HTML/SVG mockups instead of actual screenshots. Mockups may drift from reality as UI evolves. | Update SVG mockups when major UI changes are made. |

---

## 9. Recent Changes (This Session)

1. **Dashboard week picker** — Replaced 7D/1M/3M buttons with a calendar week picker (same structure as connector pages). `setRangeDefault()`, `selectWeek()`, `togglePicker()`, `renderPicker()`.
2. **File names in activity timeline** — Added SHA chip + file name tags to Dashboard and all connector page timelines (`_tlCommitExtra()` helper).
3. **Generate for past days** — My Day now shows Generate for any day with events, not just today. Sends `specific_date` to `/api/summaries/generate`. Backend uses exact midnight-to-midnight window for that date.
4. **Token / cost logging** — All AI calls (summaries, queries, chat) log `prompt_tokens`, `completion_tokens`, `total_tokens`, and `$cost` to the app logger.
5. **Ask AI GPT intent parsing** — Replaced keyword-based `intent_parser()` with `_gpt_parse_intent()`. Now understands natural language time references. Removed scope picker UI (Option 4) after implementing; backend `scope` kept as silent fallback.
6. **Help page** — Built from scratch: master-detail layout, FAQ accordion, all four connector setup guides (New User section), full page explanations with SVG line chart mockups, Recent Activity mockups, annotated callout lists.

---

## 10. Next Possible Steps

> Not committed — just a parking lot for ideas.

- [ ] Add event backfill endpoint (replay missed webhooks from connector APIs)
- [ ] Stream AI responses token-by-token (OpenAI streaming API)
- [ ] Add notification system (email/push when weekly summary is ready)
- [ ] Screenshot-based help page illustrations (replace SVG mockups with real screenshots)
- [ ] Mobile-responsive layout
- [ ] Rate limiting on webhook endpoints
- [ ] Integration tests for the connector pipelines
- [ ] GitLab / Jira connection flow end-to-end testing
- [ ] Export activity to CSV / PDF for performance review use
