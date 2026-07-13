"""Guards the Tailwind build.

app/static/app.css is GENERATED from app/static/src/app.css by `npm run css`.
Because the generated file is committed (Docker just COPY/bind-mounts it), the
failure mode is silent: add a Tailwind class to a template, forget to rebuild,
and the class is simply missing in production — no error, just unstyled markup.

This rebuilds into a temp file and compares. If it fails, run `npm run css`.
"""
import subprocess
import tempfile
from pathlib import Path

SRC = Path("app/static/src/app.css")
OUT = Path("app/static/app.css")


def test_committed_css_is_up_to_date(tailwind):
    # Invoke the CLI through node, not `npx`: on Windows npx is a .cmd, which
    # subprocess cannot exec without a shell.
    node, cli = tailwind
    with tempfile.TemporaryDirectory() as tmp:
        rebuilt = Path(tmp) / "app.css"
        r = subprocess.run(
            [node, str(cli), "-i", str(SRC), "-o", str(rebuilt), "--minify"],
            capture_output=True, text=True,
        )
        assert r.returncode == 0, f"tailwind build failed:\n{r.stderr}"

        # Compare on content, not bytes: core.autocrlf hands this file back as
        # CRLF on Windows and LF on Linux CI, so a raw byte compare would fail
        # on one platform for no real reason.
        def norm(p: Path) -> str:
            return p.read_text(encoding="utf-8").replace("\r\n", "\n").strip()

        assert norm(rebuilt) == norm(OUT), (
            "app/static/app.css is stale — a template or the CSS source changed "
            "without rebuilding. Run: npm run css"
        )


def test_shim_is_gone():
    """The !important colour shim must not come back — tailwind.config.js owns
    the colour mapping now. Its return would mean the config was bypassed."""
    css = OUT.read_text(encoding="utf-8")
    # The one legitimate !important is the collapsed-sidebar label.
    assert css.count("!important") <= 1, "an !important colour shim has reappeared"


def test_classes_built_from_app_js_survive_purge():
    """These classes exist ONLY inside app.js (sidebar connector markup). If
    app.js ever drops out of tailwind.config.js `content`, they get purged and
    the connector list silently loses its styling."""
    css = OUT.read_text(encoding="utf-8")
    for cls in (".bg-green-400", ".bg-gray-600", ".bg-indigo-600", ".text-gray-400"):
        assert cls in css, f"{cls} was purged — is app/static/app.js still in the content glob?"
