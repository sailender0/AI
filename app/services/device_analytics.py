"""Device-activity analytics — focus blocks, AI-tool usage, Claude tokens.

Pure computation + Mongo reads for the My Activity feature. Extracted from
app/routes/agent/analytics.py so the AI layer (app/ai/*) and the email reports
can build the same payloads without importing an HTTP route module. Nothing
here knows about requests, responses, or auth.
"""
import logging
from datetime import date, datetime, timedelta, timezone

from app.services.activity_query import compute_focus_blocks
from app.services.timezone import day_bounds, local_date
from app.storage.mongodb import (
    ai_tool_events, claude_usage, device_heartbeats, local_commits, week_summaries,
)

log = logging.getLogger(__name__)


def aggregate_claude(claude_docs: list[dict]) -> tuple[list[dict], int]:
    """Group claude_usage docs by repo, merging same-model rows. Returns (summary, total_tokens)."""
    repos: dict[str, dict] = {}
    for d in claude_docs:
        repo = d.get("repo") or "unknown"
        if repo not in repos:
            repos[repo] = {"repo": repo, "models": [], "files": set(),
                           "input_tokens": 0, "output_tokens": 0}
        existing = next((m for m in repos[repo]["models"] if m["model"] == d.get("model", "")), None)
        if existing:
            existing["input_tokens"]  += d.get("input_tokens", 0)
            existing["output_tokens"] += d.get("output_tokens", 0)
            existing["messages"]      += d.get("message_count", 0)
        else:
            repos[repo]["models"].append({
                "model":         d.get("model", ""),
                "input_tokens":  d.get("input_tokens", 0),
                "output_tokens": d.get("output_tokens", 0),
                "messages":      d.get("message_count", 0),
            })
        repos[repo]["input_tokens"]  += d.get("input_tokens", 0)
        repos[repo]["output_tokens"] += d.get("output_tokens", 0)
        repos[repo]["files"].update(d.get("files", []))

    summary = [{**r, "files": sorted(r["files"])} for r in repos.values()]
    total   = sum(d.get("input_tokens", 0) + d.get("output_tokens", 0) for d in claude_docs)
    return summary, total


def tool_active_minutes(ai_docs: list[dict], focus_blocks: list[dict]) -> dict[str, int]:
    """Real active minutes per tool = overlap between each tool's detected-presence
    intervals and the focus blocks (true non-idle device time).

    A tool is "present" from when an ai_tool_events doc first lists it until the next
    doc that omits it — the agent emits on change and re-emits every 30 min while
    unchanged (agent AI_RESEND_INTERVAL). Still-open intervals extend to the last
    focus-block end, so a tool running right now counts up to the latest heartbeat.
    Intersecting with focus blocks means idle/offline time is never counted, and the
    per-tool total can never exceed total_focus_min shown on the page.
    ponytail: O(intervals × blocks) nested scan — both are tiny per day; upgrade to a
    sweep line only if a day ever has thousands of either.
    """
    if not focus_blocks:
        return {}
    end_cap   = max(b["end"] for b in focus_blocks)
    intervals = _tool_presence_intervals(ai_docs, end_cap)

    out: dict[str, int] = {}
    for tool, ivs in intervals.items():
        secs = 0.0
        for a_start, a_end in ivs:
            for b in focus_blocks:
                lo = max(a_start, b["start"])
                hi = min(a_end, b["end"])
                if hi > lo:
                    secs += (hi - lo).total_seconds()
        out[tool] = round(secs / 60)
    return out


def _tool_presence_intervals(ai_docs: list[dict], end_cap: datetime) -> dict[str, list[tuple]]:
    """Per tool, the [start, end) intervals during which it was detected as present.
    Present from the first event that lists it until the next event that omits it; a
    still-open interval is capped at end_cap (the last focus-block end)."""
    docs = sorted((d for d in ai_docs if d.get("timestamp")), key=lambda d: d["timestamp"])
    intervals: dict[str, list[tuple]] = {}
    open_since: dict[str, datetime] = {}
    for d in docs:
        t = d["timestamp"]
        tools = set(d.get("tools", []))
        for tool in tools - open_since.keys():
            open_since[tool] = t
        for tool in list(open_since.keys() - tools):   # tool disappeared → close its interval
            intervals.setdefault(tool, []).append((open_since.pop(tool), t))
    for tool, start in open_since.items():             # still running → cap at last activity
        intervals.setdefault(tool, []).append((start, end_cap))
    return intervals


