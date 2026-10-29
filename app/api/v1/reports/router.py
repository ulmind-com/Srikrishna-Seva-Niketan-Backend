"""Reporting endpoints — profit & loss, distribution, payments, daily summary."""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query

from app.core.responses import success_response
from app.dependencies.auth import ActorContext, require_permission
from app.services import report_service

router = APIRouter(prefix="/reports", tags=["Reports"])


def _effective_scope(actor: ActorContext, location_id: Optional[str]) -> Dict[str, Any]:
    """Narrow the actor's branch scope to a single branch when requested."""
    if location_id and actor.can_access_location(location_id):
        return {"location_id": location_id}
    return actor.scope_filter()


@router.get("/profit-loss", summary="Firm profit & loss report")
async def report_profit_loss(
    start: Optional[dt.date] = Query(None),
    end: Optional[dt.date] = Query(None),
    location_id: Optional[str] = Query(None),
    actor: ActorContext = Depends(require_permission("reports", "view")),
) -> dict:
    data = await report_service.profit_loss(_effective_scope(actor, location_id), start, end)
    return success_response(data=data)


@router.get("/distribution", summary="Staff-to-customer distribution report")
async def report_distribution(
    location_id: Optional[str] = Query(None),
    actor: ActorContext = Depends(require_permission("reports", "view")),
) -> dict:
    data = await report_service.distribution(_effective_scope(actor, location_id))
    return success_response(data=data)


@router.get("/customer-payments", summary="Customer payment report")
async def report_customer_payments(
    location_id: Optional[str] = Query(None),
    actor: ActorContext = Depends(require_permission("reports", "view")),
) -> dict:
    data = await report_service.customer_payments(_effective_scope(actor, location_id))
    return success_response(data=data)


@router.get("/daily-summary", summary="Daily summary & vacancies")
async def report_daily_summary(
    on: Optional[dt.date] = Query(None),
    location_id: Optional[str] = Query(None),
    actor: ActorContext = Depends(require_permission("reports", "view")),
) -> dict:
    data = await report_service.daily_summary(_effective_scope(actor, location_id), on)
    return success_response(data=data)


@router.get("/duties", summary="Comprehensive duty and payment details report")
async def report_duties(
    start: Optional[dt.date] = Query(None),
    end: Optional[dt.date] = Query(None),
    payment_type: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    location_id: Optional[str] = Query(None),
    actor: ActorContext = Depends(require_permission("reports", "view")),
) -> dict:
    data = await report_service.duty_report(
        _effective_scope(actor, location_id),
        start=start,
        end=end,
        payment_type=payment_type,
        status=status,
    )
    return success_response(data=data)


@router.post("/daily-email", summary="Send today's daily report email now")
async def send_daily_email_now(
    actor: ActorContext = Depends(require_permission("reports", "view")),
) -> dict:
    """Trigger the daily report email immediately (same as the nightly job)."""
    from app.services.scheduler import send_daily_report

    await send_daily_report()
    return success_response(message="Daily report emailed to the super admins")
