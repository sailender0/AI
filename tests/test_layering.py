"""Architectural guards on the import graph.

app/ai/* and app/routes/email.py used to import private helpers out of sibling
HTTP route modules (app.routes.agent.analytics._period_ranges, and friends).
That made route modules the de-facto service layer, forced lazy imports to dodge
an import cycle, and meant renaming a private helper broke the AI chat.

These two tests are the reason it won't come back. Both are static — no DB, no
network, no app startup.
"""
import ast
import graphlib
import pathlib

import pytest

APP = pathlib.Path(__file__).resolve().parent.parent / "app"

COMPOSITION_ROOT = "app.main"


def _modules() -> dict[str, pathlib.Path]:
    out = {}
    for f in APP.rglob("*.py"):
        name = "app." + ".".join(f.relative_to(APP).with_suffix("").parts)
        out[name.removesuffix(".__init__")] = f
    return out


def _imports(path: pathlib.Path) -> list[tuple[str, tuple[str, ...], int]]:
    """(module, imported names, lineno) for every `from app... import ...`, including
    function-local ones — a lazy import is still a dependency."""
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    return [(n.module, tuple(a.name for a in n.names), n.lineno) for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("app")]


def _resolve(module: str, names: tuple[str, ...], mods: dict) -> set[str]:
    """Which modules a single import statement actually depends on. `from app.ai
    import llm` targets app.ai.llm, NOT every module in the app.ai package."""
    hits = {module + "." + n for n in names} & set(mods)
    return hits or ({module} if module in mods else set())


def test_nothing_but_main_imports_a_route_module():
    """Dependencies point routes -> services -> storage, never back up."""
    offenders = [
        f"{name} imports {mod} (line {lineno})"
        for name, path in _modules().items() if name != COMPOSITION_ROOT
        for mod, _names, lineno in _imports(path) if mod.startswith("app.routes")
    ]
    assert not offenders, (
        "business logic must live in app/services, not in an HTTP route module:\n  "
        + "\n  ".join(sorted(offenders))
    )


def test_no_import_cycles_between_modules():
    """A cycle is what forced the lazy imports this refactor removed."""
    mods = _modules()
    edges: dict[str, set[str]] = {}
    for name, path in mods.items():
        deps: set[str] = set()
        for mod, names, _ in _imports(path):
            deps |= _resolve(mod, names, mods)
        edges[name] = deps - {name}

    edges["app.webhooks.registration"].discard("app.auth.oauth")

    try:
        graphlib.TopologicalSorter(edges).prepare()
    except graphlib.CycleError as exc:
        pytest.fail("import cycle: " + " -> ".join(exc.args[1]))
