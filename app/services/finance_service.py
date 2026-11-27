"""Unified finance ledger service — Client Receipts, Partial Payments, Staff Salary Payouts & History.

Strictly enforces accounting separation:
- Billed / Revenue (Invoice Generated) comes from Assignments / Billing (`compute_assignment_financials`).
- Cash Received comes from Client `Payment` receipt entries (`receipt_no`: RCP-001...).
- Accounts Receivable (Outstanding) = Billed - Received (never negative).
- Staff Salary Due comes from Attendance / Payslips (`payroll_service`).
- Staff Paid comes from `StaffPayment` payout vouchers (`voucher_no`: PAY-001...).
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional

from bson import ObjectId

from app.models.assignment import Assignment
from app.models.customer import Customer
from app.models.employee import Employee
from app.models.enums import AssignmentStatus, PaymentStatus, StaffVerificationStatus
from app.models.finance import Expense, Payment, StaffPayment
from app.models.location import Location
from app.schemas.assignment import compute_assignment_financials
from app.utils.bill_number import format_bill_no
from app.utils.ist_time import today_ist


def _oids(ids: Any) -> List[ObjectId]:
    return [ObjectId(x) for x in ids if x and ObjectId.is_valid(x)]


def _date_bounds(
    start: Optional[dt.date], end: Optional[dt.date]
) -> Optional[Dict[str, Any]]:
    if start is None and end is None:
        return None
    rng: Dict[str, Any] = {}
    if start is not None:
        rng["$gte"] = dt.datetime.combine(start, dt.time.min)
    if end is not None:
        rng["$lte"] = dt.datetime.combine(end, dt.time.max)
    return rng


def _iso_date(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v.date().isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    return str(v)[:10]


async def sync_assignment_payment_status(assignment_id: str) -> None:
    """Recompute Assignment.payment_status (pending / partial / paid) from recorded Payments."""
    if not assignment_id or not ObjectId.is_valid(assignment_id):
        return
    asg = await Assignment.get(assignment_id)
    if asg is None:
        return

    _, bill, _, _, _ = compute_assignment_financials(asg, today_ist())
    rows = await Payment.find(
        {"assignment_id": assignment_id, "status": {"$ne": PaymentStatus.PENDING.value}}
    ).to_list()

    total_paid = sum(r.amount for r in rows)
    latest = max(rows, key=lambda r: (r.date, r.created_at)) if rows else None

    if total_paid <= 0:
        asg.payment_status = PaymentStatus.PENDING
        asg.payment_received_at = None
        asg.payment_received_by = None
    elif bill > 0 and total_paid >= bill:
        asg.payment_status = PaymentStatus.PAID
        asg.payment_received_at = dt.datetime.combine(latest.date, dt.time(12, 0), tzinfo=dt.timezone.utc) if latest else asg.payment_received_at
        asg.payment_received_by = (latest.received_by if latest else None) or "Admin"
    else:
        asg.payment_status = PaymentStatus.PARTIAL
        asg.payment_received_at = dt.datetime.combine(latest.date, dt.time(12, 0), tzinfo=dt.timezone.utc) if latest else asg.payment_received_at
        asg.payment_received_by = (latest.received_by if latest else None) or "Admin"

    asg.touch()
    await asg.save()


async def compute_client_ledger(
    scope: Dict[str, Any],
    start: Optional[dt.date] = None,
    end: Optional[dt.date] = None,
    customer_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Client-wise & invoice-wise Billed vs Actual Received vs Outstanding ledger."""
    date_rng = _date_bounds(start, end)
    asg_filter: Dict[str, Any] = {
        **(scope or {}),
        "status": {"$ne": AssignmentStatus.CANCELLED.value},
    }
    if date_rng:
        asg_filter["start_date"] = date_rng
    if customer_id:
        asg_filter["customer_id"] = customer_id

    assignments = await Assignment.find(asg_filter).sort("-start_date").to_list()

    pay_filter: Dict[str, Any] = {**(scope or {})}
    if customer_id:
        pay_filter["customer_id"] = customer_id
    all_payments = await Payment.find(pay_filter).sort("-date", "-created_at").to_list()
    settled_payments = [p for p in all_payments if p.status != PaymentStatus.PENDING]

    cust_filter: Dict[str, Any] = {**(scope or {})}
    if customer_id:
        if ObjectId.is_valid(customer_id):
            cust_filter["_id"] = ObjectId(customer_id)
    customers = await Customer.find(cust_filter).to_list()
    loc_map = {str(l.id): l.name for l in await Location.find({}).to_list()}

    # Group assignment-linked payments across all dates so an invoice billed in November
    # and paid in December reflects accurately on that November invoice.
    pay_by_asg: Dict[str, List[Payment]] = {}
    unlinked_by_cust: Dict[str, List[Payment]] = {}
    for p in settled_payments:
        if p.assignment_id:
            pay_by_asg.setdefault(p.assignment_id, []).append(p)
        else:
            if start or end:
                if start and p.date < start:
                    continue
                if end and p.date > end:
                    continue
            unlinked_by_cust.setdefault(p.customer_id, []).append(p)

    today = today_ist()
    by_customer: Dict[str, Dict[str, Any]] = {}
    for c in customers:
        cid = str(c.id)
        by_customer[cid] = {
            "customer_id": cid,
            "customer_name": c.name,
            "phone": c.phone,
            "location_id": c.location_id,
            "location_name": loc_map.get(c.location_id or "", "Branch"),
            "invoiced_amount": 0,
            "received_amount": 0,
            "outstanding_amount": 0,
            "bill_no": None,
            "bill_no_formatted": None,
            "payment_count": 0,
            "last_payment_date": None,
            "status": "no_bill",
            "invoices": [],
            "receipts": [],
        }

    def _ensure_cust(cid: str, cname: str, loc_id: Optional[str]) -> Dict[str, Any]:
        row = by_customer.get(cid)
        if row is None:
            row = {
                "customer_id": cid,
                "customer_name": cname or "(customer)",
                "phone": None,
                "location_id": loc_id,
                "location_name": loc_map.get(loc_id or "", "Branch"),
                "invoiced_amount": 0,
                "received_amount": 0,
                "outstanding_amount": 0,
                "bill_no": None,
                "bill_no_formatted": None,
                "payment_count": 0,
                "last_payment_date": None,
                "status": "no_bill",
                "invoices": [],
                "receipts": [],
            }
            by_customer[cid] = row
        return row

    for a in assignments:
        eff_days, bill, _, _, is_ended = compute_assignment_financials(a, today)
        if not is_ended or bill <= 0:
            continue
        aid = str(a.id)
        row = _ensure_cust(a.customer_id, a.customer_name, a.location_id)
        linked_pays = pay_by_asg.get(aid, [])
        paid_for_asg = sum(p.amount for p in linked_pays)
        # If no Payment record exists yet, check assignment-level legacy settlement only when explicitly PAID via legacy UI?
        # Accounting rule: Actual Cash Received tracks recorded Payment entries so Partial vs Pending vs Paid stay exact.
        outstanding_for_asg = max(0, bill - paid_for_asg)
        inv_status = (
            "paid"
            if paid_for_asg >= bill
            else ("partial" if paid_for_asg > 0 else "pending")
        )
        if a.bill_no is not None and row["bill_no"] is None:
            row["bill_no"] = a.bill_no
            row["bill_no_formatted"] = format_bill_no(a.bill_no)

        row["invoiced_amount"] += bill
        row["received_amount"] += paid_for_asg
        row["payment_count"] += len(linked_pays)
        for p in linked_pays:
            p_iso = _iso_date(p.date)
            if p_iso and (not row["last_payment_date"] or p_iso > row["last_payment_date"]):
                row["last_payment_date"] = p_iso
            row["receipts"].append(
                {
                    "id": str(p.id),
                    "receipt_no": p.receipt_no,
                    "assignment_id": p.assignment_id,
                    "bill_no": p.bill_no or a.bill_no,
                    "bill_no_formatted": format_bill_no(p.bill_no or a.bill_no),
                    "amount": p.amount,
                    "date": p_iso,
                    "mode": str(p.mode),
                    "transaction_id": p.transaction_id,
                    "note": p.note,
                }
            )

        row["invoices"].append(
            {
                "assignment_id": aid,
                "bill_no": a.bill_no,
                "bill_no_formatted": format_bill_no(a.bill_no),
                "employee_name": a.employee_name,
                "start_date": a.start_date.isoformat(),
                "end_date": a.end_date.isoformat() if a.end_date else None,
                "days_worked": eff_days,
                "invoiced_amount": bill,
                "received_amount": paid_for_asg,
                "outstanding_amount": outstanding_for_asg,
                "status": inv_status,
            }
        )

    # Apply unlinked client payments within [start, end]
    for cid, pays in unlinked_by_cust.items():
        if not pays:
            continue
        row = _ensure_cust(cid, pays[0].customer_name, pays[0].location_id)
        for p in pays:
            row["received_amount"] += p.amount
            row["payment_count"] += 1
            p_iso = _iso_date(p.date)
            if p_iso and (not row["last_payment_date"] or p_iso > row["last_payment_date"]):
                row["last_payment_date"] = p_iso
            row["receipts"].append(
                {
                    "id": str(p.id),
                    "receipt_no": p.receipt_no,
                    "assignment_id": None,
                    "bill_no": p.bill_no,
                    "bill_no_formatted": format_bill_no(p.bill_no),
                    "amount": p.amount,
                    "date": p_iso,
                    "mode": str(p.mode),
                    "transaction_id": p.transaction_id,
                    "note": p.note,
                }
            )

    # Finalize outstanding & status per customer
    client_rows: List[Dict[str, Any]] = []
    for row in by_customer.values():
        inv = row["invoiced_amount"]
        rec = row["received_amount"]
        out = max(0, inv - rec)
        row["outstanding_amount"] = out
        if inv > 0:
            if out == 0:
                row["status"] = "paid"
            elif rec > 0:
                row["status"] = "partial"
            else:
                row["status"] = "pending"
        elif rec > 0:
            row["status"] = "advance"
        else:
            row["status"] = "no_bill"
        row["receipts"].sort(key=lambda r: r.get("date") or "", reverse=True)
        if inv > 0 or rec > 0:
            client_rows.append(row)

    client_rows.sort(key=lambda r: (r["outstanding_amount"], r["invoiced_amount"]), reverse=True)

    total_billed = sum(r["invoiced_amount"] for r in client_rows)
    total_received = sum(r["received_amount"] for r in client_rows)
    total_outstanding = max(0, total_billed - total_received)
    collection_rate = round((total_received / total_billed) * 100, 2) if total_billed > 0 else 0.0

    return {
        "summary": {
            "total_billed": total_billed,
            "total_received": total_received,
            "total_outstanding": total_outstanding,
            "collection_rate_pct": collection_rate,
            "client_count": len(client_rows),
            "paid_clients": sum(1 for r in client_rows if r["status"] == "paid"),
            "partial_clients": sum(1 for r in client_rows if r["status"] == "partial"),
            "unpaid_clients": sum(1 for r in client_rows if r["status"] == "pending"),
        },
        "clients": client_rows,
    }


