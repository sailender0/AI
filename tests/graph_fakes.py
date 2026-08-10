"""Fake Graph HTTP client for the connector tests.

The same MagicMock-based response/client pair was copy-pasted into
test_teams_chat, test_outlook_connectors and test_teams_calls — two of them
byte-identical. A plain module rather than a conftest fixture because these are
called with per-test arguments inline, which a fixture would only wrap.
"""
from unittest.mock import AsyncMock, MagicMock


def graph_resp(payload, status=200):
    """One response: .status_code plus a .json() returning `payload`."""
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    return r


def graph_client(*payloads, status=200):
    """A client whose successive .get() calls return `payloads` in order.

    walk() follows @odata.nextLink, so a test that expects paging passes one
    payload per expected request.
    """
    c = MagicMock()
    c.get = AsyncMock(side_effect=[graph_resp(p, status) for p in payloads])
    return c
