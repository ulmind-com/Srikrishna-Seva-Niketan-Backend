"""Reporting service for the staff-management system.

Produces the four operational reports described in the client's notes:
  1. Firm profit & loss (income, expenses, payslips).
  2. Staff-to-customer distribution.
  3. Customer payment report (paid / pending dues).
  4. Daily summary (counts + vacancies).

All queries are location-scoped via a caller-supplied ``scope`` filter
(``ActorContext.scope_filter()``), so managers only ever see their branches.
Aggregations run server-side as MongoDB pipelines (``$group`` / ``$facet``)
rather than loading whole collections into memory and summing in Python — this
keeps the single async worker's event loop and the 512 MB box responsive even
when every report widget on the admin dashboard fires at once.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional

from bson import ObjectId

from app.models.assignment import Assignment
from app.models.customer import Customer
from app.models.employee import Employee
from app.models.enums import AssignmentStatus, EmployeeStatus, PaymentStatus
from app.models.finance import Expense, Payment
from app.schemas.assignment import _format_shift


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


def _object_ids(ids: Any) -> List[ObjectId]:
    """Convert an iterable of string ids to valid ObjectIds, dropping bad ones."""
    return [ObjectId(x) for x in ids if x and ObjectId.is_valid(x)]


def _iso_date(value: Any) -> Optional[str]:
    """Render a BSON datetime/date ``$max`` result as ``YYYY-MM-DD``."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value)


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

    # ---- Engagements: contract revenue, staff payout, payslips (server-side) ----
    assign_match = _merge(
        scope,
        {"start_date": date_rng} if date_rng else None,
        {"status": {"$ne": AssignmentStatus.CANCELLED.value}},
    )
    assign_agg = await Assignment.aggregate(
        [
            {"$match": assign_match},
            # Days billed per engagement — full span when closed, days elapsed so
            # far when ongoing (so an open duty still contributes accrued value).
            {
                "$addFields": {
                    "_eff_days": {
                        "$cond": [
                            {"$ifNull": ["$end_date", False]},
                            {"$add": [{"$dateDiff": {"startDate": "$start_date", "endDate": "$end_date", "unit": "day"}}, 1]},
                            {"$max": [1, {"$add": [{"$dateDiff": {"startDate": "$start_date", "endDate": "$$NOW", "unit": "day"}}, 1]}]},
                        ]
                    }
                }
            },
            # Prefer the stored total; else derive from the per-day rate × days.
            {
                "$addFields": {
                    "_eff_bill": {
                        "$ifNull": ["$customer_rate", {"$multiply": [{"$ifNull": ["$customer_daily_rate", 0]}, "$_eff_days"]}]
                    },
                    "_eff_payout": {
                        "$ifNull": ["$employee_payout", {"$multiply": [{"$ifNull": ["$employee_daily_rate", 0]}, "$_eff_days"]}]
                    },
                }
            },
            {
                "$facet": {
                    "totals": [
                        {
                            "$group": {
                                "_id": None,
                                "revenue": {"$sum": {"$ifNull": ["$_eff_bill", 0]}},
                                "staff_payout": {"$sum": {"$ifNull": ["$_eff_payout", 0]}},
                                "assignment_other": {"$sum": {"$ifNull": ["$other_expense", 0]}},
                                "count": {"$sum": 1},
                            }
                        }
                    ],
                    "payslips": [
                        {"$match": {"_eff_payout": {"$gt": 0}}},
                        {
                            "$group": {
                                "_id": "$employee_id",
                                "employee_name": {"$first": "$employee_name"},
                                "total_payout": {"$sum": "$_eff_payout"},
                                "engagements": {"$sum": 1},
                            }
                        },
                        {"$sort": {"total_payout": -1}},
                    ],
                }
            },
        ]
    ).to_list()

    facet = assign_agg[0] if assign_agg else {}
    totals = (facet.get("totals") or [{}])[0]
    revenue = totals.get("revenue", 0)
    staff_payout = totals.get("staff_payout", 0)
    assignment_other = totals.get("assignment_other", 0)
    assignment_count = totals.get("count", 0)
    payslips = [
        {
            "employee_id": r["_id"],
            "employee_name": r.get("employee_name"),
            "total_payout": r["total_payout"],
            "engagements": r["engagements"],
        }
        for r in facet.get("payslips", [])
    ]

    # ---- Standalone expenses ledger (rent, utilities, etc.) ----
    exp_match = _merge(scope, {"date": date_rng} if date_rng else None)
    exp_agg = await Expense.aggregate(
        [
            {"$match": exp_match},
            {
                "$facet": {
                    "totals": [
                        {"$group": {"_id": None, "total": {"$sum": "$amount"}, "count": {"$sum": 1}}}
                    ],
                    "by_category": [
                        {"$group": {"_id": "$category", "total": {"$sum": "$amount"}}}
                    ],
                }
            },
        ]
    ).to_list()

    efacet = exp_agg[0] if exp_agg else {}
    etotals = (efacet.get("totals") or [{}])[0]
    general_expense = etotals.get("total", 0)
    expense_count = etotals.get("count", 0)
    expense_by_category: Dict[str, int] = {
        r["_id"]: r["total"] for r in efacet.get("by_category", [])
    }
    if staff_payout:
        expense_by_category["staff payout"] = (
            expense_by_category.get("staff payout", 0) + staff_payout
        )
    if assignment_other:
        expense_by_category["engagement expense"] = (
            expense_by_category.get("engagement expense", 0) + assignment_other
        )

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
        "expense_by_category": expense_by_category,
        "assignment_count": assignment_count,
        "expense_count": expense_count,
        "payslips": payslips,
    }


