"""Tests for email service rendering and template integrity."""

import pytest
from app.services.email_service import email_service


def test_base_template_safe_rendering():
    """Verify that HTML tags inside body are NOT escaped by Jinja2."""
    raw_body = "<p>Dear <b>Nurse</b>,</p><table style='border-collapse:collapse'><tr><td>Posting</td></tr></table>"
    rendered = email_service._render(raw_body, subject="Test Email")

    # Critical check: raw tags must NOT be escaped
    assert "&lt;p&gt;" not in rendered
    assert "&lt;table" not in rendered
    assert "&lt;td&gt;" not in rendered

    # Tags must be preserved cleanly in the output HTML
    assert "<p>Dear <b>Nurse</b>,</p>" in rendered
    assert "Srikrishna Seva Niketan" in rendered
    assert "Healthcare &amp; Nursing Services" in rendered


@pytest.mark.asyncio
async def test_send_assignment_notice_html_structure(monkeypatch):
    """Verify that duty assignment email body is structured and contains required elements."""
    captured = {}

    async def fake_send(to, subject, body_html, text_fallback=None):
        captured["to"] = to
        captured["subject"] = subject
        captured["body_html"] = body_html
        captured["rendered"] = email_service._render(body_html, subject)
        return True

    monkeypatch.setattr(email_service, "send", fake_send)

    await email_service.send_assignment_notice(
        to="nurse@example.com",
        staff_name="Arnab Senapati",
        customer_name="Sagnik Mondal",
        shift="full_24h",
        place="Apollo Gleneagles, Kolkata",
        start_date="2026-10-29",
        end_date="2026-10-31",
    )

    assert captured["to"] == "nurse@example.com"
    assert "New Duty Assignment" in captured["subject"]
    body = captured["body_html"]
    rendered = captured["rendered"]

    # Verify content inside the email
    assert "Arnab Senapati" in body
    assert "Sagnik Mondal" in body
    assert "Apollo Gleneagles, Kolkata" in body
    assert "24 Hours (Full Day)" in body
    assert "Open Staff Portal" in body

    # Verify no raw escape codes
    assert "&lt;" not in rendered

