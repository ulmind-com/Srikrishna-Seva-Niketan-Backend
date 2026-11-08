"""Payroll computation — monthly payslips derived from daily attendance.

Nothing here is stored: a payslip is always recomputed from the
:class:`~app.models.attendance.AttendanceRecord` rows for the month, so it can
never fall out of step with the attendance it is built from. Only ``present``
and ``half_day`` rows are paid; ``leave`` and ``off`` days surface as unpaid
off-dates on the slip.
"""

from __future__ import annotations

import calendar
import datetime as dt
from typing import Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from app.models.assignment import Assignment
from app.models.attendance import AttendanceRecord
from app.models.employee import Employee
from app.models.enums import AssignmentStatus, AttendanceStatus

_PAYABLE = (AttendanceStatus.PRESENT, AttendanceStatus.HALF_DAY)
_OFF = (AttendanceStatus.LEAVE, AttendanceStatus.OFF)
_OCCUPYING = (AssignmentStatus.SCHEDULED, AssignmentStatus.ACTIVE)
_IST = ZoneInfo("Asia/Kolkata")


def today_ist() -> dt.date:
    """Today's date in Asia/Kolkata — the business calendar, not the UTC server."""
    return dt.datetime.now(_IST).date()


async def materialize_from_assignments(
    first: dt.date, last: dt.date, location_ids: Optional[List[str]] = None
) -> int:
    """Create PRESENT attendance rows for assignment-active days that have none.

    "Auto Present": every day a staff member is engaged counts as present on the
    payslip without the admin marking it. Idempotent and override-safe:
    - a day the admin hand-marked (any status, no assignment_id) is left alone,
    - an assignment's day that already has its row is skipped,
    - two assignments on one day create two rows (two shifts pay twice),
    - days after today are never marked (pay only for worked days).
    Rows are inserted directly (no audit) since they are system-generated.
    """
    today = today_ist()
    cap = min(last, today)
    if cap < first:
        return 0

    first_dt = dt.datetime.combine(first, dt.time.min)
    cap_dt = dt.datetime.combine(cap, dt.time.min)

    a_filter: dict = {
        "status": {"$in": [s.value for s in _OCCUPYING]},
        "start_date": {"$lte": cap_dt},
        "$or": [{"end_date": None}, {"end_date": {"$gte": first_dt}}],
    }
    if location_ids is not None:
        a_filter["location_id"] = {"$in": location_ids}
    assignments = await Assignment.find(a_filter).to_list()
    if not assignments:
        return 0

    emp_ids = {a.employee_id for a in assignments}
    employees = {
        str(e.id): e for e in await Employee.find({"_id": {"$in": _oids(emp_ids)}}).to_list()
    }

    existing = await _fetch_records(first, cap)
    handmarked: set = set()  # (employee_id, date) owned by the admin
    synced: set = set()      # (employee_id, date, assignment_id) already present
    for r in existing:
        if r.assignment_id:
            synced.add((r.employee_id, r.date, r.assignment_id))
        else:
            handmarked.add((r.employee_id, r.date))

    new_rows: List[AttendanceRecord] = []
    for a in assignments:
        emp = employees.get(a.employee_id)
        if emp is None:
            continue
        rate = a.employee_daily_rate if a.employee_daily_rate is not None else emp.daily_rate
        a_start = max(a.start_date, first)
        a_end = min(a.end_date or cap, cap)
        day = a_start
        while day <= a_end:
            key_day = (a.employee_id, day)
            key_syn = (a.employee_id, day, str(a.id))
            if key_day not in handmarked and key_syn not in synced:
                new_rows.append(
                    AttendanceRecord(
                        employee_id=a.employee_id,
                        employee_name=a.employee_name,
                        location_id=a.location_id,
                        date=day,
                        status=AttendanceStatus.PRESENT,
                        shift=a.shift,
                        start_time=a.start_time,
                        end_time=a.end_time,
                        shift_windows=a.shift_windows,
                        assignment_id=str(a.id),
                        customer_name=a.customer_name,
                        pay_rate=rate,
                    )
                )
                synced.add(key_syn)
            day += dt.timedelta(days=1)

    if new_rows:
        await AttendanceRecord.insert_many(new_rows)
    return len(new_rows)


def _oids(ids):
    from bson import ObjectId
    from bson.errors import InvalidId

    out = []
    for i in ids:
        try:
            out.append(ObjectId(i))
        except (InvalidId, TypeError):
            pass
    return out


def parse_month(month: Optional[str]) -> Tuple[dt.date, dt.date, int]:
    """Return ``(first_day, last_day, day_count)`` for a ``YYYY-MM`` string.

    Defaults to the current month when ``month`` is missing or malformed.
    """
    today = dt.date.today()
    year, mon = today.year, today.month
    if month:
        try:
            parts = month.strip().split("-")
            year, mon = int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            year, mon = today.year, today.month
    days = calendar.monthrange(year, mon)[1]
    return dt.date(year, mon, 1), dt.date(year, mon, days), days


