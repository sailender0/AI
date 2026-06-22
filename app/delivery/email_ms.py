"""
Email delivery via SMTP using the profile email from SSO identity.
No Teams/Graph API dependency.
"""
from __future__ import annotations

import base64
import logging
import smtplib
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.config import settings

logger = logging.getLogger(__name__)


def _html_body(period_label: str, summary_text: str, source_counts: dict, total: int) -> str:
    rows = "".join(
        f"<tr><td style='padding:4px 12px;color:#6b7280'>{k.title()}</td>"
        f"<td style='padding:4px 12px;font-weight:600'>{v}</td></tr>"
        for k, v in sorted(source_counts.items()) if v > 0
    )
    body_html = (summary_text or "<em>No summary generated.</em>").replace("\n", "<br>")
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="font-family:Segoe UI,Arial,sans-serif;background:#f3f4f6;margin:0;padding:24px">
  <div style="max-width:600px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.1)">
    <div style="background:#4f46e5;padding:20px 24px">
      <h1 style="color:#fff;margin:0;font-size:18px">Developer Activity Tracker</h1>
      <p style="color:#c7d2fe;margin:4px 0 0;font-size:13px">{period_label}</p>
    </div>
    <div style="padding:24px">
      <h2 style="color:#1f2937;font-size:15px;margin-top:0">Activity Summary</h2>
      <p style="color:#374151;line-height:1.6;font-size:14px">{body_html}</p>
      <h3 style="color:#1f2937;font-size:13px;margin-bottom:8px">Events by Tool</h3>
      <table style="border-collapse:collapse;font-size:13px">
        {rows}
        <tr style="border-top:1px solid #e5e7eb">
          <td style="padding:6px 12px;color:#374151;font-weight:600">Total</td>
          <td style="padding:6px 12px;font-weight:700;color:#4f46e5">{total}</td>
        </tr>
      </table>
      <p style="margin-top:24px;font-size:12px;color:#9ca3af">PDF report is attached.</p>
    </div>
  </div>
</body></html>"""


async def send_activity_email(
    profile_id: str,
    to_email: str,
    subject: str,
    period_label: str,
    summary_text: str,
    events: list,
    pdf_bytes: bytes,
) -> bool:
    if not settings.SMTP_HOST or not settings.SMTP_USER or not settings.SMTP_PASS:
        logger.warning("SMTP not configured — email not sent (profile=%s)", profile_id)
        return False

    source_counts: dict = {}
    for e in events:
        src = e.get("source", "other")
        source_counts[src] = source_counts.get(src, 0) + 1

    msg = MIMEMultipart()
    msg["From"]    = settings.SMTP_USER
    msg["To"]      = to_email
    msg["Subject"] = subject

    html = _html_body(period_label, summary_text, source_counts, len(events))
    msg.attach(MIMEText(html, "html", "utf-8"))

    attachment = MIMEApplication(pdf_bytes, _subtype="pdf")
    attachment.add_header("Content-Disposition", "attachment", filename="activity-report.pdf")
    msg.attach(attachment)

    try:
        if settings.SMTP_TLS:
            with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as srv:
                srv.starttls()
                srv.login(settings.SMTP_USER, settings.SMTP_PASS)
                srv.send_message(msg)
        else:
            with smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT) as srv:
                srv.login(settings.SMTP_USER, settings.SMTP_PASS)
                srv.send_message(msg)
        logger.info("Activity email sent to %s (profile=%s)", to_email, profile_id)
        return True
    except Exception as exc:
        logger.error("SMTP send failed for %s: %s", profile_id, exc)
        return False


async def deliver_summary_email(profile, period_type: str, summary_text: str, events: list, period_label: str):
    from app.services.export_pdf import generate_daily_pdf, generate_weekly_pdf

    profile_id = str(profile.id)
    to_email   = profile.email
    if not to_email:
        return

    if period_type == "daily":
        pdf_bytes = generate_daily_pdf(period_label, summary_text, events)
        subject   = f"Your Daily Activity - {period_label}"
    else:
        day_map: dict = {}
        for e in events:
            ts = e.get("occurred_at")
            day_str = f"{ts.strftime('%A, %b')} {ts.day}" if isinstance(ts, datetime) else "Unknown"
            day_map.setdefault(day_str, []).append(e)
        total_counts: dict = {}
        for e in events:
            src = e.get("source", "other")
            total_counts[src] = total_counts.get(src, 0) + 1
        pdf_bytes = generate_weekly_pdf(period_label, summary_text, list(day_map.items()), total_counts)
        subject   = f"Your Weekly Activity - {period_label}"

    await send_activity_email(
        profile_id=profile_id,
        to_email=to_email,
        subject=subject,
        period_label=period_label,
        summary_text=summary_text,
        events=events,
        pdf_bytes=pdf_bytes,
    )