async def distribution(scope: Dict[str, Any]) -> Dict[str, Any]:
    """Which staff are assigned to which customers, plus free staff."""
    occupying = [AssignmentStatus.SCHEDULED.value, AssignmentStatus.ACTIVE.value]

    # Group occupying engagements per customer, server-side.
    grouped = await Assignment.aggregate(
        [
            {"$match": _merge(scope, {"status": {"$in": occupying}})},
            {
                "$group": {
                    "_id": "$customer_id",
                    "staff": {
                        "$push": {
                            "employee_id": "$employee_id",
                            "employee_name": "$employee_name",
                            "shift": "$shift",
                        }
                    },
                    "employee_ids": {"$addToSet": "$employee_id"},
                }
            },
        ]
    ).to_list()

    assigned_employee_ids: set[str] = set()
    for g in grouped:
        assigned_employee_ids.update(eid for eid in g.get("employee_ids", []) if eid)

    # Fetch only the customers actually referenced (not the whole collection).
    referenced_ids = [g["_id"] for g in grouped if g.get("_id")]
    cust_docs = (
        await Customer.find({"_id": {"$in": _object_ids(referenced_ids)}}).to_list()
        if referenced_ids
        else []
    )
    cust_by_id = {str(c.id): c for c in cust_docs}

    rows = []
    for g in grouped:
        cid = g["_id"]
        c = cust_by_id.get(cid)
        staff = [
            {
                "employee_id": s["employee_id"],
                "employee_name": s["employee_name"],
                "shift": str(s["shift"]),
            }
            for s in g.get("staff", [])
        ]
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

    total_staff = await Employee.find(_merge(scope)).count()
    total_customers = await Customer.find(_merge(scope)).count()

    # Free staff = AVAILABLE employees in scope not currently assigned.
    free_docs = await Employee.find(
        _merge(scope, {"status": EmployeeStatus.AVAILABLE.value})
    ).to_list()
    free_staff = [
        {"employee_id": str(e.id), "name": e.name, "phone": e.phone, "category": e.category}
        for e in free_docs
        if str(e.id) not in assigned_employee_ids
    ]

    return {
        "total_staff": total_staff,
        "total_customers": total_customers,
        "assigned_staff": len(assigned_employee_ids),
        "free_staff_count": len(free_staff),
        "customers": rows,
        "free_staff": free_staff,
    }


