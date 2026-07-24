"""
Shared AI context helpers: intent parsing, MongoDB time-filter building, and the
data-fetch/formatting used to assemble the model's prompt. Imported by chat.py,
tools.py, and insights.py — this module owns no routes.
"""
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.ai import llm
from app.routes.agent.analytics import (
    _tool_active_minutes, _tool_active_periods, _merge_hourly,
    _period_ranges, _token_total,
)
from app.services.activity_query import compute_focus_blocks
from app.services.timezone import day_bounds, local_date, now_local, resolve
from app.storage.mongodb import (
    device_heartbeats, claude_usage, local_commits, ai_tool_events, standups,
)
from app.webhooks.normalizer import _INJECTION_PATTERNS, sanitize

logger = logging.getLogger(__name__)


def _load_instructions() -> str:
    return llm.load_prompt(
        "instructions.txt",
        "You are a personal work assistant. Answer only from the data provided.",
    )


# ponytail: keyword gate (same spirit as the standup gate below) — the live
# Jira fetch is three Atlassian round-trips, too heavy for every chat message.
_JIRA_STATE_WORDS = ("jira", "issue", "ticket", "assigned", "sprint",
                     "overdue", "deadline", "backlog", "story point")


def _format_jira_live(assigned: dict) -> str:
    """Prompt block for the user's CURRENT Jira plate. Unlike ACTIVITY DATA this
    is a live snapshot, independent of the question's date filter."""
    lines = [f"CURRENTLY ASSIGNED JIRA ISSUES (live snapshot, current state — "
             f"independent of the date range above; {len(assigned['issues'])} open):"]
    if assigned.get("done_7d") is not None:
        lines.append(f"  Resolved by the user in the last 7 days: {assigned['done_7d']}")
    for it in assigned["issues"]:
        bits = [it.get("key", ""), it.get("status", ""), it.get("priority", "")]
        if it.get("due_date"):
            bits.append(f"due {it['due_date']}")
        if it.get("sprint"):
            bits.append(it["sprint"])
        if it.get("story_points") is not None:
            bits.append(f"{it['story_points']} pts")
        lines.append("  " + " | ".join(str(b) for b in bits if b)
                     + f" — {sanitize(it.get('summary', ''))}")
    return "\n".join(lines)


def _sanitize_question(text: str) -> str:
    cleaned = _INJECTION_PATTERNS.sub("", text or "")
    return cleaned.strip()[:1000]


# The AI answers in text; charts already exist as Chart.js dashboards. When a
# question is trend/visual-shaped, point the user at the matching page instead
# of rendering pixels in chat. ponytail: keyword heuristic — refine if it misfires.
_CHART_INTENT = ("trend", "chart", "graph", "over time", "breakdown", "visuali",
                 "distribution", "by hour", "by day", "compare", "history", "most")
_CHART_PAGES = (  # (topic keywords, label, href) — first match wins; order matters
    (("jira", "sprint", "issue", "ticket", "priority", "overdue", "backlog"),
     "Jira charts", "/jira"),
    (("claude", "token", "copilot", "cursor", "ai tool", "ai-tool"),
     "AI tools breakdown", "/my-activity/ai-tools"),
    (("focus", "coding time", "heartbeat"),
     "My Activity", "/my-activity"),
)


def _chart_link(question: str) -> dict | None:
    """{"label","href"} of a dashboard chart to offer, or None. Only fires when the
    question reads as trend/visual; picks the page by topic, defaults to Analytics."""
    q = question.lower()
    if not any(w in q for w in _CHART_INTENT):
        return None
    for words, label, href in _CHART_PAGES:
        if any(w in q for w in words):
            return {"label": label, "href": href}
    return {"label": "Analytics", "href": "/analytics"}


# The single-window activity pipeline can't compare two periods, so a token
# comparison ("this week vs last week") gets its own fetch of both periods.
_COMPARE_WORDS = ("last week", "last month", "previous week", "previous month",
                  "compare", "comparison", " vs ", "versus",
                  "week over week", "month over month")


