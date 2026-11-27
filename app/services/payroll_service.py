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

from app.models.assignment import Assignment
from app.models.attendance import AttendanceRecord
from app.models.employee import Employee
from app.models.enums import AssignmentStatus, AttendanceStatus, ShiftType, StaffVerificationStatus
from app.utils.ist_time import (
    assignment_attendance_schedule_ist,
    calculate_duty_days_ist,
    today_ist,
)

_PAYABLE = (AttendanceStatus.PRESENT, AttendanceStatus.HALF_DAY)
_OFF = (AttendanceStatus.LEAVE, AttendanceStatus.OFF)
_OCCUPYING = (AssignmentStatus.SCHEDULED, AssignmentStatus.ACTIVE)


def _assignment_gross_days(a: Assignment) -> Optional[float]:
    """Return exact gross worked days (before duty breaks) when assignment span is closed."""
    if a.end_date is not None:
        return calculate_duty_days_ist(
            a.start_date,
            a.end_date,
            a.start_time,
            a.end_time,
            a.shift,
            duty_breaks=None,
            shift_windows=a.shift_windows,
            is_handover=(a.status == AssignmentStatus.REPLACED),
        )
    if a.status in (AssignmentStatus.COMPLETED, AssignmentStatus.REPLACED) and a.days_worked is not None:
        return float(a.days_worked)
    return None


def _effective_assignment_end(a: Assignment, cap: dt.date) -> dt.date:
    """Final attendance calendar date of an assignment aligned with IST slot calculation."""
    schedule = assignment_attendance_schedule_ist(a, cap)
    if not schedule:
        return a.start_date - dt.timedelta(days=1)
    return schedule[-1]["date"]


async def _sync_one_assignment_rows(
    a: Assignment,
    emp: Optional[Employee],
    existing_synced: Dict[Tuple[str, dt.date, str], AttendanceRecord],
    handmarked: set,
    new_rows: List[AttendanceRecord],
    cap: dt.date,
    filter_first: Optional[dt.date] = None,
    filter_last: Optional[dt.date] = None,
) -> None:
    """Reconcile synced AttendanceRecord rows for a single assignment against IST schedule."""
    if emp is None or emp.verification_status != StaffVerificationStatus.VERIFIED:
        return
    rate = a.employee_daily_rate if a.employee_daily_rate is not None else emp.daily_rate
    schedule = assignment_attendance_schedule_ist(a, cap)
    valid_dates = {item["date"] for item in schedule}

    # Prune any stale synced rows for this assignment outside its valid worked dates
    try:
        asg_rows = await AttendanceRecord.find({"assignment_id": str(a.id)}).to_list()
        stale_ids = [r.id for r in asg_rows if r.date not in valid_dates and r.id is not None]
        if stale_ids:
            await AttendanceRecord.get_motor_collection().delete_many({"_id": {"$in": stale_ids}})
    except Exception:
        pass

    for item in schedule:
        day: dt.date = item["date"]
        if filter_first and day < filter_first:
            continue
        if filter_last and day > filter_last:
            continue

        rec_status: AttendanceStatus = item["status"]
        day_start_time: Optional[str] = item["start_time"]
        day_end_time: Optional[str] = item["end_time"]
        day_windows = item["shift_windows"]
        day_note: Optional[str] = item["note"]
        day_rate = 0 if rec_status == AttendanceStatus.OFF else rate

        key_day = (a.employee_id, day)
        key_syn = (a.employee_id, day, str(a.id))
        existing_rec = existing_synced.get(key_syn)

        if existing_rec is not None:
            updates: dict = {}
            is_system_break = bool(
                existing_rec.note
                and (
                    existing_rec.note.startswith("Break:")
                    or existing_rec.note.startswith("Half Break:")
                )
            )
            # Only overwrite status/pay_rate when the row is still assignment-driven
            # (never overwrite an admin manual ABSENT, manual OFF, or Leave mark).
            is_manual_leave = existing_rec.status == AttendanceStatus.LEAVE and bool(
                existing_rec.note and (
                    existing_rec.note.startswith("Paid Leave:")
                    or existing_rec.note.startswith("Leave:")
                )
            )
            if (
                existing_rec.status in (AttendanceStatus.PRESENT, AttendanceStatus.HALF_DAY) or is_system_break
            ) and not is_manual_leave:
                if existing_rec.status != rec_status:
                    updates["status"] = rec_status
                if existing_rec.pay_rate != day_rate:
                    updates["pay_rate"] = day_rate
                if existing_rec.note != day_note and (day_note is not None or is_system_break):
                    updates["note"] = day_note
            if existing_rec.shift != a.shift:
                updates["shift"] = a.shift
            if existing_rec.start_time != day_start_time:
                updates["start_time"] = day_start_time
            if existing_rec.end_time != day_end_time:
                updates["end_time"] = day_end_time
            if existing_rec.customer_name != a.customer_name:
                updates["customer_name"] = a.customer_name
            if updates:
                updates["updated_at"] = dt.datetime.now(dt.timezone.utc)
                for k, v in updates.items():
                    setattr(existing_rec, k, v)
                await existing_rec.save()
        elif key_day not in handmarked:
            rec_doc = AttendanceRecord(
                employee_id=a.employee_id,
                employee_name=a.employee_name,
                location_id=a.location_id,
                date=day,
                status=rec_status,
                shift=a.shift,
                start_time=day_start_time,
                end_time=day_end_time,
                shift_windows=day_windows,
                assignment_id=str(a.id),
                customer_name=a.customer_name,
                pay_rate=day_rate,
                note=day_note,
            )
            new_rows.append(rec_doc)
            existing_synced[key_syn] = rec_doc


