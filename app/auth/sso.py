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

import msal
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.config import settings
from app.storage.models import LinkedIdentity, Profile
from app.storage.postgres import AsyncSessionLocal
from app.storage.redis_client import get_redis

router = APIRouter()

_GRAPH_SCOPES = []  # Chat.Read removed for local dev — Teams subscription skipped

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


@router.get("/auth/login")
async def login(request: Request):
    state = secrets.token_urlsafe(16)
    redis = get_redis()
    await redis.set(f"oauth_state:{state}", "pending", ex=600)

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
    if not await redis.get(f"oauth_state:{state}"):
        return {"error": "invalid_state"}
    await redis.delete(f"oauth_state:{state}")

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

    is_https = settings.APP_BASE_URL.startswith("https://")
    response = RedirectResponse(url="/")
    response.set_cookie("session", session_token, httponly=True, secure=is_https, samesite="lax")
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