async def _token_comparison_block(profile_id: str, tz_name: str, question: str) -> str:
    ql = question.lower()
    if not any(w in ql for w in _COMPARE_WORDS):
        return ""
    gran = "month" if "month" in ql else "week"
    rng = _period_ranges(gran, now_local(resolve(tz_name)).date())
    (tf, tt), (lf, lt) = rng["this"], rng["last"]
    this_total = await _token_total(profile_id, tf, tt)
    last_total = await _token_total(profile_id, lf, lt)
    if not (this_total or last_total):
        return ""
    delta = this_total - last_total
    pct = f"{delta / last_total * 100:+.0f}%" if last_total else "n/a (no prior data)"
    return (f"CLAUDE TOKEN USAGE COMPARISON (input+output tokens):\n"
            f"  This {gran} ({tf} to {tt}): {this_total:,} tokens\n"
            f"  Last {gran} ({lf} to {lt}): {last_total:,} tokens\n"
            f"  Change: {delta:+,} ({pct})")


def _scope_to_range(scope: str, tz_name: str = "UTC") -> dict:
    tz  = resolve(tz_name)
    now = now_local(tz)
    if scope == "week":
        monday = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
        return {"$gte": day_bounds(monday, tz)[0]}
    # ponytail: month is a rolling window (now - 30d), not day-aligned, so
    # day_bounds doesn't apply. Vestigial — scope is ~always "today".
    if scope == "month":
        return {"$gte": (now - timedelta(days=30)).astimezone(timezone.utc)}
    # today: local midnight → UTC
    return {"$gte": day_bounds(now.strftime("%Y-%m-%d"), tz)[0]}


# Strict schema for structured outputs — nullable enums via ["string","null"] + null
# in the enum; all keys required + additionalProperties:false, as strict mode demands.
_INTENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "date_from":  {"type": ["string", "null"]},
        "date_to":    {"type": ["string", "null"]},
        "source":     {"type": ["string", "null"], "enum": ["github", "gitlab", "jira", "teams", None]},
        "event_type": {"type": ["string", "null"], "enum": ["commit", "pr", "issue", "meeting", "comment", None]},
    },
    "required": ["date_from", "date_to", "source", "event_type"],
}


async def _gpt_parse_intent(question: str, today: str, tz_name: str = "UTC") -> dict:
    """Ask GPT to extract date_from, date_to, source, event_type from the question."""
    local_now    = datetime.now(ZoneInfo(tz_name or "UTC"))
    weekday_name = local_now.strftime("%A")                      # "Tuesday"
    yesterday    = (local_now - timedelta(days=1)).strftime("%Y-%m-%d (%A)")
    system_prompt = (
        f"Today is {weekday_name}, {today} (user's local time). Yesterday was {yesterday}.\n"
        "When the user refers to a day name (e.g. 'Monday', 'last Friday'), resolve it to the "
        "most recent past occurrence of that weekday as a YYYY-MM-DD date.\n"
        "The user will provide a question. Extract the following fields and return ONLY valid JSON — no explanation:\n"
        '  "date_from": YYYY-MM-DD or null\n'
        '  "date_to":   YYYY-MM-DD or null  (null = up to now)\n'
        '  "source":    one of github, gitlab, jira, teams or null\n'
        '  "event_type": one of commit, pr, issue, meeting, comment or null'
    )
    try:
        return await llm.extract_schema(system_prompt, question, _INTENT_SCHEMA, name="intent")
    except Exception as exc:
        logger.warning("GPT intent parse failed: %s", exc)
        return {}


