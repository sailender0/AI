import logging
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.config import settings

_request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

logger = logging.getLogger(__name__)

_SKIP_LOG_PATHS = {"/health", "/favicon.ico"}

# Static security headers set on every response. No CSP script-src yet: the pages
# use inline on* handlers and CDN-hosted Chart.js/Alpine, so a strict script-src
# would break the UI — add it once those move to /static and nonces. frame-ancestors
# 'none' + X-Frame-Options DENY both block framing (clickjacking) for old and new UAs.
_SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Content-Security-Policy": "frame-ancestors 'none'",
}
# HSTS only makes sense once the app is actually served over TLS. APP_BASE_URL is
# the app's own view of its scheme (works behind a TLS-terminating proxy, where
# request.url.scheme reads http).
_IS_HTTPS = settings.APP_BASE_URL.startswith("https://")


def get_request_id() -> str:
    return _request_id_var.get()


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = str(uuid.uuid4())[:8]
        token = _request_id_var.set(request_id)
        request.state.request_id = request_id
        try:
            if request.url.path not in _SKIP_LOG_PATHS:
                logger.info("[%s] %s %s", request_id, request.method, request.url.path)
            response = await call_next(request)
            if request.url.path not in _SKIP_LOG_PATHS:
                logger.info("[%s] -> %d", request_id, response.status_code)
        except Exception:
            logger.exception("[%s] unhandled error", request_id)
            raise
        finally:
            _request_id_var.reset(token)
        response.headers["X-Request-ID"] = request_id
        for k, v in _SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        if _IS_HTTPS:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        # Server-rendered pages carry auth-coupled JS — never let a client cache
        # them. A cached page outlives its session and template: the agent webview
        # resurrected a months-old page that polled dead endpoints 401/403 forever.
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-store"
        return response