def _day_amount(status: AttendanceStatus, pay_rate: Optional[int]) -> int:
    rate = pay_rate or 0
    if status == AttendanceStatus.PRESENT:
        return rate
    if status == AttendanceStatus.HALF_DAY:
        return round(rate / 2)
    return 0


async def _fetch_records(
    first: dt.date, last: dt.date, extra_filter: Optional[dict] = None
) -> List[AttendanceRecord]:
    """Attendance rows whose date falls within ``[first, last]`` inclusive."""
    start = dt.datetime.combine(first, dt.time.min)
    end = dt.datetime.combine(last, dt.time.max)
    query: dict = {"date": {"$gte": start, "$lte": end}}
    if extra_filter:
        query.update(extra_filter)
    return await AttendanceRecord.find(query).to_list()


def _date_amount(rows: Sequence[AttendanceRecord], fallback_rate: Optional[int]) -> int:
    """Pay for ONE calendar date — one day = one pay, regardless of shift count.

    Two shifts on a day are two duties already priced in their assignments, so
    the day is paid once (the highest shift rate), never multiplied by shifts.
    """
    present_rates = [
        (r.pay_rate if r.pay_rate is not None else fallback_rate) or 0
        for r in rows
        if r.status == AttendanceStatus.PRESENT
    ]
    if present_rates:
        return max(present_rates)
    half_rates = [
        (r.pay_rate if r.pay_rate is not None else fallback_rate) or 0
        for r in rows
        if r.status == AttendanceStatus.HALF_DAY
    ]
    if half_rates:
        return round(max(half_rates) / 2)
    return 0  # leave / off / absent earn nothing


def _summarize(records: Sequence[AttendanceRecord], fallback_rate: Optional[int]) -> dict:
    """Aggregate one employee's records into payslip totals.

    Pay is **per calendar date** (one day = one pay), so a two-shift day is paid
    once. ``present_days`` / ``by_shift`` still count shift rows (so the admin
    can see two shifts ran), while pay and ``worked_days`` are date-based.
    """
    present = half = leave = off = absent = 0
    gross = 0
    leave_deduction = 0  # salary the staff would have earned on leave days
    by_shift: Dict[str, int] = {}
    off_dates: List[str] = []
    absent_dates: List[str] = []
    present_dates: set = set()
    half_only_dates: set = set()
    marked_dates: set = set()

    by_date: Dict[dt.date, List[AttendanceRecord]] = {}
    for r in records:
        by_date.setdefault(r.date, []).append(r)

    for r in records:
        marked_dates.add(r.date)
        if r.status == AttendanceStatus.PRESENT:
            present += 1
            by_shift[r.shift.value] = by_shift.get(r.shift.value, 0) + 1
        elif r.status == AttendanceStatus.HALF_DAY:
            half += 1
            by_shift[r.shift.value] = by_shift.get(r.shift.value, 0) + 1
        elif r.status == AttendanceStatus.LEAVE:
            leave += 1
            leave_deduction += (r.pay_rate if r.pay_rate is not None else fallback_rate) or 0
            off_dates.append(r.date.isoformat())
        elif r.status == AttendanceStatus.OFF:
            off += 1
            off_dates.append(r.date.isoformat())
        elif r.status == AttendanceStatus.ABSENT:
            absent += 1
            absent_dates.append(r.date.isoformat())

    # Pay once per date (highest shift rate), never per shift row.
    for day, rows in by_date.items():
        has_present = any(x.status == AttendanceStatus.PRESENT for x in rows)
        has_half = any(x.status == AttendanceStatus.HALF_DAY for x in rows)
        if has_present:
            present_dates.add(day)
        elif has_half:
            half_only_dates.add(day)
        gross += _date_amount(rows, fallback_rate)

    payable_days = len(present_dates) + len(half_only_dates) * 0.5
    return {
        # Per-row counts (pay context — two shifts on a day count twice).
        "present_days": present,
        "half_days": half,
        "leave_days": leave,
        "off_days": off,
        "absent_days": absent,
        # Distinct dates actually worked (present or half).
        "worked_days": len(present_dates),
        # Distinct dates with any record.
        "marked_days": len(marked_dates),
        "payable_days": payable_days,
        "gross_pay": gross,
        "leave_deduction": leave_deduction,
        "net_payable": gross,
        "by_shift": by_shift,
        "off_dates": off_dates,
        "absent_dates": absent_dates,
    }


