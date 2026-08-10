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

_stop = threading.Event()


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


    def start_agent(token: str):
        nonlocal _agent_thread
        if _agent_thread and _agent_thread.is_alive():
            return

        def _status(connected: bool):
            if _icon:
                _icon.icon = _make_icon(connected)

        def _notify(title: str, body: str):
            if _icon:
                try:
                    _icon.notify(body, title)
                except Exception as e:
                    log.debug("tray notify failed: %s", e)

        _agent_thread = threading.Thread(
            target=run_agent,
            args=(token, backend),
            kwargs={"on_status": _status, "stop_event": _stop, "on_notify": _notify},
            daemon=True,
        )
        _agent_thread.start()


    import time
    initial_url = (f"{backend}/my-activity?_dt=1&_r={int(time.time())}"
                   if is_authenticated() else f"{backend}/")
    window = webview.create_window(
        title="Developer Activity",
        url=initial_url,
        width=1280,
        height=800,
        min_size=(800, 600),
        text_select=True,
        hidden=True,
    )

    def on_closing():
        """Hide to tray instead of closing."""
        window.hide()
        return False

    window.events.closing += on_closing


    def _show(icon=None, item=None):
        window.show()

    def _quit(icon=None, item=None):
        _stop.set()
        if _icon:
            _icon.stop()
        window.destroy()

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

    if ipc_server:
        ipc_server(lambda: window.show())


    def on_start():
        """Runs in a worker thread after webview initialises."""
        if is_authenticated():
            token = load_token()
            if token:
                start_agent(token)
                if not startup_mode:
                    window.show()
            else:
                _do_sso()
        else:
            if startup_mode:
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

    webview.start(on_start, debug=False)

    _stop.set()
