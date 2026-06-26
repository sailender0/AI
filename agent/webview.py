"""
Pywebview window for the Developer Activity desktop app.
Opens /my-activity as the default page with da_desktop cookie support.
Falls back to opening in the default browser if WebView2 is unavailable.
"""
import logging
import webbrowser

log = logging.getLogger(__name__)

_WINDOW_TITLE  = "Developer Activity"
_WINDOW_WIDTH  = 1280
_WINDOW_HEIGHT = 800


def _has_webview2() -> bool:
    """Check if Edge WebView2 runtime is available (Windows only)."""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
        )
        winreg.CloseKey(key)
        return True
    except Exception:
        return False


def open_window(backend: str):
    """
    Open the My Activity page. Uses pywebview if available, else browser.
    The ?_dt=1 param triggers the server to set the da_desktop cookie on first load.
    """
    url = f"{backend}/my-activity?_dt=1"

    try:
        import webview
    except ImportError:
        log.warning("pywebview not installed — opening in browser")
        webbrowser.open(url)
        return

    try:
        import platform
        if platform.system() == "Windows" and not _has_webview2():
            log.warning("WebView2 not found — opening in browser")
            webbrowser.open(url)
            return

        window = webview.create_window(
            title=_WINDOW_TITLE,
            url=url,
            width=_WINDOW_WIDTH,
            height=_WINDOW_HEIGHT,
            min_size=(800, 600),
            text_select=True,
        )
        # Inject desktop header on every request via JS after page load
        # pywebview doesn't support custom request headers directly,
        # but the da_desktop cookie set by ?_dt=1 on first load persists.
        webview.start(debug=False)

    except Exception as e:
        log.error("webview failed: %s — falling back to browser", e)
        webbrowser.open(url)
