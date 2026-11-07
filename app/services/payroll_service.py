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

from app.models.attendance import AttendanceRecord
from app.models.employee import Employee
from app.models.enums import AttendanceStatus

_PAYABLE = (AttendanceStatus.PRESENT, AttendanceStatus.HALF_DAY)
_OFF = (AttendanceStatus.LEAVE, AttendanceStatus.OFF)


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


def _summarize(records: Sequence[AttendanceRecord], fallback_rate: Optional[int]) -> dict:
    """Aggregate a set of one employee's records into payslip totals.

    A staff member can have several rows on one date (multiple shifts), so pay
    counts are per-row (two shifts pay twice) while ``worked_days`` counts the
    distinct dates actually worked.
    """
    present = half = leave = off = absent = 0
    gross = 0
    by_shift: Dict[str, int] = {}
    off_dates: List[str] = []
    absent_dates: List[str] = []
    present_dates: set = set()
    marked_dates: set = set()

    for r in sorted(records, key=lambda x: (x.date, x.shift.value)):
        rate = r.pay_rate if r.pay_rate is not None else fallback_rate
        marked_dates.add(r.date)
        if r.status == AttendanceStatus.PRESENT:
            present += 1
            present_dates.add(r.date)
            by_shift[r.shift.value] = by_shift.get(r.shift.value, 0) + 1
        elif r.status == AttendanceStatus.HALF_DAY:
            half += 1
            present_dates.add(r.date)
            by_shift[r.shift.value] = by_shift.get(r.shift.value, 0) + 1
        elif r.status == AttendanceStatus.LEAVE:
            leave += 1
            off_dates.append(r.date.isoformat())
        elif r.status == AttendanceStatus.OFF:
            off += 1
            off_dates.append(r.date.isoformat())
        elif r.status == AttendanceStatus.ABSENT:
            absent += 1
            absent_dates.append(r.date.isoformat())
        gross += _day_amount(r.status, rate)

    payable_days = present + half * 0.5
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
        # One entry per shift worked that day (so two shifts show as two rows).
        for rec in sorted(recs, key=lambda x: x.shift.value):
            rate = rec.pay_rate if rec.pay_rate is not None else fallback_rate
            daily.append(
                {
                    "date": day.isoformat(),
                    "status": rec.status.value,
                    "shift": rec.shift.value,
                    "start_time": rec.start_time,
                    "end_time": rec.end_time,
                    "customer_name": rec.customer_name,
                    "pay_rate": rate,
                    "amount": _day_amount(rec.status, rate),
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
