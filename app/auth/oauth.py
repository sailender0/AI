"""
One-time OAuth for GitHub, GitLab, and Jira.
Tokens are encrypted before storage. Webhook registration fires immediately after.
"""
import secrets
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.auth.sso import get_profile_from_session
from app.auth.token_store import decrypt_token, encrypt_token
from app.config import settings
from app.storage.models import Integration
from app.storage.postgres import AsyncSessionLocal
from app.storage.redis_client import get_redis

router = APIRouter()

_OAUTH_CONFIGS = {
    "github": {
        "auth_url": "https://github.com/login/oauth/authorize",
        "token_url": "https://github.com/login/oauth/access_token",
        "client_id": lambda: settings.GITHUB_CLIENT_ID,
        "client_secret": lambda: settings.GITHUB_CLIENT_SECRET,
        "scopes": "read:org,read:user,repo",
    },
    "gitlab": {
        "auth_url": "https://gitlab.com/oauth/authorize",
        "token_url": "https://gitlab.com/oauth/token",
        "client_id": lambda: settings.GITLAB_CLIENT_ID,
        "client_secret": lambda: settings.GITLAB_CLIENT_SECRET,
        "scopes": "read_api read_user",
    },
    "jira": {
        "auth_url": "https://auth.atlassian.com/authorize",
        "token_url": "https://auth.atlassian.com/oauth/token",
        "client_id": lambda: settings.JIRA_CLIENT_ID,
        "client_secret": lambda: settings.JIRA_CLIENT_SECRET,
        "scopes": "read:jira-work read:jira-user manage:jira-webhook",
    },
}


@router.get("/connect/{app}")
async def connect_app(app: str, request: Request):
    if app not in _OAUTH_CONFIGS:
        return {"error": "unsupported_app"}

    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return {"error": "not_authenticated"}

    cfg = _OAUTH_CONFIGS[app]
    state = secrets.token_urlsafe(16)
    redis = get_redis()
    await redis.set(f"oauth_state:{app}:{state}", profile_id, ex=600)

    params = {
        "client_id": cfg["client_id"](),
        "redirect_uri": f"{settings.APP_BASE_URL}/oauth/callback/{app}",
        "scope": cfg["scopes"],
        "state": state,
        "response_type": "code",
    }
    if app == "jira":
        params["audience"] = "api.atlassian.com"
        params["prompt"] = "consent"

    url = cfg["auth_url"] + "?" + "&".join(f"{k}={v}" for k, v in params.items())
    return RedirectResponse(url)


@router.get("/oauth/callback/{app}")
async def oauth_callback(app: str, request: Request, code: str, state: str):
    if app not in _OAUTH_CONFIGS:
        return {"error": "unsupported_app"}

    redis = get_redis()
    profile_id = await redis.get(f"oauth_state:{app}:{state}")
    if not profile_id:
        return {"error": "invalid_state"}
    await redis.delete(f"oauth_state:{app}:{state}")

    cfg = _OAUTH_CONFIGS[app]
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            cfg["token_url"],
            data={
                "code": code,
                "client_id": cfg["client_id"](),
                "client_secret": cfg["client_secret"](),
                "redirect_uri": f"{settings.APP_BASE_URL}/oauth/callback/{app}",
                "grant_type": "authorization_code",
            },
            headers={"Accept": "application/json"},
        )
    data = resp.json()

    access_token = data.get("access_token", "")
    refresh_token = data.get("refresh_token", "")
    expires_in = data.get("expires_in", 3600)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(Integration).where(
                    Integration.profile_id == profile_id,
                    Integration.source == app,
                )
            )
        ).scalar_one_or_none()

        if not row:
            row = Integration(profile_id=profile_id, source=app)
            db.add(row)

        row.access_token_enc = encrypt_token(access_token)
        row.refresh_token_enc = encrypt_token(refresh_token) if refresh_token else None
        row.token_expires_at = expires_at
        row.sync_status = "active"
        await db.commit()

    # For Jira, fetch the user's account ID and store as LinkedIdentity
    # so incoming webhooks can be resolved to this profile
    if app == "jira" and access_token:
        import logging as _log
        async with httpx.AsyncClient() as client:
            me_resp = await client.get(
                "https://api.atlassian.com/me",
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
        _log.getLogger(__name__).info("Jira /me: %s %s", me_resp.status_code, me_resp.text)
        if me_resp.status_code == 200:
            account_id = me_resp.json().get("account_id", "")
            _log.getLogger(__name__).info("Jira account_id: %r", account_id)
            if account_id:
                from app.storage.models import LinkedIdentity
                async with AsyncSessionLocal() as db:
                    existing = (
                        await db.execute(
                            select(LinkedIdentity).where(
                                LinkedIdentity.profile_id == profile_id,
                                LinkedIdentity.provider == "jira",
                            )
                        )
                    ).scalar_one_or_none()
                    if existing:
                        existing.tenant_id = account_id
                    else:
                        db.add(LinkedIdentity(
                            profile_id=profile_id,
                            provider="jira",
                            tenant_id=account_id,
                        ))
                    await db.commit()
                    _log.getLogger(__name__).info("Saved Jira LinkedIdentity: %s -> %s", account_id, profile_id)

    from app.webhooks.registration import auto_register_webhook
    import asyncio
    asyncio.create_task(auto_register_webhook(app, profile_id))

    return RedirectResponse(url=f"/{app}", status_code=302)


async def get_valid_token(profile_id: str, source: str) -> str | None:
    """Return a valid decrypted access token, refreshing if needed."""
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(Integration).where(
                    Integration.profile_id == profile_id,
                    Integration.source == source,
                )
            )
        ).scalar_one_or_none()

        if not row:
            return None

        needs_refresh = (
            row.token_expires_at is not None
            and row.token_expires_at < datetime.now(timezone.utc) + timedelta(minutes=5)
        )

        if needs_refresh and row.refresh_token_enc:
            try:
                new_tokens = await _refresh_token(source, decrypt_token(row.refresh_token_enc))
                row.access_token_enc = encrypt_token(new_tokens["access_token"])
                if new_tokens.get("refresh_token"):
                    row.refresh_token_enc = encrypt_token(new_tokens["refresh_token"])
                row.token_expires_at = datetime.now(timezone.utc) + timedelta(
                    seconds=new_tokens.get("expires_in", 3600)
                )
                await db.commit()
            except Exception:
                row.sync_status = "error"
                await db.commit()
                return None

        return decrypt_token(row.access_token_enc) if row.access_token_enc else None


async def _refresh_token(source: str, refresh_token: str) -> dict:
    cfg = _OAUTH_CONFIGS[source]
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            cfg["token_url"],
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": cfg["client_id"](),
                "client_secret": cfg["client_secret"](),
            },
            headers={"Accept": "application/json"},
        )
    resp.raise_for_status()
    return resp.json()