async def compute_staff_ledger(
    scope: Dict[str, Any],
    month: Optional[str] = None,
    start: Optional[dt.date] = None,
    end: Optional[dt.date] = None,
    location_id: Optional[str] = None,
    employee_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Staff-wise Salary Due vs Actual Paid vs Remaining balance (supports partial payouts & date ranges)."""
    from app.services import payroll_service

    loc_ids: Optional[List[str]] = None
    if location_id:
        loc_ids = [location_id]
    elif scope and "location_id" in scope:
        v = scope["location_id"]
        loc_ids = v.get("$in") if isinstance(v, dict) else [v]

    has_custom_range = bool(start and end)
    if start and end:
        first, last = start, end
        days = (end - start).days + 1
        eff_month = f"{first.year:04d}-{first.month:02d}"
    else:
        first, last, days = payroll_service.parse_month(month)
        eff_month = f"{first.year:04d}-{first.month:02d}"

    await payroll_service.materialize_from_assignments(first, last, loc_ids)
    run_data = await payroll_service.payroll_run(
        first, last, days, loc_ids, is_custom_range=has_custom_range
    )
    payroll_rows = run_data.get("rows", [])

    # Fetch StaffPayment payouts for this month or exact bank-statement date window
    sp_filter: Dict[str, Any] = {**(scope or {})}
    if location_id:
        sp_filter["location_id"] = location_id
    if employee_id:
        sp_filter["employee_id"] = employee_id
    all_sp = await StaffPayment.find(sp_filter).sort("-date", "-created_at").to_list()

    sp_by_emp: Dict[str, List[StaffPayment]] = {}
    for sp in all_sp:
        sp_date = sp.date.date() if isinstance(sp.date, dt.datetime) else sp.date
        matches_range = first <= sp_date <= last
        if has_custom_range:
            if matches_range:
                sp_by_emp.setdefault(sp.employee_id, []).append(sp)
        else:
            matches_month = (sp.month == eff_month) if sp.month else False
            if matches_month or (not sp.month and matches_range):
                sp_by_emp.setdefault(sp.employee_id, []).append(sp)

    # Ensure any employee who received payouts in this period (even if inactive/unlisted) is included
    payroll_by_emp = {r["employee_id"]: r for r in payroll_rows}
    missing_emp_ids = [eid for eid in sp_by_emp if eid not in payroll_by_emp]
    if missing_emp_ids:
        extra_emps = await Employee.find({"_id": {"$in": _oids(missing_emp_ids)}}).to_list()
        extra_map = {str(e.id): e for e in extra_emps}
        for eid in missing_emp_ids:
            emp_doc = extra_map.get(eid)
            first_sp = sp_by_emp[eid][0]
            payroll_rows.append(
                {
                    "employee_id": eid,
                    "name": emp_doc.name if emp_doc else first_sp.employee_name,
                    "code": emp_doc.code if emp_doc else "",
                    "category": emp_doc.category if emp_doc else "",
                    "location_id": emp_doc.location_id if emp_doc else first_sp.location_id,
                    "daily_rate": emp_doc.daily_rate if emp_doc else None,
                    "present_days": 0,
                    "half_days": 0,
                    "payable_days": 0,
                    "gross_pay": 0,
                }
            )

    if employee_id:
        payroll_rows = [r for r in payroll_rows if r["employee_id"] == employee_id]

    staff_rows: List[Dict[str, Any]] = []
    for r in payroll_rows:
        eid = r["employee_id"]
        due = int(r.get("gross_pay") or 0)
        payouts = sp_by_emp.get(eid, [])
        paid = sum(p.amount for p in payouts)
        remaining = max(0, due - paid)
        if due == 0 and paid == 0:
            st = "no_due"
        elif paid > 0 and due == 0:
            st = "advance"
        elif remaining == 0 and due > 0:
            st = "paid"
        elif paid > 0:
            st = "partial"
        else:
            st = "pending"

        last_date = _iso_date(payouts[0].date) if payouts else None
        staff_rows.append(
            {
                "employee_id": eid,
                "employee_name": r["name"],
                "code": r["code"],
                "category": r["category"],
                "location_id": r["location_id"],
                "daily_rate": r["daily_rate"],
                "worked_days": r.get("present_days", 0) + r.get("half_days", 0),
                "payable_days": r.get("payable_days", 0),
                "salary_due": due,
                "paid_amount": paid,
                "remaining_amount": remaining,
                "status": st,
                "payment_count": len(payouts),
                "last_payment_date": last_date,
                "payments": [
                    {
                        "id": str(p.id),
                        "voucher_no": p.voucher_no,
                        "amount": p.amount,
                        "date": _iso_date(p.date),
                        "month": p.month or eff_month,
                        "mode": str(p.mode),
                        "transaction_id": p.transaction_id,
                        "paid_by": p.paid_by,
                        "note": p.note,
                    }
                    for p in payouts
                ],
            }
        )

    staff_rows.sort(key=lambda x: (x["remaining_amount"], x["salary_due"]), reverse=True)
    total_due = sum(x["salary_due"] for x in staff_rows)
    total_paid = sum(x["paid_amount"] for x in staff_rows)
    total_remaining = max(0, total_due - total_paid)

    return {
        "month": eff_month,
        "period": {"start": first.isoformat(), "end": last.isoformat()},
        "summary": {
            "total_salary_due": total_due,
            "total_paid": total_paid,
            "total_remaining": total_remaining,
            "staff_count": len(staff_rows),
            "paid_staff": sum(1 for x in staff_rows if x["status"] == "paid"),
            "partial_staff": sum(1 for x in staff_rows if x["status"] == "partial"),
            "unpaid_staff": sum(1 for x in staff_rows if x["status"] == "pending"),
        },
        "staff": staff_rows,
    }


async def compute_staff_statement(
    employee: Employee,
    start: Optional[dt.date] = None,
    end: Optional[dt.date] = None,
    month: Optional[str] = None,
) -> Dict[str, Any]:
    """Bank-statement style ledger for one staff member over any [start, end] date range.

    Combines day-by-day duty earnings (credit) and salary payout vouchers (debit) into a
    chronological passbook with running balance, plus full voucher & duty breakdowns.
    """
    from app.services import payroll_service

    if start and end:
        first, last = start, end
        days = (end - start).days + 1
    else:
        first, last, days = payroll_service.parse_month(month)
    eff_month = f"{first.year:04d}-{first.month:02d}"

    emp_id = str(employee.id)
    loc_ids = [employee.location_id] if employee.location_id else None
    await payroll_service.materialize_from_assignments(first, last, loc_ids)
    slip = await payroll_service.build_payslip(
        employee, first, last, days, is_custom_range=bool(start and end)
    )

    loc = await Location.get(employee.location_id) if employee.location_id and ObjectId.is_valid(employee.location_id) else None
    location_name = loc.name if loc else "Branch"

    # Strict date-window payouts sorted chronologically (oldest -> newest) for bank-statement passbook
    all_sp = await StaffPayment.find({"employee_id": emp_id}).sort("date", "created_at").to_list()
    period_sp: List[StaffPayment] = []
    for sp in all_sp:
        sp_date = sp.date.date() if isinstance(sp.date, dt.datetime) else sp.date
        if first <= sp_date <= last:
            period_sp.append(sp)

    slip_summary = slip.get("summary", {})
    salary_due = int(slip_summary.get("net_payable") or 0)
    total_paid = sum(sp.amount for sp in period_sp)
    remaining_amount = max(0, salary_due - total_paid)
    advance_amount = max(0, total_paid - salary_due)

    if salary_due == 0 and total_paid == 0:
        st = "no_due"
    elif total_paid > 0 and salary_due == 0:
        st = "advance"
    elif remaining_amount == 0 and salary_due > 0:
        st = "paid"
    elif total_paid > 0:
        st = "partial"
    else:
        st = "pending"

    # Build chronological voucher list with running paid & balance after each payment
    payments_out: List[Dict[str, Any]] = []
    running_paid = 0
    for sp in period_sp:
        running_paid += sp.amount
        payments_out.append(
            {
                "id": str(sp.id),
                "voucher_no": sp.voucher_no or f"PAY-{str(sp.id)[-5:].upper()}",
                "amount": sp.amount,
                "date": _iso_date(sp.date),
                "month": sp.month or eff_month,
                "mode": str(sp.mode),
                "transaction_id": sp.transaction_id,
                "paid_by": sp.paid_by,
                "note": sp.note,
                "running_paid": running_paid,
                "balance_after": salary_due - running_paid,
            }
        )

    # Filter non-unmarked attendance days for duty breakdown & passbook credit entries
    shift_names = {
        "morning": "Morning Shift",
        "day": "Day Shift",
        "night": "Night Shift",
        "full_24h": "24 Hours",
        "custom": "Custom Shift",
    }
    status_names = {
        "present": "Present",
        "half_day": "Half Day",
        "off": "Off / Leave",
        "leave": "Off / Leave",
        "absent": "Absent",
    }

    daily_entries: List[Dict[str, Any]] = []
    passbook_events: List[Dict[str, Any]] = []

    for d in slip.get("days", []):
        if d.get("status") == "unmarked":
            continue
        daily_entries.append(d)
        amt = int(d.get("amount") or 0)
        if amt <= 0 and d.get("status") not in ("absent", "off", "leave"):
            continue
        sh = shift_names.get(d.get("shift") or "", d.get("shift") or "Duty")
        st_lbl = status_names.get(d.get("status") or "", d.get("status") or "")
        cust = d.get("customer_name")
        part = f"{cust} ({st_lbl})" if cust else f"Duty ({st_lbl})"
        passbook_events.append(
            {
                "sort_order": 0,
                "date": d["date"],
                "entry_type": "earning",
                "ref_no": sh,
                "particulars": part,
                "customer_name": cust,
                "mode": None,
                "transaction_id": None,
                "paid_by": None,
                "note": d.get("duration") or None,
                "voucher_id": None,
                "credit": amt,
                "debit": 0,
            }
        )

    for p in payments_out:
        mode_lbl = str(p["mode"]).replace("_", " ").upper()
        ref_str = f" · {p['transaction_id']}" if p.get("transaction_id") else ""
        note_str = f" — {p['note']}" if p.get("note") else ""
        passbook_events.append(
            {
                "sort_order": 1,
                "date": p["date"] or "",
                "entry_type": "payout",
                "ref_no": p["voucher_no"],
                "particulars": f"Salary Paid ({mode_lbl}{ref_str}){note_str}",
                "customer_name": None,
                "mode": p["mode"],
                "transaction_id": p.get("transaction_id"),
                "paid_by": p.get("paid_by"),
                "note": p.get("note"),
                "voucher_id": p["id"],
                "credit": 0,
                "debit": int(p["amount"]),
            }
        )

    passbook_events.sort(key=lambda e: (e["date"], e["sort_order"]))
    bal = 0
    passbook: List[Dict[str, Any]] = []
    for ev in passbook_events:
        bal += ev["credit"] - ev["debit"]
        passbook.append(
            {
                "date": ev["date"],
                "entry_type": ev["entry_type"],
                "ref_no": ev["ref_no"],
                "particulars": ev["particulars"],
                "customer_name": ev["customer_name"],
                "mode": ev["mode"],
                "transaction_id": ev["transaction_id"],
                "paid_by": ev["paid_by"],
                "note": ev["note"],
                "voucher_id": ev["voucher_id"],
                "credit": ev["credit"],
                "debit": ev["debit"],
                "running_balance": bal,
            }
        )

    return {
        "employee": {
            "id": emp_id,
            "name": employee.name,
            "code": employee.code,
            "category": employee.category,
            "phone": getattr(employee, "phone", None),
            "daily_rate": slip.get("employee", {}).get("daily_rate") or employee.daily_rate,
            "location_id": employee.location_id,
            "location_name": location_name,
        },
        "period": {
            "month": eff_month,
            "start": first.isoformat(),
            "end": last.isoformat(),
            "days": days,
        },
        "summary": {
            "worked_days": slip_summary.get("worked_days", 0),
            "present_days": slip_summary.get("present_days", 0),
            "half_days": slip_summary.get("half_days", 0),
            "off_days": (slip_summary.get("off_days", 0) or 0) + (slip_summary.get("leave_days", 0) or 0),
            "absent_days": slip_summary.get("absent_days", 0),
            "payable_days": slip_summary.get("payable_days", 0),
            "salary_due": salary_due,
            "paid_amount": total_paid,
            "remaining_amount": remaining_amount,
            "advance_amount": advance_amount,
            "status": st,
            "payment_count": len(payments_out),
        },
        "daily_entries": daily_entries,
        "payments": payments_out,
        "passbook": passbook,
    }


async def get_payment_history(
    scope: Dict[str, Any],
    start: Optional[dt.date] = None,
    end: Optional[dt.date] = None,
    direction: str = "all",
    mode: Optional[str] = None,
    search: Optional[str] = None,
) -> Dict[str, Any]:
    """Chronological Incoming (Client Receipts) & Outgoing (Staff Salaries + Expenses) ledger."""
    date_rng = _date_bounds(start, end)
    base_filter: Dict[str, Any] = {**(scope or {})}
    if date_rng:
        base_filter["date"] = date_rng

    transactions: List[Dict[str, Any]] = []

    if direction in ("all", "incoming"):
        pay_q = dict(base_filter)
        pay_q["status"] = {"$ne": PaymentStatus.PENDING.value}
        if mode and mode != "all":
            pay_q["mode"] = mode
        for p in await Payment.find(pay_q).to_list():
            transactions.append(
                {
                    "id": str(p.id),
                    "kind": "client_receipt",
                    "direction": "incoming",
                    "ref_no": p.receipt_no or f"#{str(p.id)[-6:].upper()}",
                    "party_id": p.customer_id,
                    "party_name": p.customer_name,
                    "bill_no": p.bill_no,
                    "bill_no_formatted": format_bill_no(p.bill_no),
                    "assignment_id": p.assignment_id,
                    "amount": p.amount,
                    "date": _iso_date(p.date),
                    "mode": str(p.mode),
                    "transaction_id": p.transaction_id,
                    "note": p.note,
                    "location_id": p.location_id,
                    "created_at": p.created_at.isoformat() if p.created_at else None,
                }
            )

    if direction in ("all", "outgoing", "staff"):
        sp_q = dict(base_filter)
        if mode and mode != "all":
            sp_q["mode"] = mode
        for sp in await StaffPayment.find(sp_q).to_list():
            transactions.append(
                {
                    "id": str(sp.id),
                    "kind": "staff_salary",
                    "direction": "outgoing",
                    "ref_no": sp.voucher_no or f"PAY-{str(sp.id)[-5:].upper()}",
                    "party_id": sp.employee_id,
                    "party_name": sp.employee_name,
                    "month": sp.month,
                    "amount": sp.amount,
                    "date": _iso_date(sp.date),
                    "mode": str(sp.mode),
                    "transaction_id": sp.transaction_id,
                    "note": sp.note,
                    "location_id": sp.location_id,
                    "created_at": sp.created_at.isoformat() if sp.created_at else None,
                }
            )

    if direction in ("all", "outgoing", "expense"):
        if not mode or mode == "all":
            for ex in await Expense.find(base_filter).to_list():
                transactions.append(
                    {
                        "id": str(ex.id),
                        "kind": "expense",
                        "direction": "outgoing",
                        "ref_no": f"EXP-{ex.category[:4].upper()}",
                        "party_id": None,
                        "party_name": ex.category.replace("_", " ").title(),
                        "amount": ex.amount,
                        "date": _iso_date(ex.date),
                        "mode": "cash",
                        "transaction_id": None,
                        "note": ex.note,
                        "location_id": ex.location_id,
                        "created_at": ex.created_at.isoformat() if ex.created_at else None,
                    }
                )

    if search:
        q = search.strip().lower()
        transactions = [
            t
            for t in transactions
            if q in (t.get("party_name") or "").lower()
            or q in (t.get("ref_no") or "").lower()
            or q in (t.get("transaction_id") or "").lower()
            or q in (t.get("note") or "").lower()
        ]

    transactions.sort(
        key=lambda t: (t.get("date") or "", t.get("created_at") or ""), reverse=True
    )

    total_incoming = sum(t["amount"] for t in transactions if t["direction"] == "incoming")
    total_staff_paid = sum(t["amount"] for t in transactions if t["kind"] == "staff_salary")
    total_expenses = sum(t["amount"] for t in transactions if t["kind"] == "expense")
    total_outgoing = total_staff_paid + total_expenses

    return {
        "summary": {
            "total_incoming": total_incoming,
            "total_staff_paid": total_staff_paid,
            "total_expenses": total_expenses,
            "total_outgoing": total_outgoing,
            "net_cash_flow": total_incoming - total_outgoing,
            "count": len(transactions),
        },
        "items": transactions,
    }
