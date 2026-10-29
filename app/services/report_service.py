"""Reporting service for the staff-management system.

Produces the four operational reports described in the client's notes:
  1. Firm profit & loss (income, expenses, payslips).
  2. Staff-to-customer distribution.
  3. Customer payment report (paid / pending dues).
  4. Daily summary (counts + vacancies).

All queries are location-scoped via a caller-supplied ``scope`` filter
(``ActorContext.scope_filter()``), so managers only ever see their branches.
Aggregations are done in Python over bounded result sets (hundreds of rows),
keeping the code simple and readable.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any, Dict, Optional

from app.models.assignment import Assignment
from app.models.customer import Customer
from app.models.employee import Employee
from app.models.enums import AssignmentStatus, EmployeeStatus, PaymentStatus
from app.models.finance import Expense, Payment


def _date_bounds(
    start: Optional[dt.date], end: Optional[dt.date]
) -> Optional[Dict[str, Any]]:
    """Build a Mongo range filter on a ``date`` field (stored as datetime)."""
    if start is None and end is None:
        return None
    rng: Dict[str, Any] = {}
    if start is not None:
        rng["$gte"] = dt.datetime.combine(start, dt.time.min)
    if end is not None:
        rng["$lte"] = dt.datetime.combine(end, dt.time.max)
    return rng


def _merge(*filters: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for f in filters:
        if f:
            out.update(f)
    return out


async def profit_loss(
    scope: Dict[str, Any],
    start: Optional[dt.date] = None,
    end: Optional[dt.date] = None,
) -> Dict[str, Any]:
    """Assignment-driven P&L.

    Revenue is the contract value (``customer_rate``) of each engagement; the
    direct costs are the staff payout and any other engagement expense entered
    at assignment time, plus standalone expenses from the Expenses ledger.
    Profit = revenue − all of those. Everything is filtered to the period
    (assignments by ``start_date``, standalone expenses by ``date``).
    """
    date_rng = _date_bounds(start, end)

    # ---- Engagements: contract revenue, staff payout, other engagement cost ----
    assign_filter = _merge(
        scope, {"start_date": date_rng} if date_rng else None
    )
    assignments = await Assignment.find(assign_filter).to_list()

    revenue = 0
    staff_payout = 0
    assignment_other = 0
    payslips: Dict[str, Dict[str, Any]] = {}
    for a in assignments:
        if a.status == AssignmentStatus.CANCELLED:
            continue
        revenue += a.customer_rate or 0
        staff_payout += a.employee_payout or 0
        assignment_other += a.other_expense or 0
        if a.employee_payout:
            slip = payslips.setdefault(
                a.employee_id,
                {
                    "employee_id": a.employee_id,
                    "employee_name": a.employee_name,
                    "total_payout": 0,
                    "engagements": 0,
                },
            )
            slip["total_payout"] += a.employee_payout
            slip["engagements"] += 1

    # ---- Standalone expenses ledger (rent, utilities, etc.) ----
    exp_filter = _merge(scope, {"date": date_rng} if date_rng else None)
    expenses = await Expense.find(exp_filter).to_list()
    general_expense = sum(e.amount for e in expenses)
    expense_by_category: Dict[str, int] = defaultdict(int)
    for e in expenses:
        expense_by_category[e.category] += e.amount
    if staff_payout:
        expense_by_category["staff payout"] += staff_payout
    if assignment_other:
        expense_by_category["engagement expense"] += assignment_other

    total_expense = staff_payout + assignment_other + general_expense
    profit = revenue - total_expense

    return {
        "income": revenue,  # alias kept for existing clients
        "revenue": revenue,
        "staff_payout": staff_payout,
        "assignment_expense": assignment_other,
        "general_expense": general_expense,
        "expense": total_expense,
        "profit": profit,
        "margin_pct": round(profit / revenue * 100) if revenue else 0,
        "expense_by_category": dict(expense_by_category),
        "assignment_count": sum(
            1 for a in assignments if a.status != AssignmentStatus.CANCELLED
        ),
        "expense_count": len(expenses),
        "payslips": sorted(
            payslips.values(), key=lambda s: s["total_payout"], reverse=True
        ),
    }


async def distribution(scope: Dict[str, Any]) -> Dict[str, Any]:
    """Which staff are assigned to which customers, plus free staff."""
    occupying = {AssignmentStatus.SCHEDULED, AssignmentStatus.ACTIVE}
    assignments = await Assignment.find(_merge(scope)).to_list()
    customers = await Customer.find(_merge(scope)).to_list()
    employees = await Employee.find(_merge(scope)).to_list()

    cust_by_id = {str(c.id): c for c in customers}
    staff_by_customer: Dict[str, list] = defaultdict(list)
    assigned_employee_ids: set[str] = set()
    for a in assignments:
        if a.status not in occupying:
            continue
        assigned_employee_ids.add(a.employee_id)
        staff_by_customer[a.customer_id].append(
            {"employee_id": a.employee_id, "employee_name": a.employee_name, "shift": str(a.shift)}
        )

    rows = []
    for cid, staff in staff_by_customer.items():
        c = cust_by_id.get(cid)
        rows.append(
            {
                "customer_id": cid,
                "customer_name": c.name if c else "(removed)",
                "phone": c.phone if c else None,
                "address": c.address if c else None,
                "staff_count": len(staff),
                "staff": staff,
            }
        )
    rows.sort(key=lambda r: r["customer_name"].lower())

    free_staff = [
        {"employee_id": str(e.id), "name": e.name, "phone": e.phone, "category": e.category}
        for e in employees
        if str(e.id) not in assigned_employee_ids and e.status == EmployeeStatus.AVAILABLE
    ]

    return {
        "total_staff": len(employees),
        "total_customers": len(customers),
        "assigned_staff": len(assigned_employee_ids),
        "free_staff_count": len(free_staff),
        "customers": rows,
        "free_staff": free_staff,
    }


async def customer_payments(scope: Dict[str, Any]) -> Dict[str, Any]:
    """Per-customer paid totals and pending dues."""
    customers = await Customer.find(_merge(scope)).to_list()
    payments = await Payment.find(_merge(scope)).to_list()

    by_customer: Dict[str, Dict[str, Any]] = {}
    for c in customers:
        by_customer[str(c.id)] = {
            "customer_id": str(c.id),
            "customer_name": c.name,
            "phone": c.phone,
            "total_paid": 0,
            "pending": 0,
            "payment_count": 0,
            "last_payment": None,
        }
    for p in payments:
        row = by_customer.get(p.customer_id)
        if row is None:
            row = by_customer.setdefault(
                p.customer_id,
                {"customer_id": p.customer_id, "customer_name": p.customer_name,
                 "phone": None, "total_paid": 0, "pending": 0, "payment_count": 0, "last_payment": None},
            )
        row["payment_count"] += 1
        if p.status == PaymentStatus.PENDING:
            row["pending"] += p.amount
        else:
            row["total_paid"] += p.amount
        pdate = p.date.isoformat() if p.date else None
        if pdate and (row["last_payment"] is None or pdate > row["last_payment"]):
            row["last_payment"] = pdate

    rows = sorted(by_customer.values(), key=lambda r: r["pending"], reverse=True)
    return {
        "total_paid": sum(r["total_paid"] for r in rows),
        "total_pending": sum(r["pending"] for r in rows),
        "customers": rows,
    }


async def daily_summary(scope: Dict[str, Any], on: Optional[dt.date] = None) -> Dict[str, Any]:
    """Counts + vacancies for a given day (defaults to today)."""
    day = on or dt.date.today()
    day_start = dt.datetime.combine(day, dt.time.min)
    day_end = dt.datetime.combine(day, dt.time.max)

    employees = await Employee.find(_merge(scope)).to_list()
    status_counts: Dict[str, int] = defaultdict(int)
    for e in employees:
        status_counts[str(e.status)] += 1

    active_customers = await Customer.find(_merge(scope, {"is_active": True})).count()

    # Assignments active on the day.
    active_filter = _merge(
        scope,
        {
            "start_date": {"$lte": day_end},
            "$or": [{"end_date": None}, {"end_date": {"$gte": day_start}}],
            "status": {"$in": [AssignmentStatus.SCHEDULED, AssignmentStatus.ACTIVE]},
        },
    )
    todays_shifts = await Assignment.find(active_filter).count()

    available = status_counts.get(str(EmployeeStatus.AVAILABLE), 0)
    return {
        "date": day.isoformat(),
        "total_staff": len(employees),
        "available_staff": available,
        "assigned_staff": status_counts.get(str(EmployeeStatus.ASSIGNED), 0),
        "on_leave_staff": status_counts.get(str(EmployeeStatus.ON_LEAVE), 0),
        "inactive_staff": status_counts.get(str(EmployeeStatus.INACTIVE), 0),
        "active_customers": active_customers,
        "todays_shifts": todays_shifts,
        "vacancies": available,
        "status_breakdown": dict(status_counts),
    }


async def duty_report(
    scope: Dict[str, Any],
    start: Optional[dt.date] = None,
    end: Optional[dt.date] = None,
    payment_type: Optional[str] = None,
    status: Optional[str] = None,
) -> Dict[str, Any]:
    """Comprehensive duty & financial report.

    Details:
      - Which client took service on which date and duration
      - Who (nurse / staff member) delivered the service
      - Payment type: prepaid vs postpaid
      - Client billing amount, staff payout, expenses, and net company profit
      - Work completion & payment collection tracking
    """
    from app.models.location import Location

    date_rng = _date_bounds(start, end)
    query_filter: Dict[str, Any] = _merge(
        scope, {"start_date": date_rng} if date_rng else None
    )
    if payment_type and payment_type.lower() != "all":
        query_filter["payment_type"] = payment_type.lower()
    if status and status.lower() != "all":
        query_filter["status"] = status.lower()

    assignments = await Assignment.find(query_filter).sort("-start_date", "-created_at").to_list()

    customers = await Customer.find(_merge(scope)).to_list()
    cust_map = {str(c.id): c for c in customers}

    employees = await Employee.find(_merge(scope)).to_list()
    emp_map = {str(e.id): e for e in employees}

    locations = await Location.find({"is_active": True}).to_list()
    loc_map = {str(l.id): l.name for l in locations}

    total_billed = 0
    total_staff_payout = 0
    total_expenses = 0
    total_profit = 0

    prepaid_count = 0
    prepaid_total = 0
    postpaid_count = 0
    postpaid_total = 0
    postpaid_collected = 0
    postpaid_pending = 0

    items = []
    for a in assignments:
        cust = cust_map.get(a.customer_id)
        emp = emp_map.get(a.employee_id)

        bill = a.customer_rate or 0
        payout = a.employee_payout or 0
        exp = a.other_expense or 0
        profit = bill - payout - exp
        margin = round(profit / bill * 100) if bill > 0 else 0

        ptype = str(getattr(a, "payment_type", "prepaid"))
        pstatus = str(getattr(a, "payment_status", "pending"))

        if a.status != AssignmentStatus.CANCELLED:
            total_billed += bill
            total_staff_payout += payout
            total_expenses += exp
            total_profit += profit

            if ptype == "postpaid":
                postpaid_count += 1
                postpaid_total += bill
                if pstatus in ("paid", "received"):
                    postpaid_collected += bill
                else:
                    postpaid_pending += bill
            else:
                prepaid_count += 1
                prepaid_total += bill

        duration_days = (
            (a.end_date - a.start_date).days + 1
            if a.end_date
            else None
        )

        items.append({
            "id": str(a.id),
            "start_date": a.start_date.isoformat(),
            "end_date": a.end_date.isoformat() if a.end_date else None,
            "duration_days": duration_days,
            "customer_id": a.customer_id,
            "customer_name": a.customer_name,
            "customer_phone": cust.phone if cust else None,
            "employee_id": a.employee_id,
            "employee_name": a.employee_name,
            "employee_code": emp.code if emp else None,
            "shift": str(a.shift),
            "place_type": str(a.place_type),
            "place_name": a.place_name,
            "address": a.address or (cust.address if cust else None),
            "payment_type": ptype,
            "payment_status": pstatus,
            "customer_rate": bill,
            "employee_payout": payout,
            "other_expense": exp,
            "profit": profit,
            "margin_pct": margin,
            "status": str(a.status),
            "acceptance_status": str(a.acceptance_status),
            "work_completed_at": a.work_completed_at.isoformat() if a.work_completed_at else None,
            "payment_received_at": a.payment_received_at.isoformat() if a.payment_received_at else None,
            "payment_received_by": a.payment_received_by,
            "location_id": a.location_id,
            "location_name": loc_map.get(a.location_id, "Branch"),
        })

    return {
        "summary": {
            "total_duties": len(items),
            "total_billed": total_billed,
            "total_staff_payout": total_staff_payout,
            "total_expenses": total_expenses,
            "total_profit": total_profit,
            "overall_margin_pct": round(total_profit / total_billed * 100) if total_billed > 0 else 0,
            "prepaid_count": prepaid_count,
            "prepaid_total": prepaid_total,
            "postpaid_count": postpaid_count,
            "postpaid_total": postpaid_total,
            "postpaid_collected": postpaid_collected,
            "postpaid_pending": postpaid_pending,
        },
        "items": items,
    }
