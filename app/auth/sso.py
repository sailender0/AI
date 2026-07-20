"""
Entra ID (Azure AD) SSO — MSAL-based auth code flow.

FIX (issue #3): We store the MSAL token cache per user so that
auto_register_teams_subscription can call acquire_token_silent with
a delegated token instead of app-only, which is required for
the me/messages Graph subscription resource.
"""
import json
import secrets
from datetime import datetime, timezone
from urllib.parse import urlparse

import msal
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.config import settings
from app.storage.models import LinkedIdentity, Profile
from app.storage.postgres import AsyncSessionLocal
from app.storage.redis_client import get_redis

router = APIRouter()

_GRAPH_SCOPES = ["Mail.Send"]  # delegated send-as-self for email reports (ADR email delivery)

AUTHORITY = f"https://login.microsoftonline.com/{settings.AZURE_TENANT_ID}"


def _build_msal_app(cache: msal.SerializableTokenCache | None = None) -> msal.ConfidentialClientApplication:
    return msal.ConfidentialClientApplication(
        settings.AZURE_CLIENT_ID,
        authority=AUTHORITY,
        client_credential=settings.AZURE_CLIENT_SECRET,
        token_cache=cache,
    )


async def _load_cache(profile_id: str) -> msal.SerializableTokenCache:
    redis = get_redis()
    raw = await redis.get(f"msal_cache:{profile_id}")
    cache = msal.SerializableTokenCache()
    if raw:
        cache.deserialize(raw)
    return cache


async def _save_cache(profile_id: str, cache: msal.SerializableTokenCache):
    if cache.has_state_changed:
        redis = get_redis()
        await redis.set(f"msal_cache:{profile_id}", cache.serialize(), ex=86400 * 30)


async def acquire_delegated_token(profile_id: str) -> str | None:
    """Silently acquire a delegated Graph token using the stored MSAL cache."""
    cache = await _load_cache(profile_id)
    app = _build_msal_app(cache)
    accounts = app.get_accounts()
    if not accounts:
        return None
    result = app.acquire_token_silent(_GRAPH_SCOPES, account=accounts[0])
    await _save_cache(profile_id, cache)
    if result and "access_token" in result:
        return result["access_token"]
    return None


def _is_local_callback(url: str) -> bool:
    """True only for a real http(s)://localhost or 127.0.0.1 callback (any port/path).
    startswith() is bypassable via userinfo (http://localhost:@evil.com) or a bogus
    port (localhost:80.evil.com); urlparse isolates the true host so the device
    token can't be redirected off-box. Accessing .port forces port validation."""
    try:
        u = urlparse(url)
        _ = u.port  # raises ValueError on a non-numeric port
    except ValueError:
        return False
    return u.scheme in ("http", "https") and u.hostname in ("localhost", "127.0.0.1")


@router.get("/auth/login")
async def login(request: Request):
    state = secrets.token_urlsafe(16)
    next_url = request.query_params.get("next", "/")
    # Only allow relative paths to prevent open-redirect
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/"
    is_desktop    = request.query_params.get("desktop") == "1"
    agent_callback = request.query_params.get("agent_callback", "")
    device_name   = request.query_params.get("device_name", "Desktop")
    # Validate: agent_callback must be a real localhost URL (no open redirect).
    # The device token is appended to this URL and the browser redirected to it,
    # so a spoofed host would leak the token — see _is_local_callback.
    if agent_callback and not _is_local_callback(agent_callback):
        agent_callback = ""
    redis = get_redis()
    await redis.set(f"oauth_state:{state}", next_url, ex=600)
    if is_desktop:
        await redis.set(f"oauth_desktop:{state}", "1", ex=600)
    if agent_callback:
        import json as _json
        await redis.set(f"oauth_agent:{state}", _json.dumps({"callback": agent_callback, "device": device_name}), ex=600)

    app = _build_msal_app()
    auth_url = app.get_authorization_request_url(
        _GRAPH_SCOPES,
        state=state,
        redirect_uri=f"{settings.APP_BASE_URL}/auth/callback",
    )
    return RedirectResponse(auth_url)


