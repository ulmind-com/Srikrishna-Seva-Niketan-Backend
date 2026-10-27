"""SMTP email service using aiosmtplib + Jinja2 templates.

Designed to be scheduled via FastAPI ``BackgroundTasks`` so the request path is
never blocked by SMTP latency. When SMTP is not configured the service logs the
message instead of raising, so local development works without credentials.
"""

from __future__ import annotations

from email.message import EmailMessage
from typing import Any, Dict, Optional

import aiosmtplib
from jinja2 import Environment, select_autoescape

from app.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_jinja = Environment(autoescape=select_autoescape(["html", "xml"]))

_BASE_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ subject }}</title>
</head>
<body style="margin:0;padding:0;background-color:#0f172a;font-family:'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;">
<!-- Outer wrapper -->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:linear-gradient(180deg,#0f172a 0%,#134e4a 50%,#0f172a 100%);min-height:100vh;">
<tr><td align="center" style="padding:40px 16px;">

<!-- Main card -->
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;width:100%;border-radius:24px;overflow:hidden;box-shadow:0 25px 60px rgba(0,0,0,0.5),0 0 80px rgba(20,184,166,0.15);">

  <!-- Gold accent strip -->
  <tr><td style="height:4px;background:linear-gradient(90deg,#d4a853,#f5d98a,#d4a853);"></td></tr>

  <!-- Header -->
  <tr>
    <td style="background:linear-gradient(135deg,#1e293b 0%,#0f172a 100%);padding:32px 40px 24px;text-align:center;border-bottom:1px solid rgba(148,163,184,0.1);">
      <!-- Logo Circle -->
      <div style="display:inline-block;width:56px;height:56px;border-radius:50%;background:linear-gradient(135deg,#0d9488,#14b8a6);text-align:center;line-height:56px;font-size:24px;font-weight:700;color:#ffffff;margin-bottom:16px;box-shadow:0 4px 20px rgba(20,184,166,0.4);">S</div>
      <h1 style="margin:0;font-size:22px;font-weight:700;color:#f1f5f9;letter-spacing:0.5px;">{{ brand }}</h1>
      <p style="margin:6px 0 0;font-size:12px;color:#94a3b8;text-transform:uppercase;letter-spacing:2px;">Healthcare &amp; Nursing Services</p>
    </td>
  </tr>

  <!-- Body content -->
  <tr>
    <td style="background:linear-gradient(180deg,#1e293b 0%,#1a2332 100%);padding:36px 40px;">
      {{ body }}
    </td>
  </tr>

  <!-- Footer -->
  <tr>
    <td style="background:#0f172a;padding:24px 40px;text-align:center;border-top:1px solid rgba(148,163,184,0.08);">
      <p style="margin:0 0 8px;font-size:12px;color:#64748b;">&copy; 2025 {{ brand }}. All rights reserved.</p>
      <p style="margin:0;font-size:11px;color:#475569;">This is an automated message. Please do not reply directly to this email.</p>
    </td>
  </tr>

  <!-- Bottom accent strip -->
  <tr><td style="height:3px;background:linear-gradient(90deg,#0d9488,#14b8a6,#0d9488);"></td></tr>

</table>
<!-- /Main card -->

</td></tr>
</table>
<!-- /Outer wrapper -->
</body>
</html>
"""


class EmailService:
    """Send transactional emails over SMTP."""

    def __init__(self) -> None:
        self.enabled = settings.email_enabled

    def _render(self, body_html: str, subject: str = "") -> str:
        template = _jinja.from_string(_BASE_TEMPLATE)
        return template.render(brand=settings.SMTP_FROM_NAME, body=body_html, subject=subject)

    async def send(
        self,
        to: str,
        subject: str,
        body_html: str,
        text_fallback: Optional[str] = None,
    ) -> bool:
        """Send a single HTML email. Returns ``True`` on success."""
        if not self.enabled:
            logger.info(
                "Email disabled; skipping send",
                extra={"to": to, "subject": subject},
            )
            return False

        rendered_html = self._render(body_html, subject=subject)

        # 1. Primary: Send via Resend REST API if configured
        if settings.RESEND_API_KEY:
            try:
                import httpx

                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post(
                        "https://api.resend.com/emails",
                        headers={
                            "Authorization": f"Bearer {settings.RESEND_API_KEY}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "from": f"{settings.SMTP_FROM_NAME} <{settings.RESEND_FROM_EMAIL}>",
                            "to": [to],
                            "subject": subject,
                            "html": rendered_html,
                            "text": text_fallback or "Please view this email in HTML.",
                        },
                    )
                    if resp.status_code in {200, 201, 202}:
                        logger.info(
                            "Email sent via Resend API",
                            extra={"to": to, "subject": subject, "resend_id": resp.json().get("id")},
                        )
                        return True
                    else:
                        logger.error(
                            "Resend API returned error status",
                            extra={"status": resp.status_code, "body": resp.text},
                        )
            except Exception as exc:
                logger.error(
                    "Resend API send failed",
                    extra={"to": to, "subject": subject, "error": str(exc)},
                )

        # 2. Fallback to SMTP if configured
        if settings.SMTP_HOST and settings.SMTP_USER:
            message = EmailMessage()
            message["From"] = f"{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM_EMAIL or settings.RESEND_FROM_EMAIL}>"
            message["To"] = to
            message["Subject"] = subject
            message.set_content(text_fallback or "Please view this email in HTML.")
            message.add_alternative(rendered_html, subtype="html")

            try:
                await aiosmtplib.send(
                    message,
                    hostname=settings.SMTP_HOST,
                    port=settings.SMTP_PORT,
                    username=settings.SMTP_USER,
                    password=settings.SMTP_PASSWORD,
                    start_tls=settings.SMTP_TLS,
                    use_tls=settings.SMTP_SSL,
                    timeout=30,
                )
                logger.info("Email sent via SMTP", extra={"to": to, "subject": subject})
                return True
            except Exception as exc:  # noqa: BLE001 - never break the request path
                logger.error(
                    "Email send failed via SMTP",
                    extra={"to": to, "subject": subject, "error": str(exc)},
                )
                return False

        logger.info(
            "Email skipped; no active provider configured",
            extra={"to": to, "subject": subject},
        )
        return False

    async def send_staff_otp(self, to: str, otp: str) -> bool:
        """Send an email verification 6-digit OTP to registering staff."""
        # Build individual digit boxes for the OTP
        digits = list(otp)
        digit_boxes = ""
        for d in digits:
            digit_boxes += (
                f'<td style="width:48px;height:58px;background:linear-gradient(180deg,#134e4a,#0f3d3a);'
                f'border:2px solid #14b8a6;border-radius:12px;text-align:center;vertical-align:middle;'
                f'font-size:28px;font-weight:700;color:#5eead4;font-family:\'Courier New\',monospace;'
                f'letter-spacing:2px;box-shadow:0 0 16px rgba(20,184,166,0.25),inset 0 1px 0 rgba(255,255,255,0.05);">'
                f'{d}</td><td style="width:8px;"></td>'
            )

        body = f"""
<!-- Greeting -->
<p style="margin:0 0 6px;font-size:15px;color:#94a3b8;">Hello,</p>
<p style="margin:0 0 24px;font-size:15px;color:#cbd5e1;line-height:1.6;">
  You've requested to verify your email for <strong style="color:#5eead4;">Staff Registration</strong>. Use the code below to complete your verification:
</p>

<!-- OTP Container -->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
<tr><td align="center" style="padding:8px 0 32px;">
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="background:linear-gradient(135deg,rgba(15,23,42,0.8),rgba(30,41,59,0.9));border:1px solid rgba(20,184,166,0.3);border-radius:20px;padding:28px 32px;box-shadow:0 8px 40px rgba(0,0,0,0.3),0 0 60px rgba(20,184,166,0.08);">
  <tr><td align="center">
    <p style="margin:0 0 4px;font-size:11px;text-transform:uppercase;letter-spacing:3px;color:#64748b;font-weight:600;">Verification Code</p>
  </td></tr>
  <tr><td align="center" style="padding:16px 0 12px;">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0">
    <tr>
      {digit_boxes}
    </tr>
    </table>
  </td></tr>
  <tr><td align="center">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0">
    <tr>
      <td style="width:16px;height:16px;vertical-align:middle;">
        <div style="width:8px;height:8px;background:#f59e0b;border-radius:50%;box-shadow:0 0 6px rgba(245,158,11,0.5);"></div>
      </td>
      <td style="vertical-align:middle;padding-left:6px;">
        <span style="font-size:12px;color:#fbbf24;font-weight:600;">Expires in 10 minutes</span>
      </td>
    </tr>
    </table>
  </td></tr>
  </table>
</td></tr>
</table>

<!-- Security Notice -->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:24px;">
<tr>
  <td style="background:rgba(15,23,42,0.6);border:1px solid rgba(148,163,184,0.1);border-radius:12px;padding:16px 20px;">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0">
    <tr>
      <td style="width:36px;vertical-align:top;padding-top:2px;">
        <div style="width:32px;height:32px;background:linear-gradient(135deg,#0d9488,#14b8a6);border-radius:8px;text-align:center;line-height:32px;font-size:16px;">&#128274;</div>
      </td>
      <td style="padding-left:12px;">
        <p style="margin:0 0 4px;font-size:13px;font-weight:600;color:#e2e8f0;">Security Notice</p>
        <p style="margin:0;font-size:12px;color:#94a3b8;line-height:1.5;">Never share this code with anyone. Our team will never ask for your verification code. If you didn't request this, please ignore this email.</p>
      </td>
    </tr>
    </table>
  </td>
</tr>
</table>

<!-- Divider -->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:20px;">
<tr><td style="height:1px;background:linear-gradient(90deg,transparent,rgba(148,163,184,0.2),transparent);"></td></tr>
</table>

<!-- Help text -->
<p style="margin:0;font-size:12px;color:#64748b;text-align:center;line-height:1.5;">
  Having trouble? Contact us at <a href="mailto:support@srikrishnasevaniketan.in" style="color:#14b8a6;text-decoration:none;font-weight:600;">support@srikrishnasevaniketan.in</a>
</p>
"""
        return await self.send(
            to=to,
            subject=f"{otp} — Your Staff Verification Code | Srikrishna Seva Niketan",
            body_html=body,
            text_fallback=f"Your Srikrishna Seva Niketan staff verification code is: {otp}. This code expires in 10 minutes. Do not share this code with anyone.",
        )

    # ---- Convenience templated senders ----

    async def send_booking_confirmation(self, to: str, context: Dict[str, Any]) -> bool:
        body = (
            f"<p>Dear {context.get('name', 'Customer')},</p>"
            f"<p>Thank you for booking <b>{context.get('service')}</b> with us.</p>"
            f"<p>Your booking reference is <b>{context.get('reference')}</b>.</p>"
            f"<p>Preferred date: <b>{context.get('date')}</b></p>"
            "<p>Our team will contact you shortly to confirm the details.</p>"
        )
        return await self.send(to, "Your Booking is Received", body)

    async def send_contact_confirmation(self, to: str, context: Dict[str, Any]) -> bool:
        body = (
            f"<p>Dear {context.get('name', 'there')},</p>"
            "<p>We have received your message and will get back to you soon.</p>"
            f"<blockquote>{context.get('message', '')}</blockquote>"
        )
        return await self.send(to, "We Received Your Message", body)

    async def send_application_confirmation(self, to: str, context: Dict[str, Any]) -> bool:
        body = (
            f"<p>Dear {context.get('name', 'Applicant')},</p>"
            f"<p>Thank you for applying for <b>{context.get('job')}</b>.</p>"
            f"<p>Your application reference is <b>{context.get('reference')}</b>.</p>"
            "<p>Our HR team will review your application and reach out if shortlisted.</p>"
        )
        return await self.send(to, "Application Received", body)

    async def send_password_reset(self, to: str, reset_link: str) -> bool:
        body = (
            "<p>We received a request to reset your password.</p>"
            f'<p><a href="{reset_link}" '
            'style="background:#0f766e;color:#fff;padding:10px 18px;'
            'border-radius:6px;text-decoration:none">Reset Password</a></p>'
            f"<p>Or copy this link: {reset_link}</p>"
            "<p>If you did not request this, you can safely ignore this email.</p>"
        )
        return await self.send(to, "Password Reset Request", body)

    async def send_assignment_notice(
        self,
        to: str,
        staff_name: str,
        customer_name: str,
        shift: str,
        place: str,
        start_date: str,
        end_date: str,
    ) -> bool:
        shift_label = {
            "day": "Day",
            "night": "Night",
            "full_24h": "24 Hours",
            "custom": "Custom",
        }.get(shift, shift)
        body = (
            f"<p>Dear {staff_name},</p>"
            "<p>You have a new duty assignment.</p>"
            "<table style='border-collapse:collapse'>"
            f"<tr><td style='padding:4px 12px 4px 0'><b>Client</b></td><td>{customer_name}</td></tr>"
            f"<tr><td style='padding:4px 12px 4px 0'><b>Posted at</b></td><td>{place}</td></tr>"
            f"<tr><td style='padding:4px 12px 4px 0'><b>Shift</b></td><td>{shift_label}</td></tr>"
            f"<tr><td style='padding:4px 12px 4px 0'><b>From</b></td><td>{start_date}</td></tr>"
            f"<tr><td style='padding:4px 12px 4px 0'><b>To</b></td><td>{end_date}</td></tr>"
            "</table>"
            "<p>Please report on time. Contact the office for any queries.</p>"
        )
        return await self.send(to, "New Duty Assignment | Srikrishna Seva Niketan", body)

    async def send_admin_notification(self, subject: str, body_html: str) -> bool:
        if not settings.ADMIN_NOTIFICATION_EMAIL:
            return False
        return await self.send(settings.ADMIN_NOTIFICATION_EMAIL, subject, body_html)


# Module-level singleton.
email_service = EmailService()
