"""Pins _tool_name_for_dir — the pure home-dir → tool-name mapping behind
auto-discovery (known dir → clean name, unknown AI-named dir → raw, else None)."""
from agent.agent import _tool_name_for_dir


def test_known_dirs_get_clean_names():
    assert _tool_name_for_dir(".claude") == "claude-code"
    assert _tool_name_for_dir(".codex") == "codex"
    assert _tool_name_for_dir(".cursor") == "cursor-ai"


def test_unknown_ai_named_dir_surfaces_raw():
    assert _tool_name_for_dir(".mygptcli") == "mygptcli"   # matches "gpt"
    assert _tool_name_for_dir(".copilot-cache") == "copilot-cache"


def test_non_ai_dirs_ignored():
    for d in (".ssh", ".aws", ".vscode", ".docker", ".npm", "notdot"):
        assert _tool_name_for_dir(d) is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
