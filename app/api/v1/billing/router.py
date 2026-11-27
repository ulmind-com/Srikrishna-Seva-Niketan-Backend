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
from app.models.enums import AssignmentStatus, ShiftType
from app.models.location import Location
from app.schemas.assignment import _format_shift, calculate_duty_days
from app.utils.bill_number import format_bill_no, next_sequential_bill_no
from app.utils.ist_time import today_ist

router = APIRouter(prefix="/billing", tags=["Billing"])


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
    if a.customer_daily_rate is not None and a.customer_daily_rate > 0:
        return a.customer_daily_rate
    total_ref = a.customer_rate or a.contract_amount
    if total_ref:
        duty_days = a.days_worked
        if not duty_days and a.end_date:
            duty_days = calculate_duty_days(
                a.start_date, a.end_date, a.start_time, a.end_time, a.shift, a.duty_breaks
            )
        if duty_days and duty_days > 0:
            return round(total_ref / duty_days)
    return 0


async def _compute_bills(
    actor: ActorContext,
    start_date: dt.date,
    end_date: dt.date,
    location_id: Optional[str] = None,
    customer_id: Optional[str] = None,
    month_label: Optional[str] = None,
) -> list[dict]:
    """Group date-range-overlapping assignments by customer into statement bills."""
    m_start_dt = dt.datetime.combine(start_date, dt.time.min)
    m_end_dt = dt.datetime.combine(end_date, dt.time.max)

    filters: dict = actor.scope_filter()
    # Only include assignments whose work has ended (end_date set or completed/replaced; exclude ongoing/cancelled).
    filters["status"] = {"$ne": "cancelled"}
    if location_id and actor.can_access_location(location_id):
        filters["location_id"] = location_id
    if customer_id:
        filters["customer_id"] = customer_id
    filters["start_date"] = {"$lte": m_end_dt}
    filters["$or"] = [
        {"end_date": {"$ne": None, "$gte": m_start_dt}},
        {"status": {"$in": [AssignmentStatus.COMPLETED.value, AssignmentStatus.REPLACED.value]}},
    ]

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

    period_key = month_label or f"{start_date.isoformat()}_{end_date.isoformat()}"
    issued = {mb.key: mb for mb in await MonthlyBill.find({}).to_list()}

    from app.models.finance import Payment
    all_paid_rows = await Payment.find(
        {"status": {"$ne": "pending"}}
    ).to_list()
    paid_by_asg: dict[str, int] = {}
    for p in all_paid_rows:
        if p.assignment_id:
            paid_by_asg[p.assignment_id] = paid_by_asg.get(p.assignment_id, 0) + p.amount

    # Group by customer so one customer receives ONE consolidated bill,
    # even when duty was split across multiple staff members (e.g. replacement).
    groups: dict[str, dict] = {}
    for a in assignments:
        # Do not generate a bill for ongoing duties whose work hasn't finished yet.
        if a.end_date is None and a.status not in (AssignmentStatus.COMPLETED, AssignmentStatus.REPLACED):
            continue
        clipped_start = a.start_date < start_date
        clipped_end = a.end_date is None or a.end_date > end_date
        row_start = max(a.start_date, start_date)
        row_end = min(a.end_date or end_date, end_date)
        if row_end < row_start:
            continue

        # Replacement staff taking over on the exact boundary cutoff date of a 24h cycle
        # has 0 completed hours inside [start_date, end_date].
        if a.is_replacement and a.shift == ShiftType.FULL_24H and clipped_end and a.start_date == end_date:
            continue

        if a.shift == ShiftType.FULL_24H:
            row_st = None if (clipped_start and clipped_end) else a.start_time
            row_et = None if clipped_end else a.end_time
        else:
            row_st = a.start_time
            row_et = a.end_time

        days = calculate_duty_days(
            row_start,
            row_end,
            row_st,
            row_et,
            a.shift,
            a.duty_breaks,
            a.shift_windows,
            is_handover=(not clipped_end and a.status == AssignmentStatus.REPLACED),
        )

        if days <= 0:
            continue

        rate = _daily_rate(a)
        if a.customer_daily_rate is not None and a.customer_daily_rate > 0:
            amount = int(round(a.customer_daily_rate * days))
        elif not clipped_start and not clipped_end and a.customer_rate is not None:
            amount = a.customer_rate
        else:
            amount = int(round(rate * days))

        cust_id = str(a.customer_id)
        g = groups.get(cust_id)
        if g is None:
            cust = customers.get(a.customer_id)
            loc = locations.get(a.location_id)
            g = {
                "key": f"{a.customer_id}:{period_key}",
                "customer_id": a.customer_id,
                "customer_name": cust.name if cust else a.customer_name,
                "customer_phone": cust.phone if cust else None,
                "customer_alt_phone": cust.alt_phone if cust else None,
                "customer_address": (cust.address if cust else None) or a.address,
                "location_id": a.location_id,
                "location_name": loc.name if loc else None,
                "branch_phone": loc.phone if loc else "9239701265",
                "branch_city": loc.city if loc else "Arambagh",
                "branch_pincode": loc.pincode if loc else "712601",
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "employee_id": a.employee_id,
                "employee_name": "",
                "employee_code": None,
                "employee_names": [],
                "line_items": [],
                "total_days": 0.0,
                "total_amount": 0,
            }
            groups[cust_id] = g

        emp = employees.get(a.employee_id)
        emp_name = emp.name if emp else a.employee_name
        emp_code = emp.code if emp else None
        emp_cat = emp.category if emp else None

        g["line_items"].append(
            {
                "assignment_id": str(a.id),
                "employee_id": a.employee_id,
                "employee_name": emp_name,
                "employee_code": emp_code,
                "employee_category": emp_cat,
                "from": row_start.isoformat(),
                "to": row_end.isoformat(),
                "from_time": row_st or a.start_time,
                "to_time": None if clipped_end else (row_et or a.end_time),
                "is_ongoing": not bool(a.end_date),
                "shift": a.shift.value,
                "shift_label": _format_shift(a.shift, a.start_time, a.end_time, a.shift_windows),
                "start_time": a.start_time,
                "end_time": a.end_time,
                "shift_windows": [w.model_dump() for w in a.shift_windows] if a.shift_windows else None,
                "days": days,
                "daily_rate": rate,
                "amount": amount,
                "bill_no": a.bill_no,
                "is_replacement": a.is_replacement,
                "replacement_reason": a.replacement_reason,
                "replacement_for_employee_name": a.replacement_for_employee_name,
                "replaced_by_employee_name": a.replaced_by_employee_name,
            }
        )
        if emp_name and emp_name not in g["employee_names"]:
            g["employee_names"].append(emp_name)
        g["total_days"] = round(g["total_days"] + days, 2)
        g["total_amount"] += amount

    result = list(groups.values())
    for g in result:
        # Sort line items chronologically
        g["line_items"].sort(key=lambda li: li["from"])
        g["employee_name"] = ", ".join(g["employee_names"])
        if len(g["employee_names"]) == 1 and g["line_items"]:
            g["employee_code"] = g["line_items"][0]["employee_code"]
        elif len(g["employee_names"]) > 1:
            g["employee_code"] = f"{len(g['employee_names'])} Staff"

        # Check if bill number already allocated
        mb = issued.get(g["key"])
        if not mb and month_label:
            mb = issued.get(f"{g['customer_id']}:{month_label}")
        if not mb:
            for li in g["line_items"]:
                if month_label:
                    mb = issued.get(f"{li['employee_id']}:{g['customer_id']}:{month_label}")
                if not mb and li.get("bill_no"):
                    g["bill_no"] = li["bill_no"]
                    break
        if mb:
            g["bill_no"] = mb.bill_no
        g["bill_no_formatted"] = format_bill_no(g.get("bill_no"))
        paid_total = sum(
            paid_by_asg.get(li["assignment_id"], 0) for li in g["line_items"]
        )
        g["paid_amount"] = paid_total
        g["outstanding_amount"] = max(0, g["total_amount"] - paid_total)
        if paid_total >= g["total_amount"] and g["total_amount"] > 0:
            g["payment_status"] = "paid"
        elif paid_total > 0:
            g["payment_status"] = "partial"
        else:
            g["payment_status"] = "pending"

    result.sort(key=lambda g: (g["customer_name"] or "").lower())
    return result