def _intent_to_filter(parsed: dict, scope: str, tz_name: str = "UTC") -> dict:
    """Convert GPT-parsed intent + UI scope into a MongoDB time range filter."""
    date_from = parsed.get("date_from")
    date_to   = parsed.get("date_to")

    if date_from:
        try:
            tz = resolve(tz_name)
            # local-midnight bounds so "yesterday" means the user's actual day, not UTC day
            start, _ = day_bounds(date_from, tz)
            _, end   = day_bounds(date_to or date_from, tz)
            return {"$gte": start, "$lte": end}
        except ValueError:
            pass

    return _scope_to_range(scope, tz_name)


def _period_label(parsed: dict, scope: str) -> str:
    """Human label for the date range the fetched data is already filtered to,
    so the model doesn't mistake filtered data for an unfiltered/cumulative dump."""
    date_from = parsed.get("date_from")
    date_to   = parsed.get("date_to")
    if date_from:
        weekday = datetime.strptime(date_from, "%Y-%m-%d").strftime("%A")
        single  = f"{weekday}, {date_from}"
        return single if (not date_to or date_to == date_from) else f"{single} to {date_to}"
    return {"today": "today", "week": "this week"}.get(scope, scope or "today")


def _map_event_type(raw: str | None) -> str | None:
    if not raw:
        return None
    r = raw.lower()
    if r in ("pr", "pull_request", "pull request"):
        return "pr_"
    if r == "issue":
        return "issue"
    if r == "comment":
        return "comment"
    return r


def _claude_date_range(time_filter: dict, tzinfo) -> tuple[str, str] | None:
    """Local (date_from, date_to) strings for the claude_usage lookup, which is
    keyed by local date. The filter's upper bound is an EXCLUSIVE next-midnight, so
    step back a second to land on the last real local day — not the day after
    (otherwise a single-day question pulls in the following day's usage). Returns
    None when the filter has no lower bound."""
    start_dt = time_filter.get("$gte")
    if not start_dt:
        return None
    end_dt = time_filter.get("$lte") or time_filter.get("$lt")
    end_dt = (end_dt - timedelta(seconds=1)) if end_dt else datetime.now(timezone.utc)
    return local_date(start_dt, tzinfo), local_date(end_dt, tzinfo)


