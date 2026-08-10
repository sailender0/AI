"""SSO auth flow — runs inside the pywebview window."""
import http.server
import logging
import socket
import threading
from urllib.parse import parse_qs, urlencode, urlparse

import keyring

from agent.agent import KEYRING_SERVICE, KEYRING_TOKEN_KEY, KEYRING_URL_KEY

log = logging.getLogger(__name__)

_SSO_TIMEOUT = 300


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def run_sso_in_window(window, backend: str, device_name: str, on_complete) -> None:
    """
    Navigate `window` through the SSO flow.
    Saves device token to keyring on success.
    Calls on_complete(token: str | None) from a background thread.
    """
    port         = _free_port()
    callback_url = f"http://localhost:{port}/auth"
    _result      = {}
    _done        = threading.Event()

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_GET(self):
            parsed = urlparse(self.path)
            qs     = parse_qs(parsed.query)
            token  = (qs.get("token") or [""])[0]
            next_  = (qs.get("next")  or [f"{backend}/my-activity?_dt=1"])[0]

            if parsed.path == "/auth" and token:
                _result["token"] = token
                self.send_response(302)
                self.send_header("Location", next_)
                self.end_headers()
                _done.set()
            else:
                self.send_response(404)
                self.end_headers()

    httpd = http.server.HTTPServer(("localhost", port), _Handler)

    def _serve():
        while not _done.is_set():
            httpd.handle_request()
        httpd.server_close()

    threading.Thread(target=_serve, daemon=True).start()

    query = urlencode({
        "next": "/my-activity?_dt=1",
        "desktop": "1",
        "agent_callback": callback_url,
        "device_name": device_name,
    })
    login_url = f"{backend}/auth/login?{query}"
    window.load_url(login_url)
    log.info("SSO started — waiting for user to log in")

    def _wait():
        _done.wait(timeout=_SSO_TIMEOUT)
        token = _result.get("token")
        if token:
            keyring.set_password(KEYRING_SERVICE, KEYRING_TOKEN_KEY, token)
            keyring.set_password(KEYRING_SERVICE, KEYRING_URL_KEY, backend)
            log.info("Device token saved to keyring")
        else:
            log.error("SSO timed out or was cancelled")
        on_complete(token)

    threading.Thread(target=_wait, daemon=True).start()


def is_authenticated() -> bool:
    return bool(keyring.get_password(KEYRING_SERVICE, KEYRING_TOKEN_KEY))
