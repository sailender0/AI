"""My Activity analytics — focus blocks, AI usage, local commits."""
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request

from app.auth.sso import get_profile_from_session
from app.storage.mongodb import ai_tool_events, claude_usage, device_heartbeats, local_commits

router = APIRouter()

_HEARTBEAT_INTERVAL = 30   # seconds
_FOCUS_GAP_SECONDS  = 300  # gap > 5 min = new block


def _day_bounds(date_str: str | None, tz_offset: int) -> tuple[datetime, datetime]:
    tz_offset = max(-840, min(840, tz_offset))
    if date_str and re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        local_midnight = datetime.strptime(date_str, "%Y-%m-%d")
        start = local_midnight.replace(tzinfo=timezone.utc) - timedelta(minutes=tz_offset)
    else:
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def _compute_focus_blocks(heartbeats: list[dict]) -> list[dict]:
    """Convert sorted non-idle heartbeats into contiguous focus blocks."""
    if not heartbeats:
        return []
    blocks, block_start, block_end = [], None, None
    for hb in heartbeats:
        ts = hb["timestamp"]
        if block_start is None:
            block_start = block_end = ts
        elif (ts - block_end).total_seconds() <= _FOCUS_GAP_SECONDS + _HEARTBEAT_INTERVAL:
            block_end = ts
        else:
            blocks.append({"start": block_start, "end": block_end,
                           "duration_min": int((block_end - block_start).total_seconds() / 60)})
            block_start = block_end = ts
    if block_start:
        blocks.append({"start": block_start, "end": block_end,
                       "duration_min": int((block_end - block_start).total_seconds() / 60)})
    return blocks


@router.get("/today")
async def activity_today(
    request: Request,
    date: str = "",
    tz_offset: int = 0,
    device_id: str = "",
):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        raise HTTPException(401, "Sign in required")

    day_start, day_end = _day_bounds(date, tz_offset)
    ts_filter = {"$gte": day_start, "$lt": day_end}

    hb_filter: dict = {"profile_id": profile_id, "timestamp": ts_filter, "idle": False}
    if device_id:
        hb_filter["device_id"] = device_id

    hbs = await device_heartbeats().find(
        hb_filter,
        projection={"timestamp": 1, "git_repo": 1, "git_branch": 1, "_id": 0},
        sort=[("timestamp", 1)],
    ).limit(5_000).to_list(5_000)

    focus_blocks = _compute_focus_blocks(hbs)
    total_focus_min = sum(b["duration_min"] for b in focus_blocks)

    # AI tools — count snapshots per tool → active minutes (1 snapshot = 60s)
    ai_docs = await ai_tool_events().find(
        {"profile_id": profile_id, "timestamp": ts_filter},
        projection={"tools": 1, "_id": 0},
    ).to_list(1500)
    tool_counts: dict[str, int] = {}
    for doc in ai_docs:
        for tool in doc.get("tools", []):
            tool_counts[tool] = tool_counts.get(tool, 0) + 1
    tool_active_min = {tool: count for tool, count in tool_counts.items()}
    active_tools    = sorted(tool_active_min.keys())

    # Claude usage today
    today_str = day_start.strftime("%Y-%m-%d")
    claude_docs = await claude_usage().find(
        {"profile_id": profile_id, "date": today_str}
    ).to_list(20)
    claude_summary = [
        {
            "model":         d.get("model", ""),
            "input_tokens":  d.get("input_tokens", 0),
            "output_tokens": d.get("output_tokens", 0),
            "messages":      d.get("message_count", 0),
        }
        for d in claude_docs
    ]

    # Local commits today
    commits = await local_commits().find(
        {"profile_id": profile_id, "timestamp": ts_filter},
        projection={"sha": 1, "repo": 1, "branch": 1, "message": 1,
                    "files_changed": 1, "insertions": 1, "deletions": 1,
                    "timestamp": 1, "_id": 0},
        sort=[("timestamp", -1)],
    ).limit(50).to_list(50)

    # Active repo/branch — most recent heartbeat
    last_hb = await device_heartbeats().find_one(
        {"profile_id": profile_id},
        sort=[("timestamp", -1)],
        projection={"git_repo": 1, "git_branch": 1, "idle": 1, "timestamp": 1, "_id": 0},
    )

    return {
        "focus_blocks":    [
            {**b, "start": b["start"].isoformat(), "end": b["end"].isoformat()}
            for b in focus_blocks
        ],
        "total_focus_min": total_focus_min,
        "active_tools":    active_tools,
        "tool_active_min": tool_active_min,
        "claude_usage":    claude_summary,
        "commits":         [
            {**c, "timestamp": c["timestamp"].isoformat() if c.get("timestamp") else None}
            for c in commits
        ],
        "active_now": {
            "repo":    last_hb.get("git_repo") if last_hb else None,
            "branch":  last_hb.get("git_branch") if last_hb else None,
            "idle":    last_hb.get("idle", True) if last_hb else True,
            "last_seen": last_hb["timestamp"].isoformat() if last_hb else None,
        } if last_hb else None,
    }


@router.get("/week")
async def activity_week(request: Request, tz_offset: int = 0):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        raise HTTPException(401, "Sign in required")

    tz_offset = max(-840, min(840, tz_offset))
    now = datetime.now(timezone.utc)
    week_start = (now - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
    ts_filter = {"$gte": week_start, "$lt": now}

    hbs = await device_heartbeats().find(
        {"profile_id": profile_id, "timestamp": ts_filter, "idle": False},
        projection={"timestamp": 1, "_id": 0},
        sort=[("timestamp", 1)],
    ).limit(35_000).to_list(35_000)

    # Group by day
    days: dict[str, list] = {}
    for hb in hbs:
        day = hb["timestamp"].strftime("%Y-%m-%d")
        days.setdefault(day, []).append(hb)

    week_data = []
    for day, day_hbs in sorted(days.items()):
        blocks = _compute_focus_blocks(day_hbs)
        week_data.append({
            "date":            day,
            "focus_min":       sum(b["duration_min"] for b in blocks),
            "focus_blocks":    len(blocks),
        })

    claude_docs = await claude_usage().find(
        {"profile_id": profile_id, "date": {"$gte": week_start.strftime("%Y-%m-%d")}}
    ).to_list(70)
    total_tokens = sum(d.get("input_tokens", 0) + d.get("output_tokens", 0) for d in claude_docs)

    commit_count = await local_commits().count_documents(
        {"profile_id": profile_id, "timestamp": ts_filter}
    )

    return {
        "days":         week_data,
        "total_tokens": total_tokens,
        "commit_count": commit_count,
    }