def _fmt_local(iso_z: str, tz) -> str:
    """'YYYY-MM-DDTHH:MM:SSZ' (naive-UTC) → local clock like '9:00 AM'."""
    dt = datetime.strptime(iso_z, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(tz)
    return dt.strftime("%I:%M %p").lstrip("0")


async def _fetch_my_activity_context(profile_id: str, time_filter: dict,
                                     tz_name: str = "UTC", question: str = "") -> str:
    lines: list[str] = []

    # Focus time — gap-based blocks, the SAME calc the My Activity page uses
    # (compute_focus_blocks) so the AI answer and the page never disagree.
    hbs = await device_heartbeats().find(
        {"profile_id": profile_id, "timestamp": time_filter, "idle": False},
        projection={"timestamp": 1, "_id": 0},
    ).sort("timestamp", 1).to_list(35_000)
    focus_blocks = compute_focus_blocks(hbs)
    focus_min = sum(b["duration_min"] for b in focus_blocks)
    logger.info(
        "AI context | profile=%s tz=%s filter=%s→%s heartbeats=%d focus_min=%d",
        profile_id[:8], tz_name,
        time_filter.get("$gte"), time_filter.get("$lte") or time_filter.get("$lt"),
        len(hbs), focus_min,
    )
    if focus_min:
        h, m = divmod(focus_min, 60)
        lines.append(f"Focus/coding time: {h}h {m}m (approx)")

    # Claude token usage — keyed by LOCAL date string, derived through the same
    # IANA tz as everything else (docs/adr-0001-timezone.md).
    rng = _claude_date_range(time_filter, resolve(tz_name))
    if rng:
        date_from, date_to = rng
        claude_docs = await claude_usage().find(
            {"profile_id": profile_id, "date": {"$gte": date_from, "$lte": date_to}}
        ).to_list(200)
        logger.info(
            "AI context | claude_usage date=%s→%s found=%d",
            date_from, date_to, len(claude_docs),
        )
        if claude_docs:
            total_in  = sum(d.get("input_tokens",  0) for d in claude_docs)
            total_out = sum(d.get("output_tokens", 0) for d in claude_docs)
            lines.append(f"\nClaude Code usage: {total_in+total_out:,} tokens "
                         f"(input {total_in:,} / output {total_out:,})")
            repos: dict[str, int] = {}
            for d in claude_docs:
                repo = d.get("repo") or "unknown"
                repos[repo] = repos.get(repo, 0) + d.get("input_tokens", 0) + d.get("output_tokens", 0)
            for repo, toks in sorted(repos.items(), key=lambda x: -x[1]):
                lines.append(f"  {repo}: {toks:,} tokens")
            hourly = _merge_hourly(claude_docs)                  # when tokens were spent
            if hourly:
                lines.append("  tokens by hour of day (local):")
                for hb in hourly:
                    h = hb["hour"]; hr = h % 12 or 12; ampm = "am" if h < 12 else "pm"
                    tot = hb["input_tokens"] + hb["output_tokens"]
                    lines.append(f"    {hr}{ampm}: {tot:,} ({hb['input_tokens']:,} in / {hb['output_tokens']:,} out)")

    # Local commits
    commits = await local_commits().find(
        {"profile_id": profile_id, "timestamp": time_filter},
        projection={"repo": 1, "branch": 1, "message": 1, "timestamp": 1, "_id": 0},
    ).sort("timestamp", -1).to_list(50)
    if commits:
        lines.append(f"\nLocal commits: {len(commits)}")
        for c in commits:
            ts  = c.get("timestamp")
            tss = ts.strftime("%Y-%m-%d %H:%M") if ts else ""
            lines.append(f"  [{tss}] {c.get('repo','?')}/{c.get('branch','?')}: "
                         f"{c.get('message','')[:80]}")

    # AI tools — with real active time per tool (running while not idle), the same
    # overlap-with-focus-blocks number the My Activity dropdown shows.
    ai_docs = await ai_tool_events().find(
        {"profile_id": profile_id, "timestamp": time_filter},
        projection={"tools": 1, "timestamp": 1, "_id": 0},
    ).to_list(2000)
    if ai_docs:
        active_min = _tool_active_minutes(ai_docs, focus_blocks)
        periods    = _tool_active_periods(ai_docs, focus_blocks)
        tz = resolve(tz_name)
        all_tools: set[str] = set()
        for doc in ai_docs:
            all_tools.update(doc.get("tools", []))
        if all_tools:
            lines.append("\nAI tools used (active = running while not idle):")
            for tool in sorted(all_tools):
                mins = active_min.get(tool, 0)
                if mins:
                    h, m = divmod(mins, 60)
                    lines.append(f"  {tool}: {f'{h}h {m}m' if h else f'{m}m'} active")
                else:
                    lines.append(f"  {tool}: detected")
                tool_periods = periods.get(tool, [])
                for p in tool_periods[:8]:                       # sessions = when it was active
                    lines.append(f"    {_fmt_local(p['start'], tz)}–{_fmt_local(p['end'], tz)}")
                if len(tool_periods) > 8:
                    lines.append(f"    (+{len(tool_periods) - 8} more sessions)")

    # Standup history — ONLY when the question is about standups. Dumping 30 days of
    # standup text into every request drowns out sparse activity data and skews every
    # answer toward reciting a standup. ponytail: keyword gate; make it intent-driven
    # if "standup" ever needs synonyms.
    if "standup" in question.lower():
        standup_docs = await standups().find(
            {"profile_id": profile_id},
            projection={"date": 1, "text": 1, "_id": 0},
        ).sort("date", -1).to_list(10)
        if standup_docs:
            lines.append("\nPAST STANDUPS (most recent first):")
            for s in standup_docs:
                lines.append(f"  [{s['date']}] {s['text']}")

    return "\n".join(lines)
