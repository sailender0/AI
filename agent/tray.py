"""
System tray icon for the Developer Activity desktop app.
Manages: agent thread, webview window, SSO re-auth.
"""
import logging
import platform
import socket
import threading
from pathlib import Path

log = logging.getLogger(__name__)

# ── Icon ──────────────────────────────────────────────────────────────────────
# Minimal 32x32 PNG icon encoded inline so no external file is needed.
# Green circle = connected, red = disconnected.

_ICON_CONNECTED    = None  # loaded lazily from icon_connected.png if present
_ICON_DISCONNECTED = None

def _load_icon(connected: bool):
    """Return a PIL Image for the tray icon."""
    from PIL import Image, ImageDraw
    img  = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    color = "#22c55e" if connected else "#ef4444"
    draw  = ImageDraw.Draw(img)
    # Outer circle (dark)
    draw.ellipse([2, 2, 62, 62], fill="#1a1a2e")
    # Status dot
    draw.ellipse([18, 18, 46, 46], fill=color)
    return img


# ── Tray ──────────────────────────────────────────────────────────────────────

class AgentTray:
    def __init__(self, backend: str):
        self.backend    = backend
        self._icon      = None
        self._connected = False
        self._agent_thread: threading.Thread | None = None
        self._webview_thread: threading.Thread | None = None

    # ── Status callback (called from agent loop) ──────────────────────────────

    def set_connected(self, connected: bool):
        if connected == self._connected:
            return
        self._connected = connected
        if self._icon:
            self._icon.icon = _load_icon(connected)
            title = "Developer Activity — Connected" if connected else "Developer Activity — Offline"
            self._icon.title = title

    # ── Menu actions ──────────────────────────────────────────────────────────

    def _open_dashboard(self, icon=None, item=None):
        from agent.webview import open_window
        if self._webview_thread and self._webview_thread.is_alive():
            return
        self._webview_thread = threading.Thread(
            target=open_window, args=(self.backend,), daemon=True
        )
        self._webview_thread.start()

    def _reconnect(self, icon=None, item=None):
        """Trigger SSO re-auth flow."""
        import platform as _pl
        from agent.auth import run_sso_flow, clear_credentials
        clear_credentials()
        device_name = _pl.node()
        threading.Thread(
            target=run_sso_flow,
            args=(self.backend, device_name, self._on_reauth),
            daemon=True,
        ).start()

    def _on_reauth(self, success: bool):
        if success:
            self._start_agent()
            log.info("Re-authenticated successfully")
        else:
            log.error("Re-authentication failed")

    def _quit(self, icon=None, item=None):
        if self._icon:
            self._icon.stop()

    # ── Agent thread ──────────────────────────────────────────────────────────

    def _start_agent(self):
        from agent.agent import load_token, load_backend, run
        token   = load_token()
        backend = load_backend()
        if not token:
            return
        if self._agent_thread and self._agent_thread.is_alive():
            return
        self._agent_thread = threading.Thread(
            target=run,
            args=(token, backend, self.set_connected),
            daemon=True,
        )
        self._agent_thread.start()

    # ── Entry point ───────────────────────────────────────────────────────────

    def run(self):
        """Start the tray icon (blocks until quit)."""
        try:
            import pystray
            from PIL import Image
        except ImportError:
            log.error("pystray / Pillow not installed — run: pip install pystray pillow")
            return

        menu = pystray.Menu(
            pystray.MenuItem("Open Dashboard",  self._open_dashboard, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Reconnect",       self._reconnect),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit",            self._quit),
        )

        self._icon = pystray.Icon(
            name="da-agent",
            icon=_load_icon(False),
            title="Developer Activity",
            menu=menu,
        )

        # Start agent in background
        self._start_agent()

        # Open dashboard on first run
        threading.Thread(target=self._open_dashboard, daemon=True).start()

        log.info("Tray icon running")
        self._icon.run()  # blocks


def main(backend: str):
    from agent.auth import is_authenticated, run_sso_flow
    import platform as _pl

    if not is_authenticated():
        log.info("Not authenticated — starting SSO flow")
        device_name = _pl.node()
        ok = run_sso_flow(backend, device_name)
        if not ok:
            log.error("Authentication failed — exiting")
            return

    tray = AgentTray(backend)
    tray.run()
