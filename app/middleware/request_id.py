import logging
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.config import settings

_request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

logger = logging.getLogger(__name__)

_SKIP_LOG_PATHS = {"/health", "/favicon.ico"}

_SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Content-Security-Policy": "frame-ancestors 'none'",
}
_IS_HTTPS = settings.APP_BASE_URL.startswith("https://")


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
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-store"
        return response
