"""SSRF guard on the Teams notification `resource`.

`resource` is attacker-controlled and interpolated into a Graph URL fetched with
the victim's delegated token. _safe_resource must drop anything that isn't a
plain message path — query strings, traversal, schemes, absolute paths — while
staying tolerant of Graph's casing/OData-key variations.
"""
from app.webhooks.receivers.teams import _safe_resource


def test_legit_message_resources_pass():
    assert _safe_resource("me/messages/AAMkAGI2")
    assert _safe_resource("Users/00000000-0000-0000-0000-000000000001/Messages/AAMk")
    assert _safe_resource("users('a@b.com')/messages('AAMk')")


def test_ssrf_shapes_rejected():
    assert not _safe_resource("me/messages?$top=999&$select=body")   # query exfil
    assert not _safe_resource("../users/victim/messages/1")          # traversal
    assert not _safe_resource("https://evil.tld/steal")              # absolute scheme
    assert not _safe_resource("/etc/passwd")                         # absolute path
    assert not _safe_resource("me/mailFolders/inbox")                # not a message path
    assert not _safe_resource("")                                    # empty
    assert not _safe_resource("me/messages/" + "A" * 300)            # oversized
