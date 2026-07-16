"""Entry point: python -m agent  (or the compiled .exe)

Flags:
  --startup   Launched by Windows on boot — start agent silently, no window.
"""
import os
import socket
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

_IPC_PORT = 47823  # fixed local port for single-instance signalling


def _signal_existing() -> None:
    """Tell the already-running instance to show its window."""
    try:
        with socket.create_connection(("localhost", _IPC_PORT), timeout=1) as s:
            s.sendall(b"show")
    except OSError:
        pass


def _bind_instance_lock() -> socket.socket | None:
    """Bind the IPC port — the bind IS the single-instance lock.
    Returns the bound socket, or None if another instance already owns it.
    No SO_REUSEADDR: on Windows it lets two sockets bind the same port,
    which is exactly the hole that allowed duplicate instances."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: block bind hijacking
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        srv.bind(("localhost", _IPC_PORT))
        srv.listen(5)
        return srv
    except OSError:
        srv.close()
        return None


def _start_ipc_server(srv: socket.socket, on_show) -> None:
    """Serve show signals from subsequent launches on the already-bound socket."""
    def _serve():
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
    # Acquire the lock BEFORE the slow imports below — the old code checked for a
    # peer here but only bound the port seconds later, so two launches in that
    # window both survived.
    _lock = _bind_instance_lock()
    if _lock is None:
        _signal_existing()
        sys.exit(0)

    startup_mode = "--startup" in sys.argv

    from agent.config import BACKEND_URL
    from agent.tray import main
    main(BACKEND_URL, startup_mode=startup_mode,
         ipc_server=lambda on_show: _start_ipc_server(_lock, on_show))
