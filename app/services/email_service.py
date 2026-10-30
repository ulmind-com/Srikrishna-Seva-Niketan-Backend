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
<meta http-equiv="X-UA-Compatible" content="IE=edge">
<title>{{ subject }}</title>
<style>
  body, table, td, p, a, li, blockquote {
    -webkit-text-size-adjust: 100%;
    -ms-text-size-adjust: 100%;
  }
  @media only screen and (max-width: 620px) {
    .email-container { width: 100% !important; margin: auto !important; }
    .email-card { border-radius: 16px !important; }
    .email-content { padding: 24px 18px !important; }
    .email-header { padding: 26px 20px 20px !important; }
    .email-footer { padding: 20px 16px !important; }
    .detail-row { display: block !important; width: 100% !important; padding: 6px 0 !important; }
    .cta-button { display: block !important; width: 100% !important; text-align: center !important; }
  }
</style>
</head>
<body style="margin:0;padding:0;background-color:#070d18;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#e2e8f0;">

<!-- Outer Background Wrapper -->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#070d18;background:radial-gradient(ellipse at top,#0f2334 0%,#070d18 70%);min-height:100vh;">
<tr>
  <td align="center" style="padding:32px 12px 40px;">

    <!-- Main Card Container -->
    <table role="presentation" width="580" cellpadding="0" cellspacing="0" border="0" class="email-container email-card" style="max-width:580px;width:100%;border-radius:20px;overflow:hidden;background:#0d1526;border:1px solid rgba(56,189,248,0.18);box-shadow:0 20px 60px rgba(0,0,0,0.6),0 0 50px rgba(13,148,136,0.12);">

      <!-- Gradient Luxury Top Accent Bar -->
      <tr>
        <td style="height:4px;background:linear-gradient(90deg,#059669 0%,#0d9488 25%,#38bdf8 70%,#f59e0b 100%);"></td>
      </tr>

      <!-- Header -->
      <tr>
        <td class="email-header" style="background:linear-gradient(180deg,#111c33 0%,#0d1526 100%);padding:30px 36px 22px;text-align:center;border-bottom:1px solid rgba(255,255,255,0.06);">
          <!-- Emblem / Logo -->
          <div style="display:inline-block;width:52px;height:52px;border-radius:15px;background:linear-gradient(135deg,#0d9488 0%,#0284c7 100%);text-align:center;line-height:52px;font-size:24px;font-weight:700;color:#ffffff;box-shadow:0 8px 24px rgba(13,148,136,0.35);border:1px solid rgba(255,255,255,0.25);margin-bottom:12px;">
            &#10010;
          </div>
          <h1 style="margin:0;font-size:21px;font-weight:800;color:#f8fafc;letter-spacing:0.4px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
            {{ brand }}
          </h1>
          <p style="margin:5px 0 0;font-size:11px;color:#38bdf8;text-transform:uppercase;letter-spacing:2.5px;font-weight:700;">
            Healthcare &amp; Nursing Services
          </p>
        </td>
      </tr>

      <!-- Body Content -->
      <tr>
        <td class="email-content" style="background:#0d1526;padding:32px 36px;">
          {{ body | safe }}
        </td>
      </tr>

      <!-- Footer -->
      <tr>
        <td class="email-footer" style="background:linear-gradient(180deg,#0a101d 0%,#070d18 100%);padding:24px 36px;text-align:center;border-top:1px solid rgba(255,255,255,0.06);">
          <div style="margin-bottom:12px;">
            <span style="display:inline-block;width:6px;height:6px;background:#10b981;border-radius:50%;margin-right:6px;vertical-align:middle;"></span>
            <span style="font-size:11px;color:#94a3b8;font-weight:600;letter-spacing:0.5px;">24/7 PATIENT SUPPORT &amp; RAPID NURSE DISPATCH</span>
          </div>
          <p style="margin:0 0 6px;font-size:12px;color:#64748b;">
            &copy; 2025 {{ brand }}. All rights reserved.
          </p>
          <p style="margin:0;font-size:11px;color:#475569;line-height:1.5;">
            Confidential medical communication. This is an automated notification. Please do not reply directly.
          </p>
        </td>
      </tr>

      <!-- Bottom Accent Bar -->
      <tr>
        <td style="height:3px;background:linear-gradient(90deg,#059669,#0d9488,#38bdf8);"></td>
      </tr>

    </table>
    <!-- /Main Card Container -->

  </td>