async def build_payslip(
    employee: Employee, first: dt.date, last: dt.date, days: int
) -> dict:
    """One employee's full monthly payslip: day-by-day calendar plus totals."""
    emp_id = str(employee.id)
    records = await _fetch_records(first, last, {"employee_id": emp_id})
    by_date: Dict[dt.date, List[AttendanceRecord]] = {}
    for r in records:
        by_date.setdefault(r.date, []).append(r)
    fallback_rate = employee.daily_rate

    daily: List[dict] = []
    for d in range(1, days + 1):
        day = dt.date(first.year, first.month, d)
        recs = by_date.get(day)
        if not recs:
            daily.append(
                {
                    "date": day.isoformat(),
                    "status": "unmarked",
                    "shift": None,
                    "start_time": None,
                    "end_time": None,
                    "customer_name": None,
                    "pay_rate": None,
                    "amount": 0,
                }
            )
            continue
        # One entry per shift worked that day (so two shifts show as two rows),
        # but the day is paid ONCE — the day's pay sits on the first paid shift,
        # extra shifts show amount 0 (same duty, no double pay).
        day_amount = _date_amount(recs, fallback_rate)
        paid_emitted = False
        for rec in sorted(recs, key=lambda x: x.shift.value):
            rate = rec.pay_rate if rec.pay_rate is not None else fallback_rate
            is_payable = rec.status in (AttendanceStatus.PRESENT, AttendanceStatus.HALF_DAY)
            amount = 0
            if is_payable and not paid_emitted:
                amount = day_amount
                paid_emitted = True
            daily.append(
                {
                    "date": day.isoformat(),
                    "status": rec.status.value,
                    "shift": rec.shift.value,
                    "start_time": rec.start_time,
                    "end_time": rec.end_time,
                    "shift_windows": (
                        [w.model_dump() for w in rec.shift_windows] if rec.shift_windows else None
                    ),
                    "customer_name": rec.customer_name,
                    "pay_rate": rate,
                    "amount": amount,
                }
            )

    summary = _summarize(records, fallback_rate)
    summary["unmarked_days"] = days - summary["marked_days"]

    return {
        "employee": {
            "id": emp_id,
            "name": employee.name,
            "code": employee.code,
            "category": employee.category,
            "daily_rate": employee.daily_rate,
            "photo": employee.photo.model_dump(mode="json") if employee.photo else None,
            "location_id": employee.location_id,
        },
        "period": {
            "month": f"{first.year:04d}-{first.month:02d}",
            "start": first.isoformat(),
            "end": last.isoformat(),
            "days_in_month": days,
        },
        "summary": summary,
        "off_dates": summary["off_dates"],
        "absent_dates": summary["absent_dates"],
        "days": daily,
    }


async def payroll_run(
    first: dt.date,
    last: dt.date,
    days: int,
    location_ids: Optional[List[str]] = None,
) -> dict:
    """Summary line for every active employee for the month (the payroll run).

    ``location_ids`` restricts to those branches (branch-scoped managers); pass
    ``None`` for an unscoped, firm-wide run.
    """
    emp_filter: dict = {"is_active": True}
    if location_ids is not None:
        emp_filter["location_id"] = {"$in": location_ids}
    employees = await Employee.find(emp_filter).to_list()

    rec_filter: dict = {}
    if location_ids is not None:
        rec_filter["location_id"] = {"$in": location_ids}
    records = await _fetch_records(first, last, rec_filter or None)

    grouped: Dict[str, List[AttendanceRecord]] = {}
    for r in records:
        grouped.setdefault(r.employee_id, []).append(r)

    rows: List[dict] = []
    total_gross = 0
    for e in sorted(employees, key=lambda x: x.name.lower()):
        eid = str(e.id)
        summary = _summarize(grouped.get(eid, []), e.daily_rate)
        total_gross += summary["gross_pay"]
        rows.append(
            {
                "employee_id": eid,
                "name": e.name,
                "code": e.code,
                "category": e.category,
                "daily_rate": e.daily_rate,
                "location_id": e.location_id,
                "present_days": summary["present_days"],
                "half_days": summary["half_days"],
                "leave_days": summary["leave_days"],
                "off_days": summary["off_days"],
                "absent_days": summary["absent_days"],
                "worked_days": summary["worked_days"],
                "payable_days": summary["payable_days"],
                "gross_pay": summary["gross_pay"],
                "leave_deduction": summary["leave_deduction"],
                "net_payable": summary["net_payable"],
                "by_shift": summary["by_shift"],
            }
        )

    return {
        "period": {
            "month": f"{first.year:04d}-{first.month:02d}",
            "start": first.isoformat(),
            "end": last.isoformat(),
            "days_in_month": days,
        },
        "totals": {
            "employees": len(rows),
            "total_gross_pay": total_gross,
        },
        "rows": rows,
    }