def tool_active_periods(ai_docs: list[dict], focus_blocks: list[dict]) -> dict[str, list[dict]]:
    """Per tool, the actual active sessions = each presence interval clipped to the
    focus blocks, merged where segments touch. Same overlap logic as
    tool_active_minutes but keeps the [start, end] ranges so the UI can list real
    sessions. Sessions split around idle gaps, so they sum to the header's active total."""
    if not focus_blocks:
        return {}
    end_cap   = max(b["end"] for b in focus_blocks)
    intervals = _tool_presence_intervals(ai_docs, end_cap)
    blocks    = sorted(focus_blocks, key=lambda b: b["start"])

    out: dict[str, list[dict]] = {}
    for tool, ivs in intervals.items():
        segs: list[tuple] = []
        for a_start, a_end in ivs:
            for b in blocks:
                lo = max(a_start, b["start"])
                hi = min(a_end, b["end"])
                if hi > lo:
                    segs.append((lo, hi))
        segs.sort()
        merged: list[list] = []
        for s, e in segs:
            if merged and s <= merged[-1][1]:          # touching/contiguous → extend
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        out[tool] = [
            {"start": _isoZ(s), "end": _isoZ(e), "minutes": round((e - s).total_seconds() / 60)}
            for s, e in merged
        ]
    return out


def merge_hourly(claude_docs: list[dict]) -> list[dict]:
    """Sum the per-hour token buckets across a day's claude_usage docs into one
    [{hour, input_tokens, output_tokens}] series (hour = local 0–23)."""
    acc: dict[int, dict] = {}
    for d in claude_docs:
        for hb in d.get("hourly", []):
            a = acc.setdefault(hb.get("hour", 0), {"input_tokens": 0, "output_tokens": 0})
            a["input_tokens"]  += hb.get("input_tokens", 0)
            a["output_tokens"] += hb.get("output_tokens", 0)
    return [{"hour": h, **v} for h, v in sorted(acc.items())]


def session_token_totals(periods: list[dict], day_hourly: list[dict],
                         day_start: datetime) -> list[dict]:
    """Split each local hour's input/output tokens across the claude-code sessions it
    overlaps (proportional to overlap; an hour touching no session goes to the nearest
    one) so every session's totals reconcile to the day total.
    ponytail: hour buckets can misdate by ±1h on DST days — negligible for usage."""
    if not periods:
        return []
    start0 = day_start.astimezone(timezone.utc).replace(tzinfo=None)   # naive-UTC, matches _isoZ
    sess = [[datetime.strptime(p["start"], "%Y-%m-%dT%H:%M:%SZ"),
             datetime.strptime(p["end"],   "%Y-%m-%dT%H:%M:%SZ")] for p in periods]
    totals = [{"input": 0.0, "output": 0.0} for _ in sess]

    for hb in day_hourly:
        h_start = start0 + timedelta(hours=hb["hour"])
        h_end   = h_start + timedelta(hours=1)
        tin, tout = hb["input_tokens"], hb["output_tokens"]
        overlaps = [(i, ov) for i, (s, e) in enumerate(sess)
                    if (ov := (min(h_end, e) - max(h_start, s)).total_seconds()) > 0]
        if overlaps:
            tot = sum(ov for _, ov in overlaps)
            for i, ov in overlaps:
                totals[i]["input"]  += tin  * ov / tot
                totals[i]["output"] += tout * ov / tot
        else:                                              # no session this hour → nearest
            mid = h_start + timedelta(minutes=30)
            i = min(range(len(sess)),
                    key=lambda k: abs((sess[k][0] + (sess[k][1] - sess[k][0]) / 2 - mid).total_seconds()))
            totals[i]["input"]  += tin
            totals[i]["output"] += tout

    return [{"input_tokens": round(t["input"]), "output_tokens": round(t["output"])} for t in totals]


def _isoZ(dt: datetime) -> str:
    # Normalize to UTC naive before formatting — Motor may return tz-aware datetimes
    # which would produce "+00:00Z" (malformed) if we just append "Z".
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def period_ranges(granularity: str, today: date) -> dict:
    """Current- and previous-period (from, to) local date strings, inclusive.
    Compared against claude_usage.date, itself a local YYYY-MM-DD string. Pure
    (takes `today`) so the date math is testable — see tests/test_token_comparison.py.
    Week is Monday-anchored to match build_activity_week."""
    if granularity == "month":
        this_start = today.replace(day=1)
        last_end   = this_start - timedelta(days=1)
        last_start = last_end.replace(day=1)
    else:
        this_start = today - timedelta(days=today.weekday())
        last_start = this_start - timedelta(days=7)
        last_end   = this_start - timedelta(days=1)
    return {"this": (this_start.isoformat(), today.isoformat()),
            "last": (last_start.isoformat(), last_end.isoformat())}


