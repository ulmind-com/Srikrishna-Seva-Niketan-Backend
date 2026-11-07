"""Monthly customer billing — one bill per (nurse + customer) per calendar month.

The amount is computed live from the assignments that overlap the month
(daily_rate × days-in-month), grouped by nurse + customer. A bill number is
allocated on demand and kept stable thereafter (idempotent per month).
"""

from __future__ import annotations

import calendar
import datetime as dt
from typing import Optional

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.exceptions import BadRequestException
from app.core.responses import success_response
from app.dependencies.auth import ActorContext, require_permission
from app.models.assignment import Assignment
from app.models.billing import MonthlyBill
from app.models.customer import Customer
from app.models.employee import Employee
from app.models.location import Location
from app.schemas.assignment import _format_shift

router = APIRouter(prefix="/billing", tags=["Billing"])

_MONTHLY_BILL_NO_BASE = 1000


def _month_bounds(month: str) -> tuple[dt.date, dt.date]:
    try:
        y_str, m_str = month.split("-")
        y, m = int(y_str), int(m_str)
        start = dt.date(y, m, 1)
    except (ValueError, AttributeError):
        raise BadRequestException("month must be in YYYY-MM format")
    last_day = calendar.monthrange(y, m)[1]
    return start, dt.date(y, m, last_day)


def _overlap_days(
    a_start: dt.date, a_end: Optional[dt.date], m_start: dt.date, m_end: dt.date
) -> int:
    """Days of an engagement that fall inside the month (inclusive)."""
    s = max(a_start, m_start)
    e = min(a_end or m_end, m_end)
    if e < s:
        return 0
    return (e - s).days + 1


def _daily_rate(a: Assignment) -> int:
    """Per-day customer charge — explicit daily rate, else derived from the total."""
    if a.customer_daily_rate is not None:
        return a.customer_daily_rate
    if a.customer_rate and a.end_date:
        total = (a.end_date - a.start_date).days + 1
        if total > 0:
            return round(a.customer_rate / total)
    return 0


async def _compute_month(
    actor: ActorContext, month: str, location_id: Optional[str]
) -> list[dict]:
    """Group month-overlapping assignments by (nurse, customer) into bills."""
    m_start, m_end = _month_bounds(month)
    m_start_dt = dt.datetime.combine(m_start, dt.time.min)
    m_end_dt = dt.datetime.combine(m_end, dt.time.min)

    filters: dict = actor.scope_filter()
    if location_id and actor.can_access_location(location_id):
        filters["location_id"] = location_id
    filters["start_date"] = {"$lte": m_end_dt}
    filters["$or"] = [{"end_date": None}, {"end_date": {"$gte": m_start_dt}}]

    assignments = await Assignment.find(filters).to_list()

    # Resolve display details.
    def _oid_set(ids: set[str]) -> list[ObjectId]:
        out = []
        for i in ids:
            try:
                out.append(ObjectId(i))
            except (InvalidId, TypeError):
                pass
        return out

    cust_ids = {a.customer_id for a in assignments}
    emp_ids = {a.employee_id for a in assignments}
    customers = {
        str(c.id): c
        for c in await Customer.find({"_id": {"$in": _oid_set(cust_ids)}}).to_list()
    }
    employees = {
        str(e.id): e
        for e in await Employee.find({"_id": {"$in": _oid_set(emp_ids)}}).to_list()
    }
    locations = {str(loc.id): loc for loc in await Location.find({}).to_list()}
    issued = {mb.key: mb for mb in await MonthlyBill.find({"month": month}).to_list()}

    groups: dict[tuple[str, str], dict] = {}
    for a in assignments:
        days = _overlap_days(a.start_date, a.end_date, m_start, m_end)
        if days <= 0:
            continue
        rate = _daily_rate(a)
        amount = rate * days
        key = (a.employee_id, a.customer_id)
        g = groups.get(key)
        if g is None:
            cust = customers.get(a.customer_id)
            emp = employees.get(a.employee_id)
            loc = locations.get(a.location_id)
            g = {
                "key": f"{a.employee_id}:{a.customer_id}:{month}",
                "employee_id": a.employee_id,
                "employee_name": emp.name if emp else a.employee_name,
                "employee_code": emp.code if emp else None,
                "customer_id": a.customer_id,
                "customer_name": cust.name if cust else a.customer_name,
                "customer_phone": cust.phone if cust else None,
                "customer_alt_phone": cust.alt_phone if cust else None,
                "customer_address": (cust.address if cust else None) or a.address,
                "location_id": a.location_id,
                "location_name": loc.name if loc else None,
                "line_items": [],
                "total_days": 0,
                "total_amount": 0,
            }
            groups[key] = g
        row_start = max(a.start_date, m_start)
        row_end = min(a.end_date or m_end, m_end)
        g["line_items"].append(
            {
                "assignment_id": str(a.id),
                "from": row_start.isoformat(),
                "to": row_end.isoformat(),
                "shift": a.shift.value,
                "shift_label": _format_shift(a.shift, a.start_time, a.end_time),
                "start_time": a.start_time,
                "end_time": a.end_time,
                "days": days,
                "daily_rate": rate,
                "amount": amount,
            }
        )
        g["total_days"] += days
        g["total_amount"] += amount

    result = list(groups.values())
    for g in result:
        mb = issued.get(g["key"])
        g["bill_no"] = mb.bill_no if mb else None
    result.sort(key=lambda g: ((g["employee_name"] or "").lower(), (g["customer_name"] or "").lower()))
    return result


