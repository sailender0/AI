"""Guards the frontend contract introduced when the shared app.js was extracted.

app.js and each template's inline <script> are classic scripts sharing ONE global
scope. A `const`/`let`/`function` in a template that collides with a name in
app.js is a SyntaxError that silently kills the *entire* page script — the page
still renders but nothing initialises. That's how the analytics page broke
(a duplicate `fmtTime`), and it is invisible to pytest and to the naked eye.

node --check on the concatenation reproduces exactly what the browser does.
"""
import re
import subprocess
import tempfile
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

TEMPLATES = Path("app/templates")
APP_JS = Path("app/static/app.js")

SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)


def _inline_js(html: str) -> str:
    """Inline JS from a template, with Jinja tags neutralised so it parses."""
    js = "\n;\n".join(SCRIPT_RE.findall(html))
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
        env.get_template(tpl.name)  # raises TemplateSyntaxError on failure


def test_app_js_is_valid(node):
    assert _node_check(node, APP_JS.read_text(encoding="utf-8")) is None


@pytest.mark.parametrize("tpl", sorted(TEMPLATES.glob("*.html")), ids=lambda p: p.name)
def test_template_does_not_clash_with_app_js(node, tpl: Path):
    js = _inline_js(tpl.read_text(encoding="utf-8"))
    if not js.strip():
        pytest.skip("no inline script")
    # Same global scope the browser gives them.
    err = _node_check(node, APP_JS.read_text(encoding="utf-8") + "\n;\n" + js)
    assert err is None, f"{tpl.name} clashes with app.js: {err}"
