"""
SSO auth flow for desktop app.

Spins up a local HTTP server on a random port, opens the browser to the
app's SSO login, waits for the callback carrying the device token, saves
it to the OS keyring, then redirects the browser to /my-activity.
"""
import http.server
import logging
import platform
import socket
import threading
import webbrowser
from urllib.parse import parse_qs, urlencode, urlparse

import keyring

from agent.agent import KEYRING_SERVICE, KEYRING_TOKEN_KEY, KEYRING_URL_KEY

log = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 120

_HTML_FAIL = b"""<!DOCTYPE html><html><head><style>
body{font-family:system-ui,sans-serif;display:flex;align-items:center;
justify-content:center;height:100vh;margin:0;background:#09090B;color:#ececf0}
.card{text-align:center;padding:2rem}h2{color:#ef4444;margin:0 0 .5rem}p{color:#9b9bad;margin:0}
</style></head><body><div class="card"><h2>Connection failed</h2>
<p>Please try again from the desktop app.</p></div></body></html>"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def run_sso_flow(backend: str, device_name: str, on_complete=None) -> bool:
    """
    Opens browser to SSO, waits for callback with device token.
    Returns True on success.
    """
    port         = _free_port()
    result_store: dict = {}
    done_event   = threading.Event()
    callback_url = f"http://localhost:{port}/auth"

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path != "/auth":
                self.send_response(404)
                self.end_headers()
                return

            params       = parse_qs(parsed.query)
            token        = (params.get("token") or [""])[0]
            next_full    = (params.get("next")  or [""])[0]  # full URL to redirect browser to

            if token:
                result_store["token"] = token
                # Redirect browser to the app's /my-activity page
                redirect_to = next_full or f"{backend}/my-activity?_dt=1"
                self.send_response(302)
                self.send_header("Location", redirect_to)
                self.end_headers()
            else:
                self.send_response(400)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(_HTML_FAIL)

            done_event.set()

    httpd  = http.server.HTTPServer(("localhost", port), _Handler)
    thread = threading.Thread(target=httpd.handle_request, daemon=True)
    thread.start()

    # Open browser to SSO — backend will redirect to callback_url with token
    device_name_encoded = device_name.replace(" ", "+")
    login_url = (
        f"{backend}/auth/login"
        f"?next=/my-activity%3F_dt%3D1"
        f"&desktop=1"
        f"&agent_callback={callback_url}"
        f"&device_name={device_name_encoded}"
    )
    log.info("Opening browser for SSO…")
    webbrowser.open(login_url)

    done_event.wait(timeout=_TIMEOUT_SECONDS)
    httpd.server_close()

    token = result_store.get("token")
    if not token:
        log.error("SSO timed out or cancelled")
        if on_complete:
            on_complete(False)
        return False

    # Save to OS keyring (Windows Credential Manager / macOS Keychain)
    keyring.set_password(KEYRING_SERVICE, KEYRING_TOKEN_KEY, token)
    keyring.set_password(KEYRING_SERVICE, KEYRING_URL_KEY, backend)
    log.info("Device token saved to OS keyring")

    if on_complete:
        on_complete(True)
    return True


def is_authenticated() -> bool:
    return bool(keyring.get_password(KEYRING_SERVICE, KEYRING_TOKEN_KEY))


def clear_credentials() -> None:
    for key in (KEYRING_TOKEN_KEY, KEYRING_URL_KEY):
        try:
            keyring.delete_password(KEYRING_SERVICE, key)
        except Exception:
            pass
