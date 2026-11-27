"""Assignment lifecycle & conflict validation helpers (IST-aware)."""

from __future__ import annotations

import datetime as dt
from typing import Any, List, Optional, Tuple

from app.models.assignment import Assignment
from app.models.employee import Employee
from app.models.enums import AssignmentStatus, EmployeeStatus, ShiftType
from app.services.dashboard_service import invalidate_dashboard_cache
from app.utils.ist_time import (
    IST,
    _DEFAULT_SHIFT_TIMES,
    ist_dt,
    now_ist,
    parse_hhmm,
    today_ist,
)

_OCCUPYING = (AssignmentStatus.SCHEDULED, AssignmentStatus.ACTIVE)


def assignment_end_datetime_ist(a: Any) -> Optional[dt.datetime]:
    """Compute exact IST completion datetime for an assignment with a finite end_date."""
    end_d = getattr(a, "end_date", None)
    if end_d is None:
        return None
    if isinstance(end_d, dt.datetime):
        end_d = end_d.astimezone(IST).date() if end_d.tzinfo else end_d.date()

    start_d = getattr(a, "start_date", None)
    if isinstance(start_d, dt.datetime):
        start_d = start_d.astimezone(IST).date() if start_d.tzinfo else start_d.date()

    shift_raw = getattr(a, "shift", ShiftType.FULL_24H)
    try:
        shift = shift_raw if isinstance(shift_raw, ShiftType) else ShiftType(str(shift_raw))
    except ValueError:
        shift = ShiftType.CUSTOM

    _, def_et = _DEFAULT_SHIFT_TIMES.get(shift, ("08:00", "20:00"))
    st_parsed = parse_hhmm(getattr(a, "start_time", None))
    et_parsed = parse_hhmm(getattr(a, "end_time", None))

    if et_parsed is None:
        if shift == ShiftType.FULL_24H and st_parsed is not None:
            et_parsed = st_parsed
        else:
            et_parsed = parse_hhmm(def_et) or dt.time(20, 0)

    eff_date = end_d
    # Single-day overnight / 24h cycle stored with end_date == start_date ends next day at et_parsed
    if (
        start_d is not None
        and end_d == start_d
        and st_parsed is not None
        and et_parsed <= st_parsed
        and shift in (ShiftType.FULL_24H, ShiftType.NIGHT)
    ):
        eff_date = end_d + dt.timedelta(days=1)

    return ist_dt(eff_date, et_parsed)


def is_assignment_currently_occupying(a: Any, now: Optional[dt.datetime] = None) -> bool:
    """True if assignment status is scheduled/active AND its IST end time hasn't passed yet."""
    status_val = str(getattr(getattr(a, "status", None), "value", getattr(a, "status", "")))
    if status_val not in (AssignmentStatus.SCHEDULED.value, AssignmentStatus.ACTIVE.value):
        return False
    end_dt = assignment_end_datetime_ist(a)
    if end_dt is None:
        return True
    ref_now = now or now_ist()
    return ref_now < end_dt


async def sync_expired_assignments(employee_ids: Optional[List[str]] = None) -> None:
    """Auto-complete active/scheduled assignments whose IST end_date+time has passed,
    and release employees to AVAILABLE when they have no remaining live assignments.
    """
    ref_now = now_ist()
    today_max = dt.datetime.combine(today_ist(), dt.time.max)

    asg_query: dict = {
        "status": {"$in": [s.value for s in _OCCUPYING]},
        "end_date": {"$ne": None, "$lte": today_max},
    }
    if employee_ids:
        asg_query["employee_id"] = {"$in": employee_ids}

    candidates = await Assignment.find(asg_query).to_list()
    changed = False
    touched_emps: set[str] = set()

    for a in candidates:
        end_dt = assignment_end_datetime_ist(a)
        if end_dt is not None and ref_now >= end_dt:
            a.status = AssignmentStatus.COMPLETED
            if not a.work_completed_at:
                a.work_completed_at = end_dt.astimezone(dt.timezone.utc)
            a.touch()
            await a.save()
            touched_emps.add(a.employee_id)
            changed = True

    # Ensure every employee marked ASSIGNED actually has an ongoing/future live assignment
    emp_query: dict = {"status": EmployeeStatus.ASSIGNED.value}
    if employee_ids:
        from bson import ObjectId
        oids = [ObjectId(eid) for eid in employee_ids if ObjectId.is_valid(eid)]
        if oids:
            emp_query["_id"] = {"$in": oids}

    assigned_emps = await Employee.find(emp_query).to_list()
    if assigned_emps:
        assigned_ids = [str(e.id) for e in assigned_emps]
        live_candidates = await Assignment.find(
            {
                "employee_id": {"$in": assigned_ids},
                "status": {"$in": [s.value for s in _OCCUPYING]},
            }
        ).to_list()
        still_busy: set[str] = set()
        for a in live_candidates:
            if is_assignment_currently_occupying(a, ref_now):
                still_busy.add(a.employee_id)

        for emp in assigned_emps:
            if str(emp.id) not in still_busy:
                emp.status = EmployeeStatus.AVAILABLE
                emp.touch()
                await emp.save()
                changed = True

    if changed:
        invalidate_dashboard_cache()