async def customer_payments(scope: Dict[str, Any]) -> Dict[str, Any]:
    """Per-customer paid totals and pending dues."""
    # Group payments per customer server-side (paid vs pending, last payment).
    pay_rows = await Payment.aggregate(
        [
            {"$match": _merge(scope)},
            {
                "$group": {
                    "_id": "$customer_id",
                    "customer_name": {"$first": "$customer_name"},
                    "total_paid": {
                        "$sum": {
                            "$cond": [
                                {"$ne": ["$status", PaymentStatus.PENDING.value]},
                                "$amount",
                                0,
                            ]
                        }
                    },
                    "pending": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$status", PaymentStatus.PENDING.value]},
                                "$amount",
                                0,
                            ]
                        }
                    },
                    "payment_count": {"$sum": 1},
                    "last_payment": {"$max": "$date"},
                }
            },
        ]
    ).to_list()

    # Seed every in-scope customer so zero-payment customers still appear.
    customers = await Customer.find(_merge(scope)).to_list()
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

    for r in pay_rows:
        cid = r["_id"]
        row = by_customer.get(cid)
        if row is None:
            row = by_customer.setdefault(
                cid,
                {
                    "customer_id": cid,
                    "customer_name": r.get("customer_name"),
                    "phone": None,
                    "total_paid": 0,
                    "pending": 0,
                    "payment_count": 0,
                    "last_payment": None,
                },
            )
        row["total_paid"] = r["total_paid"]
        row["pending"] = r["pending"]
        row["payment_count"] = r["payment_count"]
        row["last_payment"] = _iso_date(r.get("last_payment"))

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

    # Staff counts per status, server-side.
    status_rows = await Employee.aggregate(
        [{"$match": _merge(scope)}, {"$group": {"_id": "$status", "count": {"$sum": 1}}}]
    ).to_list()
    status_counts: Dict[str, int] = {r["_id"]: r["count"] for r in status_rows if r.get("_id")}
    total_staff = sum(status_counts.values())

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
        "total_staff": total_staff,
        "available_staff": available,
        "assigned_staff": status_counts.get(str(EmployeeStatus.ASSIGNED), 0),
        "on_leave_staff": status_counts.get(str(EmployeeStatus.ON_LEAVE), 0),
        "inactive_staff": status_counts.get(str(EmployeeStatus.INACTIVE), 0),
        "active_customers": active_customers,
        "todays_shifts": todays_shifts,
        "vacancies": available,
        "status_breakdown": status_counts,
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

    # The detail rows must be returned, so this result set is loaded; it is
    # bounded by the period/filter above. Reference data (customers, employees,
    # locations) is fetched only for the ids actually present — not whole
    # collections — via ``$in`` lookups.
    assignments = await Assignment.find(query_filter).sort("-start_date", "-created_at").to_list()

    cust_ids = {a.customer_id for a in assignments if a.customer_id}
    emp_ids = {a.employee_id for a in assignments if a.employee_id}
    loc_ids = {a.location_id for a in assignments if a.location_id}

    cust_docs = (
        await Customer.find({"_id": {"$in": _object_ids(cust_ids)}}).to_list()
        if cust_ids
        else []
    )
    cust_map = {str(c.id): c for c in cust_docs}

    emp_docs = (
        await Employee.find({"_id": {"$in": _object_ids(emp_ids)}}).to_list()
        if emp_ids
        else []
    )
    emp_map = {str(e.id): e for e in emp_docs}

    loc_docs = (
        await Location.find({"_id": {"$in": _object_ids(loc_ids)}}).to_list()
        if loc_ids
        else []
    )
    loc_map = {str(l.id): l.name for l in loc_docs}

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

    today = dt.date.today()
    items = []
    for a in assignments:
        cust = cust_map.get(a.customer_id)
        emp = emp_map.get(a.employee_id)

        # Days billed: the full span for a closed engagement, or days elapsed so
        # far for an ongoing one (so an open duty still shows accrued value).
        if a.end_date:
            eff_days = (a.end_date - a.start_date).days + 1
        else:
            eff_days = max(1, (today - a.start_date).days + 1)

        # Prefer the stored total; for an ongoing duty (no stored total) derive
        # it from the per-day rate × elapsed days so bill/pay are never ₹0.
        bill = a.customer_rate if a.customer_rate is not None else (a.customer_daily_rate or 0) * eff_days
        payout = a.employee_payout if a.employee_payout is not None else (a.employee_daily_rate or 0) * eff_days
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
            "shift_label": _format_shift(a.shift, a.start_time, a.end_time, a.shift_windows),
            "shift_windows": [w.model_dump() for w in a.shift_windows] if a.shift_windows else None,
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