@router.get("/monthly", summary="General & Monthly customer bills")
@router.get("/general", summary="General customer statement bills")
async def monthly_bills(
    month: Optional[str] = Query(None, description="Billing month, YYYY-MM"),
    start_date: Optional[dt.date] = Query(None, description="Custom start date (inclusive)"),
    end_date: Optional[dt.date] = Query(None, description="Custom end date (inclusive)"),
    location_id: Optional[str] = Query(None),
    customer_id: Optional[str] = Query(None),
    actor: ActorContext = Depends(require_permission("assignments", "view")),
) -> dict:
    if start_date and end_date:
        if end_date < start_date:
            raise BadRequestException("End date cannot be earlier than start date")
        m_start = start_date
        m_end = end_date
        label = month or f"{start_date.isoformat()} to {end_date.isoformat()}"
    elif month:
        m_start, m_end = _month_bounds(month)
        label = month
    else:
        today = today_ist()
        month = f"{today.year:04d}-{today.month:02d}"
        m_start, m_end = _month_bounds(month)
        label = month

    # Ensure legacy >=1000 bill numbers (if any exist in DB before v2 migration) are cleared before read
    try:
        from app.database.connection import get_database
        _db = get_database()
    except Exception:
        _db = MonthlyBill.get_motor_collection().database
    if await _db["counters"].find_one({"_id": "monthly_bill_seq_v2"}) is None:
        if await _db["counters"].find_one({"_id": "monthly_bill_no"}) is not None:
            await _db["monthly_bills"].delete_many({"bill_no": {"$gte": 1000}})
            await _db["assignments"].update_many({"bill_no": {"$gte": 1000}}, {"$set": {"bill_no": None}})
            await _db["counters"].update_one({"_id": "monthly_bill_seq_v2"}, {"$setOnInsert": {"seq": 0}}, upsert=True)

    bills = await _compute_bills(actor, m_start, m_end, location_id, customer_id, month)
    totals = {
        "bills": len(bills),
        "total_days": round(sum(b["total_days"] for b in bills), 2),
        "total_amount": sum(b["total_amount"] for b in bills),
        "total_paid": sum(b.get("paid_amount", 0) for b in bills),
        "total_outstanding": sum(b.get("outstanding_amount", b["total_amount"]) for b in bills),
    }
    return success_response(
        data={
            "month": month,
            "period": {"start": m_start.isoformat(), "end": m_end.isoformat(), "label": label},
            "totals": totals,
            "bills": bills,
        }
    )