</tr>
</table>
<!-- /Outer Background Wrapper -->
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

        # 2. Fallback to SMTP if configured and not dummy placeholder
        is_dummy_smtp = (
            not settings.SMTP_HOST
            or not settings.SMTP_USER
            or "your_email" in settings.SMTP_USER
            or "example.com" in settings.SMTP_USER
            or "your_app_password" in (settings.SMTP_PASSWORD or "")
        )
        if not is_dummy_smtp:
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
                    timeout=4,
                )
                logger.info("Email sent via SMTP", extra={"to": to, "subject": subject})
                return True
            except Exception as exc:  # noqa: BLE001 - never break the request path
                logger.warning(
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
        name = context.get("name", "Valued Customer")
        service = context.get("service", "Healthcare Service")
        reference = context.get("reference", "NHB-PENDING")
        date = context.get("date", "To be confirmed")

        body = f"""
<div style="margin-bottom:20px;">
  <span style="display:inline-block;background:rgba(16,185,129,0.18);border:1px solid rgba(16,185,129,0.4);color:#34d399;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;padding:5px 12px;border-radius:9999px;">
    &#10003; BOOKING RECEIVED
  </span>
</div>

<h2 style="margin:0 0 10px;font-size:22px;font-weight:800;color:#f8fafc;line-height:1.3;">
  Your Service Booking is Registered
</h2>
<p style="margin:0 0 16px;font-size:15px;color:#cbd5e1;line-height:1.6;">
  Dear <strong style="color:#ffffff;">{name}</strong>,
</p>
<p style="margin:0 0 24px;font-size:14px;color:#94a3b8;line-height:1.6;">
  Thank you for choosing <strong>{settings.SMTP_FROM_NAME}</strong>. We have received your booking request and our clinical supervisor is reviewing the care requirements.
</p>

<!-- Booking Summary Card -->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:linear-gradient(135deg,#131f38 0%,#0f172a 100%);border:1px solid rgba(56,189,248,0.22);border-radius:16px;overflow:hidden;margin-bottom:26px;box-shadow:0 8px 30px rgba(0,0,0,0.35);">
  <tr>
    <td style="padding:16px 20px;background:rgba(255,255,255,0.03);border-bottom:1px solid rgba(255,255,255,0.06);">
      <span style="font-size:11px;text-transform:uppercase;letter-spacing:2px;color:#38bdf8;font-weight:700;">Booking Reference</span>
      <div style="margin-top:6px;font-family:'Courier New',monospace;font-size:18px;font-weight:700;color:#2dd4bf;letter-spacing:1px;">
        {reference}
      </div>
    </td>
  </tr>

  <tr>
    <td style="padding:14px 20px;border-bottom:1px solid rgba(255,255,255,0.05);">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr>
          <td width="130" style="font-size:13px;color:#94a3b8;font-weight:600;">Service</td>
          <td style="font-size:15px;color:#f8fafc;font-weight:700;">{service}</td>
        </tr>
      </table>
    </td>
  </tr>

  <tr>
    <td style="padding:14px 20px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr>
          <td width="130" style="font-size:13px;color:#94a3b8;font-weight:600;">Preferred Date</td>
          <td style="font-size:14px;color:#e2e8f0;font-weight:600;">&#128197; {date}</td>
        </tr>
      </table>
    </td>
  </tr>
</table>

<!-- What happens next -->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:20px;">
  <tr>
    <td style="background:rgba(15,23,42,0.6);border:1px solid rgba(148,163,184,0.12);border-radius:12px;padding:18px 20px;">
      <p style="margin:0 0 10px;font-size:13px;font-weight:700;color:#e2e8f0;text-transform:uppercase;letter-spacing:1px;">
        What Happens Next?
      </p>
      <div style="font-size:13px;color:#94a3b8;line-height:1.6;">
        <p style="margin:0 0 6px;">&#9312; <strong>Verification:</strong> Our clinical care manager checks staff availability.</p>
        <p style="margin:0 0 6px;">&#9313; <strong>Confirmation Call:</strong> We will contact your phone number to finalize arrival time.</p>
        <p style="margin:0;">&#9314; <strong>Care Delivery:</strong> Verified nurse or healthcare staff arrives at your home.</p>
      </div>
    </td>
  </tr>
</table>

<p style="margin:0;font-size:13px;color:#64748b;text-align:center;line-height:1.5;">
  Need urgent help or have questions? Contact our 24/7 care desk anytime.
</p>
"""
        return await self.send(
            to,
            f"Booking Received [{reference}] — {service} | {settings.SMTP_FROM_NAME}",
            body,
            text_fallback=f"Dear {name}, thank you for booking {service}. Reference: {reference}, Preferred Date: {date}.",
        )

    async def send_contact_confirmation(self, to: str, context: Dict[str, Any]) -> bool:
        name = context.get("name", "there")
        msg = context.get("message", "")
        body = f"""
<div style="margin-bottom:20px;">
  <span style="display:inline-block;background:rgba(56,189,248,0.15);border:1px solid rgba(56,189,248,0.35);color:#38bdf8;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;padding:5px 12px;border-radius:9999px;">
    &#128172; INQUIRY RECEIVED
  </span>
</div>

<h2 style="margin:0 0 10px;font-size:22px;font-weight:800;color:#f8fafc;line-height:1.3;">
  We Have Received Your Message
</h2>
<p style="margin:0 0 16px;font-size:15px;color:#cbd5e1;line-height:1.6;">
  Dear <strong style="color:#ffffff;">{name}</strong>,
</p>
<p style="margin:0 0 20px;font-size:14px;color:#94a3b8;line-height:1.6;">
  Thank you for reaching out to <strong>{settings.SMTP_FROM_NAME}</strong>. Our care team has received your message and will get back to you shortly.
</p>

<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:rgba(15,23,42,0.6);border-left:4px solid #0d9488;border-radius:0 12px 12px 0;padding:16px 20px;margin-bottom:24px;">
  <tr>
    <td style="font-size:13px;color:#cbd5e1;font-style:italic;line-height:1.6;">
      "{msg}"
    </td>
  </tr>
</table>

<p style="margin:0;font-size:13px;color:#64748b;text-align:center;">
  For immediate medical assistance, please call our 24/7 helpline.
</p>
"""
        return await self.send(to, f"We Received Your Message | {settings.SMTP_FROM_NAME}", body)

    async def send_application_confirmation(self, to: str, context: Dict[str, Any]) -> bool:
        name = context.get("name", "Applicant")
        job = context.get("job", "Healthcare Position")
        ref = context.get("reference", "APP-PENDING")
        body = f"""
<div style="margin-bottom:20px;">
  <span style="display:inline-block;background:rgba(147,51,234,0.18);border:1px solid rgba(168,85,247,0.4);color:#c084fc;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;padding:5px 12px;border-radius:9999px;">
    &#128188; CAREERS APPLICATION
  </span>
</div>

<h2 style="margin:0 0 10px;font-size:22px;font-weight:800;color:#f8fafc;line-height:1.3;">
  Application Submitted Successfully
</h2>
<p style="margin:0 0 16px;font-size:15px;color:#cbd5e1;line-height:1.6;">
  Dear <strong style="color:#ffffff;">{name}</strong>,
</p>
<p style="margin:0 0 20px;font-size:14px;color:#94a3b8;line-height:1.6;">
  Thank you for your interest in joining <strong>{settings.SMTP_FROM_NAME}</strong>. We have received your application for the role of <strong style="color:#38bdf8;">{job}</strong>.
</p>

<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:rgba(15,23,42,0.6);border:1px solid rgba(148,163,184,0.15);border-radius:12px;padding:16px 20px;margin-bottom:24px;">
  <tr>
    <td style="font-size:13px;color:#94a3b8;">Application Reference:</td>
    <td align="right" style="font-family:'Courier New',monospace;font-size:14px;font-weight:700;color:#2dd4bf;">{ref}</td>
  </tr>
</table>

<p style="margin:0 0 12px;font-size:14px;color:#94a3b8;line-height:1.6;">
  Our HR and nursing recruitment team will review your qualifications and credentials. If shortlisted, our representative will contact you for an interview.
</p>
"""
        return await self.send(to, f"Application Received — {job} | {settings.SMTP_FROM_NAME}", body)

    async def send_password_reset(self, to: str, reset_link: str) -> bool:
        body = f"""
<div style="margin-bottom:20px;">
  <span style="display:inline-block;background:rgba(239,68,68,0.15);border:1px solid rgba(239,68,68,0.4);color:#f87171;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;padding:5px 12px;border-radius:9999px;">
    &#128274; SECURITY ALERT
  </span>
</div>

<h2 style="margin:0 0 10px;font-size:22px;font-weight:800;color:#f8fafc;line-height:1.3;">
  Password Reset Request
</h2>
<p style="margin:0 0 16px;font-size:15px;color:#cbd5e1;line-height:1.6;">
  Hello,
</p>
<p style="margin:0 0 24px;font-size:14px;color:#94a3b8;line-height:1.6;">
  We received a request to reset your password for your <strong>{settings.SMTP_FROM_NAME}</strong> account. Click the button below to choose a new password:
</p>

<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:24px 0;">
  <tr>
    <td align="center">
      <a href="{reset_link}" target="_blank" class="cta-button" style="display:inline-block;background:linear-gradient(135deg,#0d9488 0%,#0284c7 100%);color:#ffffff;font-size:15px;font-weight:800;text-decoration:none;padding:15px 36px;border-radius:12px;box-shadow:0 6px 25px rgba(13,148,136,0.4);border:1px solid rgba(255,255,255,0.2);">
        Reset Password &rarr;
      </a>
    </td>
  </tr>
</table>

<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:rgba(15,23,42,0.6);border:1px solid rgba(148,163,184,0.12);border-radius:12px;padding:16px 20px;margin-bottom:20px;">
  <tr>
    <td style="font-size:12px;color:#94a3b8;line-height:1.6;">
      <p style="margin:0 0 6px;"><strong style="color:#e2e8f0;">Security Notice:</strong> This link will expire in <strong>30 minutes</strong>.</p>
      <p style="margin:0;">If you did not request a password reset, you can safely ignore this email. Your account remains completely secure.</p>
    </td>
  </tr>
</table>

<p style="margin:0;font-size:12px;color:#64748b;word-break:break-all;">
  Button not working? Copy and paste this link into your browser:<br/>
  <a href="{reset_link}" style="color:#38bdf8;text-decoration:none;">{reset_link}</a>
</p>
"""
        return await self.send(to, f"Password Reset Request | {settings.SMTP_FROM_NAME}", body)

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
            "day": "Day Shift",
            "night": "Night Shift",
            "full_24h": "24 Hours (Full Day)",
            "custom": "Custom Shift",
        }.get(shift, shift)

        portal_url = "https://srikrishna-seva-niketan-admin.vercel.app/staff/status"

        body = f"""
<!-- Notification Badge -->
<div style="margin-bottom:20px;">
  <span style="display:inline-block;background:rgba(13,148,136,0.18);border:1px solid rgba(20,184,166,0.4);color:#2dd4bf;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;padding:5px 12px;border-radius:9999px;">
    &#128657; OFFICIAL DUTY ROSTER
  </span>
</div>

<!-- Heading & Salutation -->
<h2 style="margin:0 0 10px;font-size:22px;font-weight:800;color:#f8fafc;line-height:1.3;">
  New Duty Assignment Scheduled
</h2>
<p style="margin:0 0 18px;font-size:15px;color:#cbd5e1;line-height:1.6;">
  Dear <strong style="color:#ffffff;">{staff_name}</strong>,
</p>
<p style="margin:0 0 24px;font-size:14px;color:#94a3b8;line-height:1.6;">
  You have been assigned to an active patient care duty by the clinical operations team. Please carefully review your duty schedule and client destination details below:
</p>

<!-- Duty Details Glass Card -->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:linear-gradient(135deg,#131f38 0%,#0f172a 100%);border:1px solid rgba(56,189,248,0.22);border-radius:16px;overflow:hidden;margin-bottom:26px;box-shadow:0 8px 30px rgba(0,0,0,0.35);">
  <tr>
    <td style="padding:16px 20px;background:rgba(255,255,255,0.03);border-bottom:1px solid rgba(255,255,255,0.06);">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr>
          <td>
            <span style="font-size:11px;text-transform:uppercase;letter-spacing:2px;color:#38bdf8;font-weight:700;">Engagement Summary</span>
          </td>
          <td align="right">
            <span style="background:rgba(16,185,129,0.18);color:#34d399;border:1px solid rgba(16,185,129,0.35);font-size:11px;font-weight:700;padding:3px 10px;border-radius:9999px;">Active Posting</span>
          </td>
        </tr>
      </table>
    </td>
  </tr>

  <!-- Field: Client -->
  <tr>
    <td style="padding:14px 20px;border-bottom:1px solid rgba(255,255,255,0.05);">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr>
          <td width="130" style="vertical-align:top;font-size:13px;color:#94a3b8;font-weight:600;">Client / Patient</td>
          <td style="font-size:15px;color:#f8fafc;font-weight:700;">{customer_name}</td>
        </tr>
      </table>
    </td>
  </tr>

  <!-- Field: Location -->
  <tr>
    <td style="padding:14px 20px;border-bottom:1px solid rgba(255,255,255,0.05);">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr>
          <td width="130" style="vertical-align:top;font-size:13px;color:#94a3b8;font-weight:600;">Posted At</td>
          <td style="font-size:14px;color:#e2e8f0;line-height:1.5;">&#128205; {place}</td>
        </tr>
      </table>
    </td>
  </tr>

  <!-- Field: Shift -->
  <tr>
    <td style="padding:14px 20px;border-bottom:1px solid rgba(255,255,255,0.05);">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr>
          <td width="130" style="vertical-align:top;font-size:13px;color:#94a3b8;font-weight:600;">Shift Schedule</td>
          <td>
            <span style="display:inline-block;background:rgba(20,184,166,0.15);color:#2dd4bf;border:1px solid rgba(20,184,166,0.35);padding:3px 10px;border-radius:6px;font-size:13px;font-weight:700;">
              &#9201; {shift_label}
            </span>
          </td>
        </tr>
      </table>
    </td>
  </tr>

  <!-- Field: Dates -->
  <tr>
    <td style="padding:14px 20px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr>
          <td width="130" style="vertical-align:top;font-size:13px;color:#94a3b8;font-weight:600;">Duration Period</td>
          <td style="font-size:14px;color:#f8fafc;font-weight:600;">
            {start_date} <span style="color:#64748b;margin:0 4px;">&rarr;</span> {end_date}
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>

<!-- Primary CTA Button -->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:28px 0 24px;">
  <tr>
    <td align="center">
      <a href="{portal_url}" target="_blank" class="cta-button" style="display:inline-block;background:linear-gradient(135deg,#0d9488 0%,#0284c7 100%);color:#ffffff;font-size:15px;font-weight:800;text-decoration:none;padding:15px 36px;border-radius:12px;box-shadow:0 6px 25px rgba(13,148,136,0.4);border:1px solid rgba(255,255,255,0.2);letter-spacing:0.4px;">
        Open Staff Portal &amp; View Live Route &rarr;
      </a>
    </td>
  </tr>
</table>

<!-- Operating Instructions -->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:20px;">
  <tr>
    <td style="background:rgba(15,23,42,0.6);border:1px solid rgba(148,163,184,0.12);border-radius:12px;padding:16px 20px;">
      <p style="margin:0 0 8px;font-size:12px;font-weight:700;color:#e2e8f0;text-transform:uppercase;letter-spacing:1px;">
        &#9888; Important Duty Guidelines:
      </p>
      <ul style="margin:0;padding-left:18px;font-size:13px;color:#94a3b8;line-height:1.6;">
        <li>Please reach the patient location at least 15 minutes before shift begins.</li>
        <li>Ensure you carry your official uniform, nurse badge, and necessary medical equipment kit.</li>
        <li>Turn on GPS on your mobile device to view real-time road directions in your Staff Portal.</li>
        <li>In case of any emergency or shift timing issue, notify the branch coordinator immediately.</li>
      </ul>
    </td>
  </tr>
</table>
"""
        return await self.send(
            to=to,
            subject=f"New Duty Assignment ({shift_label}) | {settings.SMTP_FROM_NAME}",
            body_html=body,
            text_fallback=(
                f"Dear {staff_name}, you have a new duty assignment.\n"
                f"Client: {customer_name}\n"
                f"Location: {place}\n"
                f"Shift: {shift_label}\n"
                f"Duration: {start_date} to {end_date}\n\n"
                f"Please open your staff portal to view live road navigation: {portal_url}"
            ),
        )

    async def send_admin_notification(self, subject: str, body_html: str) -> bool:
        if not settings.ADMIN_NOTIFICATION_EMAIL:
            return False
        styled_body = f"""
<div style="margin-bottom:20px;">
  <span style="display:inline-block;background:rgba(245,158,11,0.18);border:1px solid rgba(245,158,11,0.4);color:#fbbf24;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;padding:5px 12px;border-radius:9999px;">
    &#9889; OPERATIONS ALERT
  </span>
</div>
<h2 style="margin:0 0 16px;font-size:20px;font-weight:800;color:#f8fafc;">{subject}</h2>
<div style="background:rgba(15,23,42,0.6);border:1px solid rgba(148,163,184,0.15);border-radius:12px;padding:20px;font-size:14px;color:#cbd5e1;line-height:1.6;">
  {body_html}
</div>
"""
        return await self.send(settings.ADMIN_NOTIFICATION_EMAIL, subject, styled_body)


# Module-level singleton.
email_service = EmailService()
