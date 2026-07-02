"""Entry point: python -m agent  (or the compiled .exe)

Flags:
  --startup   Launched by Windows on boot — start agent silently, no window.
"""
import os
import socket
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.config import BACKEND_URL

_IPC_PORT = 47823  # fixed local port for single-instance signalling


def _signal_existing() -> bool:
    """Send 'show' to an already-running instance. Returns True if one exists."""
    try:
        with socket.create_connection(("localhost", _IPC_PORT), timeout=1) as s:
            s.sendall(b"show")
        return True
    except OSError:
        return False


def _start_ipc_server(on_show):
    """Listen for show signals from subsequent launches (daemon thread)."""
    def _serve():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                srv.bind(("localhost", _IPC_PORT))
            except OSError:
                return  # another instance already owns this port
            srv.listen(5)
            while True:
                try:
                    conn, _ = srv.accept()
                    with conn:
                        if conn.recv(16) == b"show":
                            on_show()
                except Exception:
                    pass
    threading.Thread(target=_serve, daemon=True).start()


if __name__ == "__main__":
    # Second instance — tell the first one to show its window
    if _signal_existing():
        sys.exit(0)

    startup_mode = "--startup" in sys.argv

    from agent.tray import main
    main(BACKEND_URL, startup_mode=startup_mode, ipc_server=_start_ipc_server)
