from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.auth.sso import get_profile_from_session
from app.config import settings
from app.storage.models import Device
from app.storage.postgres import AsyncSessionLocal
from app.storage.redis_client import get_redis
from app.ws_manager import manager as ws_manager

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


async def _has_agent(profile_id: str) -> bool:
    """Whether this profile has a desktop agent registered.

    Gates the Device Activity page. Previously a `da_desktop` cookie, which the
    desktop login only ever set on the one browser it opened — every other
    browser on the same machine got the "Download & Install" screen while the
    agent was running and its data was already in the DB.
    """
    async with AsyncSessionLocal() as db:
        return (await db.execute(
            select(Device.id).where(Device.profile_id == profile_id).limit(1)
        )).first() is not None


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    profile_id = await get_profile_from_session(request)
    if profile_id is None:
        return templates.TemplateResponse(request=request, name="homepage.html")
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "logged_in": True,
            "github_app_slug": settings.GITHUB_APP_SLUG,
            "app_base_url": settings.APP_BASE_URL,
            "active_page": "overview",
        },
    )


@router.get("/github", response_class=HTMLResponse)
async def github_page(request: Request):
    if not await get_profile_from_session(request):
        return RedirectResponse("/")
    return templates.TemplateResponse(request=request, name="github.html", context={"active_page": "github"})


@router.get("/jira", response_class=HTMLResponse)
async def jira_page(request: Request):
    if not await get_profile_from_session(request):
        return RedirectResponse("/")
    return templates.TemplateResponse(request=request, name="jira.html", context={"active_page": "jira"})


@router.get("/teams-outlook", response_class=HTMLResponse)
async def teams_outlook_page(request: Request):
    """One activity calendar over both connectors. Replaces the separate /teams
    and /outlook mock pages — the spec is a single month grid, not two.

    Communication metadata is admin-granted: holding either connector permission
    opens the page, and the calendar itself shows only what that permission covers.
    """
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return RedirectResponse("/")
    from app.auth.rbac import granted
    from app.storage.models import Profile
    from app.storage.postgres import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        profile = await db.get(Profile, profile_id)
    if not profile:
        return RedirectResponse("/")
    perms = granted(profile)
    if not {"teams_activity", "outlook_activity"} & set(perms):
        return RedirectResponse("/")
    return templates.TemplateResponse(
        request=request, name="teams_outlook.html",
        context={"active_page": "teams_outlook",
                 "show_teams": "teams_activity" in perms,
                 "show_outlook": "outlook_activity" in perms},
    )


@router.get("/ai", response_class=HTMLResponse)
async def ai_page(request: Request):
    if not await get_profile_from_session(request):
        return RedirectResponse("/")
    return templates.TemplateResponse(request=request, name="ai.html", context={"active_page": "ai"})


@router.get("/help", response_class=HTMLResponse)
async def help_page(request: Request):
    if not await get_profile_from_session(request):
        return RedirectResponse("/")
    return templates.TemplateResponse(request=request, name="help.html", context={"active_page": "help"})


@router.get("/gitlab", response_class=HTMLResponse)
async def gitlab_page(request: Request):
    if not await get_profile_from_session(request):
        return RedirectResponse("/")
    return templates.TemplateResponse(request=request, name="gitlab.html", context={"active_page": "gitlab"})


@router.get("/my-activity", response_class=HTMLResponse)
async def my_activity_page(request: Request):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return RedirectResponse("/auth/login?next=/my-activity&desktop=1")
    return templates.TemplateResponse(
        request=request,
        name="my_activity.html",
        context={"active_page": "my_activity", "is_desktop": await _has_agent(profile_id)},
    )


@router.get("/my-activity/ai-tools", response_class=HTMLResponse)
async def ai_tools_page(
    request: Request,
    date: str = "",
    week: str = "",
):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return RedirectResponse("/auth/login?next=/my-activity/ai-tools")
    if not await _has_agent(profile_id):
        return RedirectResponse("/my-activity")
    init_mode = "week" if week else "day"
    init_date = date or ""
    init_week = week or ""
    return templates.TemplateResponse(
        request=request,
        name="agent_ai_tools.html",
        context={
            "active_page": "my_activity",
            "init_mode":   init_mode,
            "init_date":   init_date,
            "init_week":   init_week,
        },
    )


@router.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request):
    if not await get_profile_from_session(request):
        return RedirectResponse("/")
    return templates.TemplateResponse(request=request, name="analytics.html", context={"active_page": "analytics"})


@router.get("/my-day", response_class=HTMLResponse)
async def my_day_page(request: Request):
    if not await get_profile_from_session(request):
        return RedirectResponse("/")
    return templates.TemplateResponse(request=request, name="my_day.html", context={"active_page": "my_day"})


@router.get("/email", response_class=HTMLResponse)
async def email_page(request: Request):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return RedirectResponse("/")
    from app.auth.rbac import granted
    from app.storage.models import Profile
    from app.storage.postgres import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        profile = await db.get(Profile, profile_id)
    if not profile or "email_report" not in granted(profile):
        return RedirectResponse("/")
    return templates.TemplateResponse(request=request, name="email.html", context={"active_page": "email"})


@router.get("/report", response_class=HTMLResponse)
async def report_page(request: Request):
    """Attendance report — the people × days grid. Row-scoped by role; a plain user
    sees only their own row."""
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return RedirectResponse("/")
    from app.auth.rbac import granted
    from app.storage.models import Profile
    from app.storage.postgres import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        profile = await db.get(Profile, profile_id)
    if not profile or "attendance_report" not in granted(profile):
        return RedirectResponse("/")
    return templates.TemplateResponse(
        request=request, name="attendance.html",
        context={"active_page": "report", "is_elevated": profile.role in ("manager", "admin")},
    )


@router.get("/report/summary", response_class=HTMLResponse)
async def summary_page(request: Request):
    """Consolidated report — the AI narrative summary over a custom date range."""
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return RedirectResponse("/")
    from app.auth.rbac import granted
    from app.storage.models import Profile
    from app.storage.postgres import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        profile = await db.get(Profile, profile_id)
    if not profile or "consolidated_report" not in granted(profile):
        return RedirectResponse("/")
    return templates.TemplateResponse(
        request=request, name="consolidated.html", context={"active_page": "summary"},
    )


@router.get("/user-management", response_class=HTMLResponse)
async def user_management_page(request: Request):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return RedirectResponse("/")
    from app.storage.models import Profile
    from app.storage.postgres import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        profile = await db.get(Profile, profile_id)
    if not profile or profile.role not in ("manager", "admin"):
        return RedirectResponse("/")
    return templates.TemplateResponse(
        request=request, name="user_management.html",
        context={"active_page": "user_management", "can_edit": profile.role == "admin"},
    )


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    token = websocket.cookies.get("session")
    profile_id = None
    if token:
        redis = get_redis()
        profile_id = await redis.get(f"session:{token}")
    if not profile_id:
        await websocket.close(code=4001)
        return
    pid = str(profile_id)
    await ws_manager.connect(websocket, pid)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket, pid)
