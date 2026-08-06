"""Guards the frontend contract introduced when the shared app.js was extracted.

app.js, each template's inline <script>, and each template's own
/static/pages/<page>.js are classic scripts sharing ONE global scope. A
`const`/`let`/`function` in a page script that collides with a name in app.js is
a SyntaxError that silently kills the *entire* page script — the page still
renders but nothing initialises. That's how the analytics page broke (a
duplicate `fmtTime`), and it is invisible to pytest and to the naked eye.

node --check on the concatenation reproduces exactly what the browser does.

The page scripts used to live inline in the templates. They don't any more, so
this file resolves `src="/static/..."` and checks the referenced file — a
src-only guard that skipped those templates would be a guard that isn't
guarding, which is the failure mode it exists to prevent.
"""
import re
import subprocess
import tempfile
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

TEMPLATES = Path("app/templates")
STATIC = Path("app/static")
APP_JS = STATIC / "app.js"

SCRIPT_RE = re.compile(r"<script([^>]*)>(.*?)</script>", re.S)
SRC_RE = re.compile(r'\bsrc="([^"]+)"')


def local_srcs(html: str) -> list[Path]:
    """The /static/ files a template pulls in, in document order (CDN URLs skipped)."""
    out = []
    for attrs, _ in SCRIPT_RE.findall(html):
        m = SRC_RE.search(attrs)
        if m and m.group(1).startswith("/static/"):
            out.append(STATIC / m.group(1)[len("/static/"):])
    return out


def _page_js(html: str) -> str:
    """A template's JS in load order — inline bodies and the contents of the
    /static/ files it references — with Jinja tags neutralised so it parses."""
    parts = []
    for attrs, body in SCRIPT_RE.findall(html):
        m = SRC_RE.search(attrs)
        if not m:
            parts.append(body)
        elif m.group(1).startswith("/static/"):
            path = STATIC / m.group(1)[len("/static/"):]
            if path != APP_JS and path.exists():
                parts.append(path.read_text(encoding="utf-8"))
    js = "\n;\n".join(parts)
    js = re.sub(r"\{\{.*?\}\}", "0", js, flags=re.S)
    js = re.sub(r"\{%.*?%\}", "", js, flags=re.S)
    return js


def _node_check(node: str, source: str) -> str | None:
    """Return the syntax error, or None if the source parses."""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(source)
        path = f.name
    try:
        r = subprocess.run([node, "--check", path], capture_output=True, text=True)
        if r.returncode == 0:
            return None
        return next((l for l in r.stderr.splitlines() if "Error" in l), r.stderr[:200]).strip()
    finally:
        Path(path).unlink(missing_ok=True)


def test_all_templates_compile():
    env = Environment(loader=FileSystemLoader(TEMPLATES))
    for tpl in sorted(TEMPLATES.glob("*.html")):
        env.get_template(tpl.name)


def test_app_js_is_valid(node):
    assert _node_check(node, APP_JS.read_text(encoding="utf-8")) is None


@pytest.mark.parametrize("tpl", sorted(TEMPLATES.glob("*.html")), ids=lambda p: p.name)
def test_template_does_not_clash_with_app_js(node, tpl: Path):
    js = _page_js(tpl.read_text(encoding="utf-8"))
    if not js.strip():
        pytest.skip("no page script")
    err = _node_check(node, APP_JS.read_text(encoding="utf-8") + "\n;\n" + js)
    assert err is None, f"{tpl.name} clashes with app.js: {err}"


@pytest.mark.parametrize("tpl", sorted(TEMPLATES.glob("*.html")), ids=lambda p: p.name)
def test_script_srcs_resolve(tpl: Path):
    """A typo'd /static/ path 404s and kills the whole page, silently — the new
    failure mode created by moving the page scripts out of the templates."""
    for path in local_srcs(tpl.read_text(encoding="utf-8")):
        assert path.is_file(), f"{tpl.name} references missing script {path}"
