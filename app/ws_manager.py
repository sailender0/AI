from __future__ import annotations
import logging
from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self):
        self._connections: dict[str, list[WebSocket]] = {}

    async def connect(self, ws: WebSocket, profile_id: str):
        await ws.accept()
        self._connections.setdefault(profile_id, []).append(ws)

    def disconnect(self, ws: WebSocket, profile_id: str):
        conns = self._connections.get(profile_id, [])
        if ws in conns:
            conns.remove(ws)

    async def notify(self, profile_id: str, payload: dict):
        dead = []
        for ws in list(self._connections.get(profile_id, [])):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws, profile_id)


manager = ConnectionManager()