async def _next_monthly_bill_no() -> int:
    try:
        from app.database.connection import get_database
        db = get_database()
    except Exception:
        db = MonthlyBill.get_motor_collection().database

    return await next_sequential_bill_no(db, "monthly")


class IssueMonthlyBillRequest(BaseModel):
    month: Optional[str] = None
    start_date: Optional[dt.date] = None
    end_date: Optional[dt.date] = None
    customer_id: str
    employee_id: Optional[str] = None


@router.post("/monthly/issue", summary="Allocate a monthly bill number")
@router.post("/issue", summary="Allocate a general bill number")
async def issue_monthly_bill(
    payload: IssueMonthlyBillRequest,
    actor: ActorContext = Depends(require_permission("assignments", "view")),
) -> dict:
    """Allocate (or reuse) the bill number for one customer's bill."""
    period_key = payload.month or (
        f"{payload.start_date.isoformat()}_{payload.end_date.isoformat()}"
        if payload.start_date and payload.end_date
        else "current"
    )
    key = f"{payload.customer_id}:{period_key}"
    existing = await MonthlyBill.find_one({"key": key})
    if existing is None and payload.employee_id:
        existing = await MonthlyBill.find_one(
            {"key": f"{payload.employee_id}:{payload.customer_id}:{period_key}"}
        )
    if existing is not None:
        return success_response(data={"bill_no": existing.bill_no, "bill_no_formatted": format_bill_no(existing.bill_no)})

    if payload.start_date and payload.end_date:
        m_start = payload.start_date
        m_end = payload.end_date
    elif payload.month:
        m_start, m_end = _month_bounds(payload.month)
    else:
        today = today_ist()
        m_start, m_end = _month_bounds(f"{today.year:04d}-{today.month:02d}")

    # Recompute the group so the snapshot + scope check are honest.
    bills = await _compute_bills(actor, m_start, m_end, None, payload.customer_id, payload.month)
    group = next((b for b in bills if b["customer_id"] == payload.customer_id), None)
    if group is None:
        raise BadRequestException("No billable duty for this customer in this period")

    bill_no = await _next_monthly_bill_no()
    mb = MonthlyBill(
        key=key,
        employee_id=payload.employee_id or group.get("employee_id") or "multiple",
        customer_id=payload.customer_id,
        location_id=group.get("location_id"),
        month=payload.month or period_key,
        bill_no=bill_no,
        total_days=group.get("total_days"),
        total_amount=group.get("total_amount"),
    )
    await mb.insert()
    return success_response(data={"bill_no": bill_no, "bill_no_formatted": format_bill_no(bill_no)})
