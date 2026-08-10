"""Behavioural check for dateRange() in app/static/app.js.

The two native <input type="date"> that replaced the hand-rolled two-month grid
carry real logic: the clamping that stops end preceding start, and the guard that
withholds onChange until BOTH ends are set. attendance.js refetches from the
server inside that callback, so a half-set range firing early means a request for
a nonsense window — the exact thing the old grid avoided by only reporting
complete ranges.

app.js runs side effects at import (initBase(), document listeners), so the two
functions under test are sliced out by brace matching rather than loading the
whole file. That still exercises the committed source, not a copy of it. node is
already required by tests/test_frontend_globals.py and by CI.
"""
import json
import subprocess
import tempfile
from pathlib import Path

APP_JS = Path("app/static/app.js")


def _slice_block(src: str, decl: str) -> str:
    """A `function name(...) { ... }` declaration, to its balanced closing brace.

    Only safe for bodies without template literals — `${` would be counted as an
    opening brace. dateRange has none; isoOf is full of them, hence _slice_line.
    """
    start = src.index(decl)
    assert "`" not in src[start:src.index("\n}", start)], f"{decl} grew a template literal"
    # Start at the BODY brace, not the first one: a destructured parameter list
    # like `function f({ a, b })` would otherwise open and close the count before
    # the body is reached.
    depth, i = 0, src.index("{", src.index(") {", start))
    while True:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
        i += 1


def _slice_line(src: str, decl: str) -> str:
    """A one-line `const name = ...;` declaration."""
    start = src.index(decl)
    return src[start:src.index("\n", start)]


HARNESS = """
function el() {
  return { value: '', min: '', max: '', _h: [],
           addEventListener(_, f) { this._h.push(f); },
           fire() { this._h.forEach(f => f()); } };
}
const a = el(), b = el();
global.document = { getElementById: id => (id === 'from' ? a : id === 'to' ? b : null) };

__SRC__

const TODAY = isoOf(new Date());
const results = [];
let fired = [];
const api = dateRange({ from: 'from', to: 'to', onChange: (s, e) => fired.push([s, e]) });

results.push(['max is today on both', a.max === TODAY && b.max === TODAY]);

a.value = '2026-07-01'; a.fire();
results.push(['start alone does not fire', fired.length === 0]);
results.push(['end cannot precede start', b.min === '2026-07-01']);

b.value = '2026-07-10'; b.fire();
results.push(['both set fires once', fired.length === 1]);
results.push(['fires with the pair', JSON.stringify(fired[0]) === '["2026-07-01","2026-07-10"]']);
results.push(['start cannot follow end', a.max === '2026-07-10']);

fired = [];
api.set('2026-06-01', '2026-06-05');
results.push(['set() writes both', a.value === '2026-06-01' && b.value === '2026-06-05']);
results.push(['set() re-clamps', b.min === '2026-06-01' && a.max === '2026-06-05']);
results.push(['set() does not fire', fired.length === 0]);

api.set('2026-06-01', null);
results.push(['cleared end re-opens max', a.max === TODAY && b.value === '']);

console.log(JSON.stringify(results));
"""


def test_date_range_clamps_and_withholds_until_complete(node):
    src = APP_JS.read_text(encoding="utf-8")
    slices = "\n".join([
        _slice_line(src, "const isoOf   ="),
        _slice_block(src, "function dateRange("),
    ])
    harness = HARNESS.replace("__SRC__", slices)

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(harness)
        path = f.name
    try:
        r = subprocess.run([node, path], capture_output=True, text=True)
        assert r.returncode == 0, f"harness failed:\n{r.stderr[:2000]}"
        checks = json.loads(r.stdout.strip().splitlines()[-1])
    finally:
        Path(path).unlink(missing_ok=True)

    failed = [name for name, ok in checks if not ok]
    assert not failed, "dateRange() broke: " + ", ".join(failed)
    assert len(checks) == 10, f"expected 10 assertions, ran {len(checks)}"