async def token_total(profile_id: str, date_from: str, date_to: str) -> int:
    """Sum input+output Claude tokens over an inclusive local-date range."""
    docs = await claude_usage().find(
        {"profile_id": profile_id, "date": {"$gte": date_from, "$lte": date_to}},
        projection={"input_tokens": 1, "output_tokens": 1, "_id": 0},
    ).to_list(500)
    return sum(d.get("input_tokens", 0) + d.get("output_tokens", 0) for d in docs)


async def build_activity_today(profile_id: str, tzinfo, the_date: str, device_id: str = "") -> dict:
    """My Activity 'today' payload — served by GET /agent/activity/today and reused
    by the email report without an HTTP round-trip."""
    day_start, day_end = day_bounds(the_date, tzinfo)
    ts_filter = {"$gte": day_start, "$lt": day_end}

    hb_filter: dict = {"profile_id": profile_id, "timestamp": ts_filter, "idle": False}
    if device_id:
        hb_filter["device_id"] = device_id

    hbs = await device_heartbeats().find(
        hb_filter,
        projection={"timestamp": 1, "_id": 0},
        sort=[("timestamp", 1)],
    ).limit(5_000).to_list(5_000)

    focus_blocks    = compute_focus_blocks(hbs)
    total_focus_min = sum(b["duration_min"] for b in focus_blocks)

    ai_docs = await ai_tool_events().find(
        {"profile_id": profile_id, "timestamp": ts_filter},
        projection={"tools": 1, "timestamp": 1, "_id": 0},
    ).to_list(1500)
    tools_seen: set[str] = set()
    for doc in ai_docs:
        tools_seen.update(doc.get("tools", []))

    claude_docs = await claude_usage().find(
        {"profile_id": profile_id, "date": the_date}
    ).to_list(100)
    claude_summary, _ = aggregate_claude(claude_docs)

    # If claude usage exists for the day, ensure claude-code shows up in active_tools
    # regardless of whether ai_tool_events captured it (agent may not restart daily).
    if claude_docs:
        tools_seen.add("claude-code")

    # Real active minutes per tool — overlap of detection windows with focus blocks.
    tool_active_min = tool_active_minutes(ai_docs, focus_blocks)
    # Same overlap, kept as individual sessions so the tool dropdown can list ranges.
    active_periods = tool_active_periods(ai_docs, focus_blocks)
    # Option C — attribute per-hour Claude tokens to each claude-code session.
    # Skipped for days with no hourly data (pre-rebuild) so old rows don't show 0/0.
    cc_periods = active_periods.get("claude-code")
    day_hourly = merge_hourly(claude_docs)
    if cc_periods and day_hourly:
        for p, tk in zip(cc_periods, session_token_totals(cc_periods, day_hourly, day_start)):
            p["input_tokens"]  = tk["input_tokens"]
            p["output_tokens"] = tk["output_tokens"]

    log.info("TODAY debug: tz=%s day_start=%s date=%s ai_tool_events=%d claude_docs=%d tools=%s",
             tzinfo.key, day_start, the_date, len(ai_docs), len(claude_docs), sorted(tools_seen))

    commits = await local_commits().find(
        {"profile_id": profile_id, "timestamp": ts_filter},
        projection={"sha": 1, "repo": 1, "branch": 1, "message": 1,
                    "files_changed": 1, "insertions": 1, "deletions": 1,
                    "timestamp": 1, "_id": 0},
        sort=[("timestamp", -1)],
    ).limit(50).to_list(50)

    # Bug fix: restrict to last 6 hours so stale yesterday repo doesn't show
    now = datetime.now(timezone.utc)
    last_hb = await device_heartbeats().find_one(
        {"profile_id": profile_id, "timestamp": {"$gte": now - timedelta(hours=6)}},
        sort=[("timestamp", -1)],
        projection={"git_repo": 1, "git_branch": 1, "idle": 1, "timestamp": 1, "_id": 0},
    )

    return {
        "focus_blocks": [
            {"start": _isoZ(b["start"]), "end": _isoZ(b["end"]),
             "duration_min": b["duration_min"]}
            for b in focus_blocks
        ],
        "total_focus_min": total_focus_min,
        "active_tools":    sorted(tools_seen),
        "tool_active_min": tool_active_min,
        "tool_active_periods": active_periods,
        "claude_usage":    claude_summary,
        "commits": [
            {**c, "timestamp": _isoZ(c["timestamp"]) if c.get("timestamp") else None}
            for c in commits
        ],
        "active_now": {
            "repo":      last_hb.get("git_repo"),
            "branch":    last_hb.get("git_branch"),
            "idle":      last_hb.get("idle", True),
            "last_seen": _isoZ(last_hb["timestamp"]),
        } if last_hb else None,
    }


