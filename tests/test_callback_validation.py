"""Open-redirect guard for the desktop SSO callback (security regression).

The device token is redirected to agent_callback, so only a real localhost URL
may pass — userinfo/bogus-port tricks must be rejected (P0 token-theft fix).
"""
from app.auth.sso import _is_local_callback


def test_real_agent_callback_allowed():
    # matches agent/auth.py: f"http://localhost:{port}/auth"
    assert _is_local_callback("http://localhost:53123/auth")
    assert _is_local_callback("http://127.0.0.1:9999/auth")


def test_spoofed_hosts_rejected():
    for bad in [
        "http://localhost:@evil.com/steal",   # userinfo trick (the bypass)
        "http://localhost@evil.com/steal",
        "http://localhost:8000.evil.com/",    # bogus non-numeric port
        "http://localhost.evil.com/",         # subdomain
        "https://evil.com/localhost",
        "//evil.com",
    ]:
        assert not _is_local_callback(bad), bad
