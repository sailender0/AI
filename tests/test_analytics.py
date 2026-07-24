"""Pins aggregate_claude — the claude_usage roll-up used by /today and /week."""
from app.services.device_analytics import aggregate_claude


def test_merges_same_repo_and_model():
    docs = [
        {"repo": "a", "model": "sonnet", "input_tokens": 10, "output_tokens": 5, "message_count": 1, "files": ["x"]},
        {"repo": "a", "model": "sonnet", "input_tokens": 20, "output_tokens": 5, "message_count": 2, "files": ["y"]},
    ]
    summary, total = aggregate_claude(docs)
    assert len(summary) == 1
    r = summary[0]
    assert r["repo"] == "a"
    assert (r["input_tokens"], r["output_tokens"]) == (30, 10)
    assert len(r["models"]) == 1 and r["models"][0]["messages"] == 3
    assert r["files"] == ["x", "y"]
    assert total == 40


def test_splits_repos_and_models():
    docs = [
        {"repo": "a", "model": "sonnet", "input_tokens": 1, "output_tokens": 1, "message_count": 1, "files": []},
        {"repo": "a", "model": "opus",   "input_tokens": 2, "output_tokens": 2, "message_count": 1, "files": []},
        {"repo": "b", "model": "sonnet", "input_tokens": 3, "output_tokens": 3, "message_count": 1, "files": []},
    ]
    summary, total = aggregate_claude(docs)
    assert {r["repo"] for r in summary} == {"a", "b"}
    a = next(r for r in summary if r["repo"] == "a")
    assert len(a["models"]) == 2
    assert total == 12


def test_empty():
    assert aggregate_claude([]) == ([], 0)


if __name__ == "__main__":
    for _n, _f in list(globals().items()):
        if _n.startswith("test_"):
            _f()
    print("ok")