async def build_activity_week(profile_id: str, tzinfo, week_start_str: str) -> dict:
    """Weekly device-activity payload — per-day focus, tokens, and AI tools.
    Served by GET /agent/activity/week and reused by the email report."""
    now = datetime.now(timezone.utc)
    week_end_str = (datetime.strptime(week_start_str, "%Y-%m-%d") + timedelta(days=7)).strftime("%Y-%m-%d")
    w_start, _   = day_bounds(week_start_str, tzinfo)   # UTC start of the local Monday
    w_end,   _   = day_bounds(week_end_str, tzinfo)     # UTC start of the following Monday
    week_start   = week_start_str

    # Serve from cache for completed past weeks
    is_past = w_end < now - timedelta(hours=1)
    if is_past:
        cached = await week_summaries().find_one(
            {"profile_id": profile_id, "week_start": week_start}
        )
        if cached:
            cached.pop("_id", None)
            cached.pop("profile_id", None)
            return cached

    ts_filter = {"$gte": w_start, "$lt": min(w_end, now)}

    # Heartbeats — group by LOCAL date (bug fix: was grouping by UTC)
    hbs = await device_heartbeats().find(
        {"profile_id": profile_id, "timestamp": ts_filter, "idle": False},
        projection={"timestamp": 1, "_id": 0},
        sort=[("timestamp", 1)],
    ).limit(35_000).to_list(35_000)

    days: dict[str, list] = {}
    for hb in hbs:
        days.setdefault(local_date(hb["timestamp"], tzinfo), []).append(hb)

    week_data = []
    all_blocks: list[dict] = []
    blocks_by_day: dict[str, list] = {}
    for day, day_hbs in sorted(days.items()):
        blocks = compute_focus_blocks(day_hbs)
        all_blocks.extend(blocks)
        blocks_by_day[day] = blocks
        week_data.append({
            "date":         day,
            "focus_min":    sum(b["duration_min"] for b in blocks),
            "focus_blocks": len(blocks),
        })

    claude_docs = await claude_usage().find(
        {"profile_id": profile_id, "date": {"$gte": week_start, "$lt": week_end_str}}
    ).to_list(200)
    claude_summary, total_tokens = aggregate_claude(claude_docs)

    # Per-day breakdown — group docs by date, aggregate each day independently
    day_buckets: dict[str, list] = {}
    for doc in claude_docs:
        dk = doc.get("date", "")
        if dk:
            day_buckets.setdefault(dk, []).append(doc)

    claude_by_day: dict[str, list] = {}
    day_tokens: dict[str, int] = {}
    for dk, docs in day_buckets.items():
        summary, day_total = aggregate_claude(docs)
        claude_by_day[dk] = summary
        day_tokens[dk] = day_total

    # Patch ai_tokens into already-built week_data
    for d in week_data:
        d["ai_tokens"] = day_tokens.get(d["date"], 0)

    ai_docs = await ai_tool_events().find(
        {"profile_id": profile_id, "timestamp": ts_filter},
        projection={"tools": 1, "timestamp": 1, "_id": 0},
    ).to_list(10_000)
    tools_seen: set[str] = set()
    tools_by_day: dict[str, set] = {}
    ai_by_day: dict[str, list] = {}
    for doc in ai_docs:
        tools = doc.get("tools", [])
        tools_seen.update(tools)
        ts = doc.get("timestamp")
        if ts:
            dk = local_date(ts, tzinfo)
            tools_by_day.setdefault(dk, set()).update(tools)
            ai_by_day.setdefault(dk, []).append(doc)
    tools_by_day_out = {dk: sorted(s) for dk, s in tools_by_day.items()}

    if claude_docs:
        tools_seen.add("claude-code")

    # Real active minutes per tool across the week (overlap with all focus blocks).
    tool_active_min = tool_active_minutes(ai_docs, all_blocks)
    # Same, but per local day, so the day chips can show each tool's active time.
    tool_active_by_day = {
        dk: tool_active_minutes(docs, blocks_by_day.get(dk, []))
        for dk, docs in ai_by_day.items()
    }

    commit_count = await local_commits().count_documents(
        {"profile_id": profile_id, "timestamp": ts_filter}
    )

    result = {
        "week_start":      week_start,
        "days":            week_data,
        "total_tokens":    total_tokens,
        "commit_count":    commit_count,
        "claude_usage":    claude_summary,
        "claude_by_day":   claude_by_day,
        "tools_by_day":    tools_by_day_out,
        "tool_active_min": tool_active_min,
        "tool_active_by_day": tool_active_by_day,
        "active_tools":    sorted(tools_seen),
    }

    if is_past:
        await week_summaries().update_one(
            {"profile_id": profile_id, "week_start": week_start},
            {"$set": {**result, "profile_id": profile_id}},
            upsert=True,
        )

    return result
