"""
App orchestrator — window is primary, tray is minimize-to-tray.

Threading model:
  Main thread  : webview.start() — blocks until Quit
  Daemon thread : pystray icon.run()
  Daemon thread : agent run() loop
  Daemon thread : SSO callback server (only during first login)
  Daemon thread : single-instance IPC server
"""
import logging
import platform
import threading

log = logging.getLogger(__name__)

_stop = threading.Event()  # signals agent thread to stop cleanly


def _make_icon(connected: bool):
    from PIL import Image, ImageDraw
    img  = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([2, 2, 62, 62], fill="#1a1a2e")
    draw.ellipse([18, 18, 46, 46], fill="#22c55e" if connected else "#6e7681")
    return img


def main(backend: str, startup_mode: bool = False, ipc_server=None):
    try:
        import webview
        import pystray
    except ImportError as exc:
        log.error("Missing dependency: %s  —  pip install pywebview pystray pillow", exc)
        return

    from agent.auth import is_authenticated, run_sso_in_window
    from agent.agent import load_token, run as run_agent

    _icon        = None
    _agent_thread = None

    # ── Agent ─────────────────────────────────────────────────────────────────

    def start_agent(token: str):
        nonlocal _agent_thread
        if _agent_thread and _agent_thread.is_alive():
            return

        def _status(connected: bool):
            if _icon:
                _icon.icon = _make_icon(connected)

        _agent_thread = threading.Thread(
            target=run_agent,
            args=(token, backend),
            kwargs={"on_status": _status, "stop_event": _stop},
            daemon=True,
        )
        _agent_thread.start()

    # ── Window ────────────────────────────────────────────────────────────────

    # Start hidden; show after auth check in on_start()
    initial_url = f"{backend}/my-activity?_dt=1" if is_authenticated() else f"{backend}/"
    window = webview.create_window(
        title="Developer Activity",
        url=initial_url,
        width=1280,
        height=800,
        min_size=(800, 600),
        text_select=True,
        hidden=True,          # shown explicitly once ready
    )

    def on_closing():
        """Hide to tray instead of closing."""
        window.hide()
        return False  # cancel the close

    window.events.closing += on_closing

    # ── Tray ──────────────────────────────────────────────────────────────────

    def _show(icon=None, item=None):
        window.show()

    def _quit(icon=None, item=None):
        _stop.set()
        if _icon:
            _icon.stop()
        window.destroy()  # makes webview.start() return

    _icon = pystray.Icon(
        "da-agent",
        _make_icon(False),
        "Developer Activity",
        menu=pystray.Menu(
            pystray.MenuItem("Open", _show, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", _quit),
        ),
    )
    threading.Thread(target=_icon.run, daemon=True).start()

    # IPC: second instance sends "show" → bring window to front
    if ipc_server:
        ipc_server(lambda: window.show())

    # ── Webview start callback ────────────────────────────────────────────────

    def on_start():
        """Runs in a worker thread after webview initialises."""
        if is_authenticated():
            token = load_token()
            if token:
                start_agent(token)
                if not startup_mode:
                    window.show()
            else:
                # Token missing from keyring despite is_authenticated passing — re-auth
                _do_sso()
        else:
            if startup_mode:
                # No credentials and launched at startup — wait silently for user to open
                log.info("Startup mode: no credentials — waiting for user to open app")
            else:
                _do_sso()

    def _do_sso():
        window.show()
        device_name = platform.node()

        def on_auth(token):
            if token:
                start_agent(token)
            else:
                log.error("Authentication failed or timed out")

        run_sso_in_window(window, backend, device_name, on_auth)

    # ── Block main thread in webview event loop ───────────────────────────────
    webview.start(on_start, debug=False)

    # webview.start() returns only when window.destroy() is called (Quit)
    _stop.set()
