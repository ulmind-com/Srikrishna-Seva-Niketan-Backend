"""Background scheduler — emails a daily business report to the super admins.

Runs in-process via APScheduler's asyncio scheduler, started from the app
lifespan. One job fires each day at ``DAILY_REPORT_HOUR`` (local
``REPORT_TIMEZONE``) and mails that day's P&L and operational summary.
"""

from __future__ import annotations

import datetime as dt
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config.settings import settings
from app.models.user import User
from app.services import report_service
from app.services.email_service import email_service

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def _rupee(n) -> str:
    return f"Rs {int(n or 0):,}"


async def _recipients() -> list[str]:
    """Super-admin emails plus the configured notification address (deduped)."""
    emails: set[str] = set()
    if settings.ADMIN_NOTIFICATION_EMAIL:
        emails.add(settings.ADMIN_NOTIFICATION_EMAIL)
    supers = await User.find({"is_superuser": True, "is_active": True}).to_list()
    for u in supers:
        if u.email:
            emails.add(str(u.email))
    return sorted(emails)


async def send_daily_report() -> None:
    """Compute today's figures (all branches) and email the super admins."""
    today = dt.date.today()
    scope: dict = {}  # all branches — this is the head-office digest

    pnl = await report_service.profit_loss(scope, today, today)
    summary = await report_service.daily_summary(scope, today)

    payslip_rows = "".join(
        f"<tr><td style='padding:3px 10px 3px 0'>{s['employee_name']}</td>"
        f"<td>{s['engagements']}</td>"
        f"<td style='text-align:right'>{_rupee(s['total_payout'])}</td></tr>"
        for s in pnl.get("payslips", [])[:20]
    ) or "<tr><td colspan='3'>No payouts today.</td></tr>"

    profit = pnl.get("profit", 0)
    profit_color = "#16a34a" if profit >= 0 else "#dc2626"

    body = (
        f"<p>Daily report for <b>{today.isoformat()}</b> (all branches).</p>"
        "<h3 style='margin:14px 0 4px'>Finance</h3>"
        "<table style='border-collapse:collapse'>"
        f"<tr><td style='padding:3px 14px 3px 0'>Revenue</td><td><b>{_rupee(pnl.get('revenue'))}</b></td></tr>"
        f"<tr><td style='padding:3px 14px 3px 0'>Staff payout</td><td>{_rupee(pnl.get('staff_payout'))}</td></tr>"
        f"<tr><td style='padding:3px 14px 3px 0'>Total expense</td><td>{_rupee(pnl.get('expense'))}</td></tr>"
        f"<tr><td style='padding:3px 14px 3px 0'>Profit / Loss</td><td style='color:{profit_color}'><b>{_rupee(profit)}</b></td></tr>"
        f"<tr><td style='padding:3px 14px 3px 0'>Margin</td><td>{pnl.get('margin_pct', 0)}%</td></tr>"
        "</table>"
        "<h3 style='margin:14px 0 4px'>Operations</h3>"
        "<table style='border-collapse:collapse'>"
        f"<tr><td style='padding:3px 14px 3px 0'>Total staff</td><td>{summary.get('total_staff', 0)}</td></tr>"
        f"<tr><td style='padding:3px 14px 3px 0'>On duty</td><td>{summary.get('assigned_staff', 0)}</td></tr>"
        f"<tr><td style='padding:3px 14px 3px 0'>Available (vacancy)</td><td>{summary.get('available_staff', 0)}</td></tr>"
        f"<tr><td style='padding:3px 14px 3px 0'>Today's shifts</td><td>{summary.get('todays_shifts', 0)}</td></tr>"
        f"<tr><td style='padding:3px 14px 3px 0'>Active customers</td><td>{summary.get('active_customers', 0)}</td></tr>"
        "</table>"
        "<h3 style='margin:14px 0 4px'>Staff payout</h3>"
        "<table style='border-collapse:collapse'>"
        "<tr><td><b>Staff</b></td><td><b>Engagements</b></td><td><b>Payout</b></td></tr>"
        f"{payslip_rows}"
        "</table>"
    )

    recipients = await _recipients()
    if not recipients:
        logger.warning("Daily report: no recipient emails configured")
        return
    subject = f"Daily Report — {today.isoformat()} | Srikrishna Seva Niketan"
    for to in recipients:
        try:
            await email_service.send(to, subject, body)
        except Exception:  # noqa: BLE001 — one bad address must not stop the rest
            logger.warning("Daily report email failed", extra={"to": to})
    logger.info("Daily report sent", extra={"recipients": len(recipients)})


def start_scheduler() -> None:
    """Start the daily-report scheduler (idempotent)."""
    global _scheduler
    if not settings.ENABLE_SCHEDULER:
        logger.info("Scheduler disabled")
        return
    if _scheduler is not None:
        return
    _scheduler = AsyncIOScheduler(timezone=settings.REPORT_TIMEZONE)
    _scheduler.add_job(
        send_daily_report,
        CronTrigger(hour=settings.DAILY_REPORT_HOUR, minute=0),
        id="daily_report",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    _scheduler.start()
    logger.info(
        "Daily report scheduler started",
        extra={"hour": settings.DAILY_REPORT_HOUR, "tz": settings.REPORT_TIMEZONE},
    )


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