async def sync_assignment_attendance(a: Assignment, cap_date: Optional[dt.date] = None) -> None:
    """Immediately reconcile AttendanceRecord rows when a single assignment is edited, replaced, or paused."""
    cap = min(cap_date or today_ist(), today_ist())
    if a.status == AssignmentStatus.CANCELLED:
        await AttendanceRecord.get_motor_collection().delete_many({"assignment_id": str(a.id)})
        return

    emp = await Employee.get(a.employee_id)
    if emp is None or emp.verification_status != StaffVerificationStatus.VERIFIED:
        return

    existing_rows = await AttendanceRecord.find({"employee_id": a.employee_id}).to_list()
    handmarked: set = set()
    existing_synced: Dict[Tuple[str, dt.date, str], AttendanceRecord] = {}
    for r in existing_rows:
        if r.assignment_id:
            existing_synced[(r.employee_id, r.date, r.assignment_id)] = r
        else:
            handmarked.add((r.employee_id, r.date))

    new_rows: List[AttendanceRecord] = []
    await _sync_one_assignment_rows(a, emp, existing_synced, handmarked, new_rows, cap)
    if new_rows:
        await AttendanceRecord.insert_many(new_rows)


async def materialize_from_assignments(
    first: dt.date, last: dt.date, location_ids: Optional[List[str]] = None
) -> int:
    """Create and sync AttendanceRecord rows from active/closed engagements in IST.

    Idempotent and override-safe:
    - manual admin rows (no assignment_id) are left alone,
    - synced rows reflect exact IST start/end dates, times, shifts, handovers, and merged breaks,
    - stale rows outside an assignment's worked IST span (e.g. extra checkout morning) are pruned,
    - days after today (IST) are never marked.
    """
    today = today_ist()
    cap = min(last, today)
    if cap < first:
        return 0

    first_dt = dt.datetime.combine(first, dt.time.min)
    cap_dt = dt.datetime.combine(cap, dt.time.min)

    a_filter: dict = {
        "status": {"$ne": AssignmentStatus.CANCELLED.value},
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

    # Clean up orphan attendance records whose assignments have been deleted or cancelled
    try:
        all_asgs = await Assignment.find({}).to_list()
        valid_asg_ids = {str(a.id) for a in all_asgs if a.status != AssignmentStatus.CANCELLED}
        await AttendanceRecord.get_motor_collection().delete_many({
            "assignment_id": {"$exists": True, "$ne": None, "$nin": list(valid_asg_ids)},
        })
        existing = await _fetch_records(first, cap)
    except Exception:
        pass

    handmarked: set = set()  # (employee_id, date) owned by the admin
    existing_synced: dict[tuple[str, dt.date, str], AttendanceRecord] = {}
    for r in existing:
        if r.assignment_id:
            existing_synced[(r.employee_id, r.date, r.assignment_id)] = r
        else:
            handmarked.add((r.employee_id, r.date))

    new_rows: List[AttendanceRecord] = []
    for a in assignments:
        await _sync_one_assignment_rows(
            a,
            employees.get(a.employee_id),
            existing_synced,
            handmarked,
            new_rows,
            cap,
            filter_first=first,
            filter_last=cap,
        )

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

    Defaults to the current month in IST when ``month`` is missing or malformed.
    """
    today = today_ist()
    year, mon = today.year, today.month
    if month:
        try:
            parts = month.strip().split("-")
            year, mon = int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            year, mon = today.year, today.month
    days = calendar.monthrange(year, mon)[1]
    return dt.date(year, mon, 1), dt.date(year, mon, days), days


def _is_paid_leave(r: AttendanceRecord) -> bool:
    return r.status == AttendanceStatus.LEAVE and bool(r.note and r.note.startswith("Paid Leave"))


def _day_amount(status: AttendanceStatus, pay_rate: Optional[int], is_paid_leave: bool = False) -> int:
    rate = pay_rate or 0
    if status == AttendanceStatus.PRESENT or is_paid_leave:
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
        if r.status == AttendanceStatus.PRESENT or _is_paid_leave(r)
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
    return 0  # unpaid leave / off / absent earn nothing


def _summarize(records: Sequence[AttendanceRecord], fallback_rate: Optional[int]) -> dict:
    """Aggregate one employee's records into payslip totals.

    A staff member can work multiple shifts/timings on the same calendar day.
    - Day counts (present_days, half_days, absent_days, off_days, worked_days, payable_days)
      are based on calendar dates: one worked date = 1 day present.
    - total_shifts and by_shift count individual shift duties worked across those days.
    """
    by_date: Dict[dt.date, List[AttendanceRecord]] = {}
    for r in records:
        by_date.setdefault(r.date, []).append(r)

    by_shift: Dict[str, int] = {}
    total_shifts = 0
    for r in records:
        if r.status in (AttendanceStatus.PRESENT, AttendanceStatus.HALF_DAY):
            total_shifts += 1
            by_shift[r.shift.value] = by_shift.get(r.shift.value, 0) + 1

    present_dates: set[dt.date] = set()
    half_only_dates: set[dt.date] = set()
    paid_leave_dates: set[dt.date] = set()
    absent_dates: set[dt.date] = set()
    off_dates: set[dt.date] = set()
    leave_only_count = 0
    marked_dates: set[dt.date] = set(by_date.keys())
    gross = 0

    # Pay once per date (highest shift rate), never per shift row.
    for day, rows in by_date.items():
        has_present = any(x.status == AttendanceStatus.PRESENT for x in rows)
        has_paid_leave = any(_is_paid_leave(x) for x in rows)
        has_half = any(x.status == AttendanceStatus.HALF_DAY for x in rows)
        has_absent = any(x.status == AttendanceStatus.ABSENT for x in rows)
        has_leave = any(x.status == AttendanceStatus.LEAVE for x in rows)
        has_off = any(x.status in (AttendanceStatus.OFF, AttendanceStatus.LEAVE) for x in rows)

        if has_present:
            present_dates.add(day)
        elif has_paid_leave:
            paid_leave_dates.add(day)
            leave_only_count += 1
        elif has_half:
            half_only_dates.add(day)
        elif has_absent:
            absent_dates.add(day)
        elif has_off:
            off_dates.add(day)
            if has_leave:
                leave_only_count += 1

        gross += _date_amount(rows, fallback_rate)

    present_count = len(present_dates)
    half_count = len(half_only_dates)
    paid_leave_count = len(paid_leave_dates)
    worked_count = present_count + half_count
    payable_days = present_count + half_count * 0.5 + paid_leave_count

    return {
        "present_days": present_count,
        "total_shifts": total_shifts,
        "half_days": half_count,
        "leave_days": leave_only_count,
        "off_days": len(off_dates),
        "absent_days": len(absent_dates),
        "worked_days": worked_count,
        "marked_days": len(marked_dates),
        "payable_days": payable_days,
        "gross_pay": gross,
        "leave_deduction": 0,
        "net_payable": gross,
        "by_shift": by_shift,
        "off_dates": [d.isoformat() for d in sorted(off_dates)],
        "absent_dates": [d.isoformat() for d in sorted(absent_dates)],
    }


async def build_payslip(
    employee: Employee,
    first: dt.date,
    last: dt.date,
    days: int,
    is_custom_range: bool = False,
) -> dict:
    """One employee's payslip for [first, last] (month or custom date range)."""
    emp_id = str(employee.id)
    records = await _fetch_records(first, last, {"employee_id": emp_id})
    by_date: Dict[dt.date, List[AttendanceRecord]] = {}
    for r in records:
        by_date.setdefault(r.date, []).append(r)
    fallback_rate = employee.daily_rate
    if fallback_rate is None:
        for r in records:
            if r.pay_rate:
                fallback_rate = r.pay_rate
                break

    daily: List[dict] = []
    for d in range(days):
        day = first + dt.timedelta(days=d)
        recs = by_date.get(day)
        if not recs:
            daily.append(
                {
                    "date": day.isoformat(),
                    "status": "unmarked",
                    "shift": None,
                    "start_time": None,
                    "end_time": None,
                    "duration": None,
                    "customer_name": None,
                    "pay_rate": None,
                    "amount": 0,
                }
            )
            continue

        # Deduplicate multiple identical records for the day
        seen_keys = set()
        unique_recs: List[AttendanceRecord] = []
        for r in recs:
            k = (r.customer_name, r.shift.value if r.shift else None, r.start_time, r.end_time)
            if k not in seen_keys:
                seen_keys.add(k)
                unique_recs.append(r)

        # One entry per shift worked that day (so two shifts show as two rows),
        # but the day is paid ONCE — the day's pay sits on the first paid shift,
        # extra shifts show amount 0 (same duty, no double pay).
        day_amount = _date_amount(unique_recs, fallback_rate)
        paid_emitted = False
        for rec in sorted(unique_recs, key=lambda x: x.shift.value):
            rate = rec.pay_rate if rec.pay_rate is not None else fallback_rate
            is_payable = rec.status in (AttendanceStatus.PRESENT, AttendanceStatus.HALF_DAY)
            amount = 0
            if is_payable and not paid_emitted:
                amount = day_amount
                paid_emitted = True

            duration_str = None
            if rec.status in (AttendanceStatus.OFF, AttendanceStatus.LEAVE, AttendanceStatus.ABSENT):
                duration_str = "—"
            elif rec.status == AttendanceStatus.HALF_DAY and rec.shift == ShiftType.FULL_24H:
                duration_str = "Half Day (12 hrs)"
            elif rec.shift == ShiftType.FULL_24H:
                duration_str = "24 hrs"
            elif rec.start_time and rec.end_time:
                try:
                    sh, sm = map(int, rec.start_time.split(":")[:2])
                    eh, em = map(int, rec.end_time.split(":")[:2])
                    dur_h = (eh * 60 + em - (sh * 60 + sm)) / 60.0
                    if dur_h <= 0:
                        dur_h += 24.0
                    duration_str = f"{int(dur_h) if dur_h.is_integer() else dur_h:.1f} hrs"
                except Exception:
                    duration_str = "12 hrs"
            elif rec.shift == ShiftType.MORNING:
                duration_str = "8 hrs"
            elif rec.shift in (ShiftType.DAY, ShiftType.NIGHT, ShiftType.CUSTOM):
                duration_str = "12 hrs"

            daily.append(
                {
                    "date": day.isoformat(),
                    "status": "off" if rec.status == AttendanceStatus.LEAVE else rec.status.value,
                    "shift": rec.shift.value,
                    "start_time": rec.start_time,
                    "end_time": rec.end_time,
                    "duration": duration_str,
                    "shift_windows": (
                        [w.model_dump(mode="json") for w in rec.shift_windows] if rec.shift_windows else None
                    ),
                    "customer_name": rec.customer_name,
                    "pay_rate": rate,
                    "amount": amount,
                }
            )

    summary = _summarize(records, fallback_rate)
    summary["unmarked_days"] = days - summary["marked_days"]

    from app.models.finance import StaffPayment
    eff_month = f"{first.year:04d}-{first.month:02d}"
    same_month_range = first.year == last.year and first.month == last.month
    sp_docs = await StaffPayment.find({"employee_id": emp_id}).to_list()
    paid_amount = sum(
        sp.amount
        for sp in sp_docs
        if (
            (first <= sp.date <= last) or (same_month_range and sp.month == eff_month)
            if is_custom_range or not sp.month
            else (sp.month == eff_month)
        )
    )
    summary["paid_amount"] = paid_amount
    summary["remaining_amount"] = max(0, summary["net_payable"] - paid_amount)

    return {
        "employee": {
            "id": emp_id,
            "name": employee.name,
            "code": employee.code,
            "category": employee.category,
            "daily_rate": fallback_rate,
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
    is_custom_range: bool = False,
) -> dict:
    """Summary line for every active employee for [first, last] (month or custom date range).

    ``location_ids`` restricts to those branches (branch-scoped managers); pass
    ``None`` for an unscoped, firm-wide run.
    """
    emp_filter: dict = {
        "is_active": True,
        "verification_status": StaffVerificationStatus.VERIFIED.value,
    }
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

    from app.models.finance import StaffPayment

    eff_month = f"{first.year:04d}-{first.month:02d}"
    same_month_range = first.year == last.year and first.month == last.month
    sp_filter: dict = {}
    if location_ids is not None:
        sp_filter["location_id"] = {"$in": location_ids}
    all_sp = await StaffPayment.find(sp_filter).to_list()
    sp_by_emp: Dict[str, int] = {}
    for sp in all_sp:
        matches = (
            ((first <= sp.date <= last) or (same_month_range and sp.month == eff_month))
            if is_custom_range or not sp.month
            else (sp.month == eff_month)
        )
        if matches:
            sp_by_emp[sp.employee_id] = sp_by_emp.get(sp.employee_id, 0) + sp.amount

    rows: List[dict] = []
    total_gross = 0
    total_paid = 0
    for e in sorted(employees, key=lambda x: x.name.lower()):
        eid = str(e.id)
        emp_records = grouped.get(eid, [])
        rate = e.daily_rate
        if rate is None:
            for r in emp_records:
                if r.pay_rate:
                    rate = r.pay_rate
                    break
        summary = _summarize(emp_records, rate)
        total_gross += summary["gross_pay"]
        emp_paid = sp_by_emp.get(eid, 0)
        total_paid += emp_paid
        rows.append(
            {
                "employee_id": eid,
                "name": e.name,
                "code": e.code,
                "category": e.category,
                "daily_rate": rate,
                "location_id": e.location_id,
                "present_days": summary["present_days"],
                "total_shifts": summary.get("total_shifts", summary["present_days"]),
                "half_days": summary["half_days"],
                "leave_days": summary["leave_days"],
                "off_days": summary["off_days"],
                "absent_days": summary["absent_days"],
                "worked_days": summary["worked_days"],
                "payable_days": summary["payable_days"],
                "gross_pay": summary["gross_pay"],
                "leave_deduction": summary["leave_deduction"],
                "net_payable": summary["net_payable"],
                "paid_amount": emp_paid,
                "remaining_amount": max(0, summary["net_payable"] - emp_paid),
                "by_shift": summary["by_shift"],
            }
        )

    return {
        "period": {
            "month": eff_month,
            "start": first.isoformat(),
            "end": last.isoformat(),
            "days_in_month": days,
        },
        "totals": {
            "employees": len(rows),
            "total_gross_pay": total_gross,
            "total_paid": total_paid,
            "total_remaining": max(0, total_gross - total_paid),
        },
        "rows": rows,
    }