@router.get("/monthly", summary="Monthly per-nurse customer bills")
async def monthly_bills(
    month: str = Query(..., description="Billing month, YYYY-MM"),
    location_id: Optional[str] = Query(None),
    actor: ActorContext = Depends(require_permission("assignments", "view")),
) -> dict:
    bills = await _compute_month(actor, month, location_id)
    totals = {
        "bills": len(bills),
        "total_days": sum(b["total_days"] for b in bills),
        "total_amount": sum(b["total_amount"] for b in bills),
    }
    return success_response(data={"month": month, "totals": totals, "bills": bills})


async def _next_monthly_bill_no() -> int:
    from pymongo import ReturnDocument

    from app.database.connection import get_database

    db = get_database()
    res = await db["counters"].find_one_and_update(
        {"_id": "monthly_bill_no"},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return _MONTHLY_BILL_NO_BASE + int(res["seq"])


class IssueMonthlyBillRequest(BaseModel):
    month: str
    employee_id: str
    customer_id: str


@router.post("/monthly/issue", summary="Allocate a monthly bill number")
async def issue_monthly_bill(
    payload: IssueMonthlyBillRequest,
    actor: ActorContext = Depends(require_permission("assignments", "view")),
) -> dict:
    """Allocate (or reuse) the bill number for one nurse+customer month bill."""
    key = f"{payload.employee_id}:{payload.customer_id}:{payload.month}"
    existing = await MonthlyBill.find_one({"key": key})
    if existing is not None:
        return success_response(data={"bill_no": existing.bill_no})

    # Recompute the group so the snapshot + scope check are honest.
    bills = await _compute_month(actor, payload.month, None)
    group = next((b for b in bills if b["key"] == key), None)
    if group is None:
        raise BadRequestException("No billable duty for this nurse/customer this month")

    bill_no = await _next_monthly_bill_no()
    mb = MonthlyBill(
        key=key,
        employee_id=payload.employee_id,
        customer_id=payload.customer_id,
        location_id=group.get("location_id"),
        month=payload.month,
        bill_no=bill_no,
        total_days=group.get("total_days"),
        total_amount=group.get("total_amount"),
    )
    await mb.insert()
    return success_response(data={"bill_no": bill_no})
