"""The frontend must not ask for more rows than an endpoint allows.

github/gitlab/jira all called /api/events/recent with limit=500 while the route
caps it at le=200. FastAPI answered 422 — and because the callers did
`(await res.json()).events || []`, the rejection rendered as an empty
"no activity" panel instead of an error. It went unnoticed indefinitely.

This reads the cap straight out of the route definition, so it stays correct if
the cap ever changes.
"""
import re
from pathlib import Path

import pytest

ROUTES = Path("app/routes")
FRONTEND = list(Path("app/templates").glob("*.html")) + [Path("app/static/app.js")]

ROUTE_RE = re.compile(
    r'@router\.(?:get|post)\(\s*["\'](?P<path>/api/[^"\']+)["\'].*?'
    r'limit:\s*int\s*=\s*Query\([^)]*\ble=(?P<cap>\d+)',
    re.S,
)


def _caps() -> dict[str, int]:
    caps = {}
    for py in ROUTES.rglob("*.py"):
        for m in ROUTE_RE.finditer(py.read_text(encoding="utf-8")):
            caps[m.group("path")] = int(m.group("cap"))
    return caps


def test_found_the_capped_endpoints():
    caps = _caps()
    assert caps, "no capped endpoints parsed — has the Query(le=...) style changed?"
    assert caps.get("/api/events/recent") == 200


@pytest.mark.parametrize("f", FRONTEND, ids=lambda p: p.name)
def test_frontend_respects_limit_caps(f: Path):
    caps = _caps()
    text = f.read_text(encoding="utf-8")
    for path, cap in caps.items():
        for m in re.finditer(re.escape(path) + r"[^`'\"\s]*?[?&]limit=(\d+)", text):
            requested = int(m.group(1))
            assert requested <= cap, (
                f"{f.name} requests limit={requested} from {path}, "
                f"but the endpoint caps it at {cap} → FastAPI returns 422"
            )
