from app.services.email_report import _md_html, render_chat


def test_md_html_escapes_and_formats():
    h = _md_html("<script>alert(1)</script>\n- one\n- two\n**bold** line")
    assert "<script>" not in h and "&lt;script&gt;" in h
    assert h.count("<li>") == 2
    assert "<b>bold</b>" in h


def test_render_chat_uses_report_shell():
    subject, html = render_chat("compare my activity", "Week 1: 10 commits")
    assert subject.startswith("AI answer:")
    assert "Developer Activity Tracker" in html
