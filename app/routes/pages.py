from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth.sso import get_profile_from_session
from app.config import settings
from app.storage.redis_client import get_redis
from app.ws_manager import manager as ws_manager

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    profile_id = await get_profile_from_session(request)
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "logged_in": profile_id is not None,
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


@router.get("/teams", response_class=HTMLResponse)
async def teams_page(request: Request):
    if not await get_profile_from_session(request):
        return RedirectResponse("/")
    return templates.TemplateResponse(request=request, name="teams.html", context={"active_page": "teams"})


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
    if not await get_profile_from_session(request):
        return RedirectResponse("/auth/login?next=/my-activity&desktop=1")
    is_desktop = request.cookies.get("da_desktop") == "1"
    response = templates.TemplateResponse(
        request=request,
        name="my_activity.html",
        context={"active_page": "my_activity", "is_desktop": is_desktop},
    )
    # If opened with ?_dt=1 (desktop app first load), set the cookie
    if request.query_params.get("_dt") == "1" and not is_desktop:
        is_https = settings.APP_BASE_URL.startswith("https://")
        response.set_cookie("da_desktop", "1", httponly=False, secure=is_https, samesite="lax", max_age=86400 * 30)
    return response


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