def _assignment_datetimes_ist(
    start_date: dt.date,
    end_date: Optional[dt.date],
    start_time: Optional[str],
    end_time: Optional[str],
    shift: Any = ShiftType.FULL_24H,
) -> Tuple[dt.datetime, Optional[dt.datetime]]:
    try:
        sh = shift if isinstance(shift, ShiftType) else ShiftType(str(shift))
    except ValueError:
        sh = ShiftType.CUSTOM
    def_st, def_et = _DEFAULT_SHIFT_TIMES.get(sh, ("08:00", "20:00"))
    st_t = parse_hhmm(start_time) or parse_hhmm(def_st) or dt.time(8, 0)
    s_dt = ist_dt(start_date, st_t)

    if end_date is None:
        return s_dt, None

    et_t = parse_hhmm(end_time) or (st_t if sh == ShiftType.FULL_24H else (parse_hhmm(def_et) or dt.time(20, 0)))
    eff_end_d = end_date
    if end_date == start_date and et_t <= st_t and sh in (ShiftType.FULL_24H, ShiftType.NIGHT):
        eff_end_d = end_date + dt.timedelta(days=1)
    e_dt = ist_dt(eff_end_d, et_t)
    if e_dt <= s_dt:
        e_dt = s_dt
    return s_dt, e_dt


async def find_employee_assignment_conflict(
    employee_id: str,
    start_date: dt.date,
    end_date: Optional[dt.date],
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    shift: Any = ShiftType.FULL_24H,
    customer_id: Optional[str] = None,
    exclude_assignment_id: Optional[str] = None,
) -> Optional[Tuple[Assignment, str]]:
    """Detect if `employee_id` already has ANY non-cancelled assignment overlapping the requested period.

    Checks BOTH active/scheduled duties AND past completed/replaced duties so a staff member
    cannot be double-booked or re-assigned on dates they already worked for the same customer.
    Sequential handovers touching at exact boundary time (prev_end <= new_start) are allowed.
    """
    query: dict = {
        "employee_id": employee_id,
        "status": {"$ne": AssignmentStatus.CANCELLED.value},
    }
    if exclude_assignment_id:
        from bson import ObjectId
        from bson.errors import InvalidId
        try:
            query["_id"] = {"$ne": ObjectId(exclude_assignment_id)}
        except (InvalidId, TypeError):
            pass

    existing_list = await Assignment.find(query).to_list()
    target_s_dt, target_e_dt = _assignment_datetimes_ist(
        start_date, end_date, start_time, end_time, shift
    )
    target_end_date = end_date or dt.date.max

    for a in existing_list:
        a_end_date = a.end_date or dt.date.max

        # Disjoint calendar date spans never overlap
        if start_date > a_end_date or a.start_date > target_end_date:
            continue

        a_s_dt, a_e_dt = _assignment_datetimes_ist(
            a.start_date, a.end_date, a.start_time, a.end_time, a.shift
        )

        # Boundary touch check: if previous duty ended at exact handover time <= new start (or vice versa)
        if a_e_dt is not None and target_s_dt >= a_e_dt:
            continue
        if target_e_dt is not None and a_s_dt >= target_e_dt:
            continue

        # Legacy same-date clock check when datetimes were incomplete
        if start_date == a_end_date and a_end_date != dt.date.max:
            if start_time and a.end_time and a.end_time <= start_time:
                continue
        if target_end_date == a.start_date and target_end_date != dt.date.max:
            if end_time and a.start_time and end_time <= a.start_time:
                continue

        # Also if exact handover left 0 effective hours on a replaced assignment, skip
        if a.end_date is not None and a.end_date == a.start_date and a.end_time == a.start_time and a.status == AssignmentStatus.REPLACED:
            continue

        same_cust = bool(customer_id and a.customer_id == customer_id)
        if same_cust:
            return a, "same_customer"

        # Different customer: only conflict if existing duty occupies/overlaps live or exact IST hours
        # (non-overlapping single-day split shifts at different hours, e.g. Morning vs Night, don't overlap above)
        return a, "other_customer"

    return None


def format_conflict_error(emp_name: str, conflict: Assignment, kind: str) -> str:
    s_txt = conflict.start_date.strftime("%d %b %Y")
    e_txt = conflict.end_date.strftime("%d %b %Y") if conflict.end_date else "Ongoing"
    if kind == "same_customer":
        return (
            f"{emp_name} ইতিমধ্যেই {conflict.customer_name}-এর কাছে {s_txt} থেকে {e_txt} পর্যন্ত কাজ করেছেন/করছেন। "
            f"একই কাস্টমার ও তারিখে পুনরায় অ্যাসাইন করা যাবে না। "
            f"({emp_name} already worked for {conflict.customer_name} from {conflict.start_date.isoformat()} to "
            f"{conflict.end_date.isoformat() if conflict.end_date else 'Ongoing'}). "
        ).strip()
    return (
        f"{emp_name} ইতিমধ্যেই {conflict.customer_name}-এর কাছে {s_txt} থেকে {e_txt} পর্যন্ত ডিউটিতে আছেন/ছিলেন। "
        f"({emp_name} is already assigned to {conflict.customer_name} from {conflict.start_date.isoformat()} to "
        f"{conflict.end_date.isoformat() if conflict.end_date else 'Ongoing'}). "
    ).strip()