@router.get("/auth/callback")
async def auth_callback(request: Request, code: str, state: str):
    redis = get_redis()
    next_url = await redis.get(f"oauth_state:{state}")
    if not next_url:
        return {"error": "invalid_state"}
    is_desktop = await redis.get(f"oauth_desktop:{state}") == "1"
    agent_meta_raw = await redis.get(f"oauth_agent:{state}")
    await redis.delete(f"oauth_state:{state}")
    await redis.delete(f"oauth_desktop:{state}")
    await redis.delete(f"oauth_agent:{state}")
    agent_meta = {}
    if agent_meta_raw:
        import json as _json
        try:
            agent_meta = _json.loads(agent_meta_raw)
        except Exception:
            pass

    cache = msal.SerializableTokenCache()
    app = _build_msal_app(cache)
    result = app.acquire_token_by_authorization_code(
        code,
        scopes=_GRAPH_SCOPES,
        redirect_uri=f"{settings.APP_BASE_URL}/auth/callback",
    )
    if "error" in result:
        return {"error": result.get("error_description")}

    claims = result.get("id_token_claims", {})
    entra_id = claims.get("oid") or claims.get("sub")
    email = claims.get("preferred_username", "")
    tenant_id = claims.get("tid", "")
    teams_user_id = claims.get("oid", "")

    async with AsyncSessionLocal() as db:
        profile = (await db.execute(select(Profile).where(Profile.entra_id == entra_id))).scalar_one_or_none()
        if not profile:
            profile = Profile(entra_id=entra_id, email=email, teams_user_id=teams_user_id)
            db.add(profile)
            await db.flush()

            identity = LinkedIdentity(
                profile_id=profile.id,
                provider="entra",
                tenant_id=tenant_id,
            )
            db.add(identity)
        else:
            profile.teams_user_id = teams_user_id
        await db.commit()
        await db.refresh(profile)

    profile_id = str(profile.id)

    # Persist MSAL cache so Teams subscription renewal can use delegated token
    await _save_cache(profile_id, cache)

    session_token = secrets.token_urlsafe(32)
    await redis.set(f"session:{session_token}", profile_id, ex=86400)

    is_https  = settings.APP_BASE_URL.startswith("https://")
    safe_next = next_url if (next_url and next_url.startswith("/") and not next_url.startswith("//")) else "/"

    # If desktop agent callback present, generate a device token and redirect
    # to the local callback server instead of the app page directly.
    if agent_meta.get("callback"):
        import hashlib as _hl
        from app.storage.models import Device, DeviceToken
        raw_token  = secrets.token_urlsafe(48)
        token_hash = _hl.sha256(raw_token.encode()).hexdigest()
        async with AsyncSessionLocal() as db:
            device = Device(
                profile_id=profile_id,
                name=agent_meta.get("device", "Desktop")[:100],
                platform="windows",
                registered_at=datetime.now(timezone.utc),
            )
            db.add(device)
            await db.flush()
            db.add(DeviceToken(device_id=device.id, token_hash=token_hash))
            await db.commit()
        # Redirect to agent local server with the token
        # The agent server will then redirect browser to safe_next
        from urllib.parse import urlencode
        params    = urlencode({"token": raw_token, "next": f"{settings.APP_BASE_URL}{safe_next}"})
        cb_url    = f"{agent_meta['callback']}?{params}"
        response  = RedirectResponse(url=cb_url)
        response.set_cookie("session", session_token, httponly=True, secure=is_https, samesite="lax")
        response.set_cookie("da_desktop", "1", httponly=False, secure=is_https, samesite="lax", max_age=86400 * 30)
        return response

    response = RedirectResponse(url=safe_next)
    response.set_cookie("session", session_token, httponly=True, secure=is_https, samesite="lax")
    if is_desktop:
        response.set_cookie("da_desktop", "1", httponly=False, secure=is_https, samesite="lax", max_age=86400 * 30)
    return response


@router.get("/auth/logout")
async def logout(request: Request):
    token = request.cookies.get("session")
    if token:
        await get_redis().delete(f"session:{token}")
    response = RedirectResponse(url="/")
    response.delete_cookie("session")
    return response


async def get_profile_from_session(request: Request) -> str | None:
    token = request.cookies.get("session")
    if not token:
        return None
    redis = get_redis()
    val = await redis.get(f"session:{token}")
    if val is None:
        return None
    return val.decode() if isinstance(val, bytes) else val


async def require_profile(request: Request) -> str:
    """Dependency form of get_profile_from_session: 401s instead of returning None.
    API routes take `profile_id: str = Depends(require_profile)`; only routes with
    a non-401 unauthenticated path (e.g. /api/me, HTML pages) call the raw getter."""
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        raise HTTPException(401, "not_authenticated")
    return profile_id
