"""Repo-tracking filter: system/vendored dirs are never tracked, real repos are.
Guards the exclusion that stops the agent reporting a bogus 'system32' repo and
keeps auto-discovery from harvesting dependency/system checkouts.
"""
from pathlib import Path

from agent.agent import _is_trackable_repo


def test_excludes_system_and_vendor_dirs():
    assert not _is_trackable_repo(Path(r"C:\WINDOWS\system32"))
    assert not _is_trackable_repo(Path(r"C:\Program Files\Git"))
    assert not _is_trackable_repo(Path(r"C:\ProgramData\thing"))
    assert not _is_trackable_repo(Path(r"C:\Users\me\proj\node_modules\dep"))


def test_accepts_real_repos():
    assert _is_trackable_repo(Path(r"E:\AI"))
    assert _is_trackable_repo(Path(r"C:\Users\me\dev\myapp"))


if __name__ == "__main__":
    test_excludes_system_and_vendor_dirs()
    test_accepts_real_repos()
    print("ok")
