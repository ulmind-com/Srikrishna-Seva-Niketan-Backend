"""Payroll endpoints — daily attendance marking and monthly payslips.

Attendance is the source of truth: each staff member has at most one record per
calendar day (present / half-day / leave / off). Payslips are computed from
those rows on demand (see :mod:`app.services.payroll_service`), so pay always
matches the attendance behind it. Everything is multi-location scoped exactly
like the assignments and employees modules.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.api.helpers import item_response, paginated_response
from app.core.exceptions import BadRequestException
from app.core.pagination import PaginationParams, pagination_params
from app.core.responses import success_response
from app.dependencies.auth import ActorContext, require_permission
from app.dependencies.scoping import ensure_can_access
from app.models.assignment import Assignment, DutyBreak
from app.models.attendance import AttendanceRecord, StaffLeave
from app.models.employee import Employee
from app.models.enums import (
    AssignmentStatus,
    AttendanceStatus,
    LeaveApprovalStatus,
    StaffVerificationStatus,
)
from app.repositories.base import BaseRepository
from app.schemas.attendance import (
    AttendanceBulkCreate,
    AttendanceCreate,
    AttendanceResponse,
    AttendanceUpdate,
    StaffLeaveCreate,
    StaffLeaveResponse,
    StaffLeaveUpdate,
)
from app.services import payroll_service
from app.services.crud import CrudService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/payroll", tags=["Payroll"])

_attendance = CrudService(
    AttendanceRecord, entity="attendance", search_fields=("employee_name", "customer_name"),
)
_leaves = CrudService(StaffLeave, entity="staff_leave", search_fields=("employee_name", "customer_name", "reason"))
_employees: BaseRepository[Employee] = BaseRepository(Employee)

_OCCUPYING = {AssignmentStatus.SCHEDULED, AssignmentStatus.ACTIVE}


async def _upsert_day(
    employee: Employee,
    day: dt.date,
    *,
    status: AttendanceStatus,
    shift,
    pay_rate: Optional[int],
    customer_name: Optional[str],
    note: Optional[str],
    assignment_id: Optional[str],
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    shift_windows=None,
    actor: ActorContext,
) -> AttendanceRecord:
    """Create the day's record, or update the matching one.

    A staff member may have several rows per day (one per shift / engagement),
    so the row is keyed by (employee, date, assignment_id) for synced rows and
    (employee, date, shift) for hand-marked rows — not just (employee, date).
    """
    rate = pay_rate if pay_rate is not None else employee.daily_rate
    shift_value = getattr(shift, "value", shift)
    query: dict = {"employee_id": str(employee.id), "date": day}
    if assignment_id:
        query["assignment_id"] = assignment_id
    else:
        query["assignment_id"] = None
        query["shift"] = shift_value
    existing = await AttendanceRecord.find_one(query)
    data = {
        "status": status,
        "shift": shift,
        "start_time": start_time,
        "end_time": end_time,
        "shift_windows": shift_windows,
        "pay_rate": rate,
        "customer_name": customer_name,
        "note": note,
        "assignment_id": assignment_id,
    }
    if existing is not None:
        # Only overwrite fields that were explicitly supplied.
        clean = {k: v for k, v in data.items() if v is not None or k in ("status", "shift")}
        return await _attendance.update(str(existing.id), clean, actor)
    data.update(
        {
            "employee_id": str(employee.id),
            "employee_name": employee.name,
            "location_id": employee.location_id,
            "date": day,
        }
    )
    return await _attendance.create(data, actor)


def _month_bounds(month: Optional[str]):
    return payroll_service.parse_month(month)


# --------------------------------------------------------------------------
# Attendance
# --------------------------------------------------------------------------

@router.get("/attendance", summary="List attendance records")
async def list_attendance(
    params: PaginationParams = Depends(pagination_params),
    employee_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    location_id: Optional[str] = Query(None),
    date_from: Optional[dt.date] = Query(None),
    date_to: Optional[dt.date] = Query(None),
    actor: ActorContext = Depends(require_permission("payroll", "view")),
) -> dict:
    filters: dict = actor.scope_filter()
    if employee_id is not None:
        filters["employee_id"] = employee_id
    if status is not None:
        filters["status"] = status
    if location_id is not None and actor.can_access_location(location_id):
        filters["location_id"] = location_id
    if date_from is not None or date_to is not None:
        bounds: dict = {}
        if date_from is not None:
            bounds["$gte"] = dt.datetime.combine(date_from, dt.time.min)
        if date_to is not None:
            bounds["$lte"] = dt.datetime.combine(date_to, dt.time.max)
        filters["date"] = bounds
    items, total = await _attendance.paginate(
        page=params.page, page_size=params.page_size, search=params.search,
        sort_by=params.sort_by or "date", sort_order=params.sort_direction,
        filters=filters or None,
    )
    return paginated_response(AttendanceResponse, items, total, params)


@router.get("/attendance/day", summary="One day's attendance roster")
async def day_roster(
    date: Optional[dt.date] = Query(None, description="Defaults to today."),
    location_id: Optional[str] = Query(None),
    actor: ActorContext = Depends(require_permission("payroll", "view")),
) -> dict:
    """Who was present / on leave / off on a single day, with branch totals."""
    day = date or payroll_service.today_ist()

    # Auto-fill present rows from the day's duties so the roster reflects them.
    if actor.is_scoped:
        mat_locs = actor.location_ids
    elif location_id is not None and actor.can_access_location(location_id):
        mat_locs = [location_id]
    else:
        mat_locs = None
    await payroll_service.materialize_from_assignments(day, day, mat_locs)

    filters: dict = actor.scope_filter()
    filters["date"] = day
    if location_id is not None and actor.can_access_location(location_id):
        filters["location_id"] = location_id

    # Read raw and validate each row on its own, so a single corrupt/legacy
    # attendance document can never 500 the whole roster (it is skipped + logged).
    raw_rows = await AttendanceRecord.get_motor_collection().find(filters).to_list(length=None)
    items: list[AttendanceRecord] = []
    for raw in raw_rows:
        try:
            items.append(AttendanceRecord.model_validate(raw))
        except Exception as exc:
            logger.warning("Skipping unparseable attendance row %s: %s", raw.get("_id"), exc)
    items.sort(key=lambda a: (a.employee_name or "").lower())

    counts: dict[str, int] = {s.value: 0 for s in AttendanceStatus}
    records: list[dict] = []
    for a in items:
        try:
            dumped = AttendanceResponse.model_validate(a).model_dump(mode="json")
            if dumped.get("status") == "leave":
                dumped["status"] = "off"
            records.append(dumped)
            status_val = "off" if a.status.value == "leave" else a.status.value
            counts[status_val] = counts.get(status_val, 0) + 1
        except Exception as exc:
            logger.warning("Skipping attendance row in roster serialize: %s", exc)

    return success_response(
        data={
            "date": day.isoformat(),
            "totals": {
                "marked": len(records),
                "present": counts.get("present", 0),
                "half_day": counts.get("half_day", 0),
                "off": counts.get("off", 0),
                "absent": counts.get("absent", 0),
            },
            "records": records,
        }
    )


@router.post("/attendance", status_code=201, summary="Mark one day's attendance")
async def mark_attendance(
    payload: AttendanceCreate,
    actor: ActorContext = Depends(require_permission("payroll", "create")),
) -> dict:
    employee = await _employees.get(payload.employee_id)
    if employee is None:
        raise BadRequestException("Employee not found")
    ensure_can_access(actor, employee.location_id)
    doc = await _upsert_day(
        employee,
        payload.date,
        status=payload.status,
        shift=payload.shift,
        pay_rate=payload.pay_rate,
        customer_name=payload.customer_name,
        note=payload.note,
        assignment_id=payload.assignment_id,
        start_time=payload.start_time,
        end_time=payload.end_time,
        actor=actor,
    )
    return item_response(AttendanceResponse, doc, "Attendance marked")


@router.post("/attendance/bulk", summary="Mark a whole roster for one day")
async def mark_attendance_bulk(
    payload: AttendanceBulkCreate,
    actor: ActorContext = Depends(require_permission("payroll", "create")),
) -> dict:
    """Mark many staff for a single day in one request (morning roster)."""
    saved = 0
    skipped: list[dict] = []
    for item in payload.items:
        employee = await _employees.get(item.employee_id)
        if employee is None or not actor.can_access_location(employee.location_id):
            skipped.append({"employee_id": item.employee_id, "reason": "not found or out of scope"})
            continue
        await _upsert_day(
            employee,
            payload.date,
            status=item.status,
            shift=item.shift,
            pay_rate=item.pay_rate,
            customer_name=None,
            note=item.note,
            assignment_id=None,
            start_time=item.start_time,
            end_time=item.end_time,
            actor=actor,
        )
        saved += 1
    return success_response(
        data={"date": payload.date.isoformat(), "saved": saved, "skipped": skipped},
        message=f"Marked {saved} staff for {payload.date.isoformat()}",
    )


@router.post("/attendance/sync-from-assignments", summary="Auto-mark from running duties")
async def sync_from_assignments(
    date: Optional[dt.date] = Query(None, description="Defaults to today."),
    actor: ActorContext = Depends(require_permission("payroll", "create")),
) -> dict:
    """Mark everyone on a running engagement that day as present (their shift).

    Implements "it flows in automatically": for every assignment active on the
    date, a present record is created for the staff member with the engagement's
    shift and the customer's name — unless that day was already marked by hand.
    """
    day = date or payroll_service.today_ist()
    day_dt = dt.datetime.combine(day, dt.time.min)

    filters: dict = actor.scope_filter()
    # Include completed duties too (a finished shift is still a worked day).
    filters["status"] = {"$ne": AssignmentStatus.CANCELLED.value}
    filters["start_date"] = {"$lte": day_dt}
    filters["$or"] = [{"end_date": None}, {"end_date": {"$gte": day_dt}}]
    running = await Assignment.find(filters).to_list()

    created = 0
    skipped = 0
    for a in running:
        # Respect exact duty days (so a 24h handover checkout morning doesn't get an extra day).
        if payroll_service._effective_assignment_end(a, day) < day:
            skipped += 1
            continue
        # Keyed per assignment, so a nurse with two duties that day (e.g. a day
        # shift at one client and a night shift at another) gets two rows.
        existing = await AttendanceRecord.find_one(
            {"employee_id": a.employee_id, "date": day, "assignment_id": str(a.id)}
        )
        if existing is not None:
            skipped += 1
            continue
        employee = await _employees.get(a.employee_id)
        if employee is None:
            skipped += 1
            continue
        await _upsert_day(
            employee,
            day,
            status=AttendanceStatus.PRESENT,
            shift=a.shift,
            # Per-day staff rate — NOT the engagement total (employee_payout).
            pay_rate=a.employee_daily_rate or employee.daily_rate,
            customer_name=a.customer_name,
            note=None,
            assignment_id=str(a.id),
            start_time=a.start_time,
            end_time=a.end_time,
            shift_windows=[w.model_dump() for w in a.shift_windows] if a.shift_windows else None,
            actor=actor,
        )
        created += 1

    return success_response(
        data={"date": day.isoformat(), "created": created, "skipped": skipped},
        message=f"Synced {created} duties into attendance for {day.isoformat()}",
    )


@router.put("/attendance/{record_id}", summary="Update an attendance record")
async def update_attendance(
    record_id: str,
    payload: AttendanceUpdate,
    actor: ActorContext = Depends(require_permission("payroll", "update")),
) -> dict:
    existing = await _attendance.get_or_404(record_id)
    ensure_can_access(actor, existing.location_id)
    data = payload.model_dump(exclude_unset=True)
    doc = await _attendance.update(record_id, data, actor)
    return item_response(AttendanceResponse, doc, "Attendance updated")


@router.delete("/attendance/{record_id}", summary="Delete an attendance record")
async def delete_attendance(
    record_id: str,
    actor: ActorContext = Depends(require_permission("payroll", "delete")),
) -> dict:
    existing = await _attendance.get_or_404(record_id)
    ensure_can_access(actor, existing.location_id)
    await _attendance.delete(record_id, actor)
    return success_response(message="Attendance record deleted")


# --------------------------------------------------------------------------
# Payslips
# --------------------------------------------------------------------------

@router.get("/payslip", summary="One employee's payslip / attendance statement")
async def get_payslip(
    employee_id: str = Query(...),
    month: Optional[str] = Query(None, description="YYYY-MM; defaults to this month."),
    start_date: Optional[dt.date] = Query(None, description="Custom start date (inclusive)"),
    end_date: Optional[dt.date] = Query(None, description="Custom end date (inclusive)"),
    actor: ActorContext = Depends(require_permission("payroll", "view")),
) -> dict:
    employee = await _employees.get(employee_id)
    if employee is None:
        raise BadRequestException("Employee not found")
    ensure_can_access(actor, employee.location_id)

    if employee.verification_status != StaffVerificationStatus.VERIFIED:
        raise BadRequestException("Only verified staff members have payslips. Please verify staff first.")

    is_custom = bool(start_date and end_date)
    if start_date and end_date:
        if end_date < start_date:
            raise BadRequestException("End date cannot be earlier than start date")
        first = start_date
        last = end_date
        days = (end_date - start_date).days + 1
    elif start_date or end_date:
        raise BadRequestException("Provide both start_date and end_date")
    else:
        first, last, days = _month_bounds(month)

    # Auto-fill present days from this staff member's duties before reading.
    await payroll_service.materialize_from_assignments(
        first, last, [employee.location_id] if employee.location_id else None
    )
    data = await payroll_service.build_payslip(
        employee, first, last, days, is_custom_range=is_custom
    )
    return success_response(data=data)


@router.get("/payslips", summary="Payroll run (all staff, month or custom date range)")
async def get_payroll_run(
    month: Optional[str] = Query(None, description="YYYY-MM; defaults to this month."),
    start_date: Optional[dt.date] = Query(None, description="Custom start date (inclusive)"),
    end_date: Optional[dt.date] = Query(None, description="Custom end date (inclusive)"),
    location_id: Optional[str] = Query(None),
    actor: ActorContext = Depends(require_permission("payroll", "view")),
) -> dict:
    is_custom = bool(start_date and end_date)
    if start_date and end_date:
        if end_date < start_date:
            raise BadRequestException("End date cannot be earlier than start date")
        first = start_date
        last = end_date
        days = (end_date - start_date).days + 1
    elif start_date or end_date:
        raise BadRequestException("Provide both start_date and end_date")
    else:
        first, last, days = _month_bounds(month)

    if actor.is_scoped:
        location_ids = actor.location_ids
    elif location_id:
        location_ids = [location_id]
    else:
        location_ids = None
    # Auto-fill present days from duties across the run's scope before reading.
    await payroll_service.materialize_from_assignments(first, last, location_ids)
    data = await payroll_service.payroll_run(
        first, last, days, location_ids, is_custom_range=is_custom
    )
    return success_response(data=data)


# --------------------------------------------------------------------------
# Staff Leave Management
# --------------------------------------------------------------------------


async def _apply_leave_effects(leave: StaffLeave, emp: Employee, actor: ActorContext) -> None:
    """Sync an approved leave onto its Assignment break + AttendanceRecord(s)."""
    from app.api.v1.assignments.router import _recompute_assignment_totals

    end_d = leave.end_date or leave.leave_date
    asg: Optional[Assignment] = (
        await Assignment.get(leave.assignment_id) if leave.assignment_id else None
    )

    if leave.approval_status != LeaveApprovalStatus.APPROVED:
        if asg and leave.linked_break_id and asg.duty_breaks:
            asg.duty_breaks = [b for b in asg.duty_breaks if b.id != leave.linked_break_id]
            _recompute_assignment_totals(asg)
            asg.touch()
            await asg.save()
            leave.linked_break_id = None
            await leave.save()
            await payroll_service.sync_assignment_attendance(asg)
        return

    # Approved leave:
    note_prefix = "Paid Leave" if not leave.deduct_pay else "Leave"
    note_txt = f"{note_prefix}: {leave.leave_type.value} — {leave.reason or 'Staff leave'}"
    rate = (asg.employee_daily_rate if asg and asg.employee_daily_rate is not None else emp.daily_rate) or 0

    if asg is not None:
        break_id = leave.linked_break_id or f"lv-{str(leave.id)[-6:]}"
        if leave.deduct_pay:
            if asg.duty_breaks is None:
                asg.duty_breaks = []
            # Replace existing linked break if present
            asg.duty_breaks = [b for b in asg.duty_breaks if b.id != break_id]
            b_st = asg.start_time or "08:00"
            # Full-day break covering [leave_date..end_date] in IST (for 24h ends on end_date+1 at start_time)
            from app.models.enums import ShiftType
            brk_end_d = end_d + dt.timedelta(days=1) if asg.shift == ShiftType.FULL_24H else end_d
            brk_et = b_st if asg.shift == ShiftType.FULL_24H else (asg.end_time or "20:00")
            asg.duty_breaks.append(
                DutyBreak(
                    id=break_id,
                    start_date=leave.leave_date,
                    end_date=brk_end_d,
                    start_time=b_st,
                    end_time=brk_et,
                    reason=note_txt,
                )
            )
            leave.linked_break_id = break_id
            await leave.save()
            _recompute_assignment_totals(asg)
            asg.touch()
            await asg.save()
            await payroll_service.sync_assignment_attendance(asg)
        elif leave.linked_break_id and asg.duty_breaks:
            asg.duty_breaks = [b for b in asg.duty_breaks if b.id != leave.linked_break_id]
            _recompute_assignment_totals(asg)
            asg.touch()
            await asg.save()
            leave.linked_break_id = None
            await leave.save()

    # Upsert explicit AttendanceRecord with LEAVE status on each day [leave_date..end_date]
    d = leave.leave_date
    while d <= end_d:
        await _upsert_day(
            emp,
            d,
            status=AttendanceStatus.LEAVE,
            shift=asg.shift if asg else "full_24h",
            pay_rate=rate if not leave.deduct_pay else 0,
            customer_name=asg.customer_name if asg else leave.customer_name,
            note=note_txt,
            assignment_id=str(asg.id) if asg else None,
            start_time=asg.start_time if asg else None,
            end_time=asg.end_time if asg else None,
            actor=actor,
        )
        d += dt.timedelta(days=1)


@router.get("/leaves", summary="List staff leaves")
async def list_leaves(
    employee_id: Optional[str] = Query(None),
    assignment_id: Optional[str] = Query(None),
    approval_status: Optional[str] = Query(None),
    start_date: Optional[dt.date] = Query(None),
    end_date: Optional[dt.date] = Query(None),
    location_id: Optional[str] = Query(None),
    actor: ActorContext = Depends(require_permission("payroll", "view")),
) -> dict:
    filters: dict = actor.scope_filter()
    if employee_id:
        filters["employee_id"] = employee_id
    if assignment_id:
        filters["assignment_id"] = assignment_id
    if approval_status:
        filters["approval_status"] = approval_status
    if location_id and actor.can_access_location(location_id):
        filters["location_id"] = location_id
    if start_date or end_date:
        rng: dict = {}
        if start_date:
            rng["$gte"] = dt.datetime.combine(start_date, dt.time.min)
        if end_date:
            rng["$lte"] = dt.datetime.combine(end_date, dt.time.max)
        filters["leave_date"] = rng

    docs = await StaffLeave.find(filters).sort("-leave_date", "-created_at").to_list()
    items = [StaffLeaveResponse.model_validate(d).model_dump(mode="json") for d in docs]
    return success_response(data={"items": items, "total": len(items)})


@router.post("/leaves", status_code=201, summary="Record staff leave")
async def create_leave(
    payload: StaffLeaveCreate,
    actor: ActorContext = Depends(require_permission("payroll", "create")),
) -> dict:
    emp = await _employees.get(payload.employee_id)
    if emp is None:
        raise BadRequestException("Employee not found")
    ensure_can_access(actor, emp.location_id)

    end_d = payload.end_date or payload.leave_date
    if end_d < payload.leave_date:
        raise BadRequestException("End date cannot be earlier than leave date")

    asg = await Assignment.get(payload.assignment_id) if payload.assignment_id else None
    data = payload.model_dump(exclude_unset=True)
    data["end_date"] = end_d
    data["employee_name"] = emp.name
    data["location_id"] = (asg.location_id if asg else emp.location_id)
    if asg:
        data["customer_name"] = asg.customer_name
    if data.get("approval_status") == LeaveApprovalStatus.APPROVED and actor.user:
        data["approved_by"] = actor.user.name

    doc = await _leaves.create(data, actor)
    await _apply_leave_effects(doc, emp, actor)

    from app.services.dashboard_service import invalidate_dashboard_cache
    invalidate_dashboard_cache()
    return item_response(StaffLeaveResponse, doc, "Leave recorded")


@router.put("/leaves/{leave_id}", summary="Update leave status / details")
async def update_leave(
    leave_id: str,
    payload: StaffLeaveUpdate,
    actor: ActorContext = Depends(require_permission("payroll", "update")),
) -> dict:
    existing = await _leaves.get_or_404(leave_id)
    ensure_can_access(actor, existing.location_id)
    data = payload.model_dump(exclude_unset=True)
    if data.get("approval_status") == LeaveApprovalStatus.APPROVED and actor.user:
        data["approved_by"] = actor.user.name
    doc = await _leaves.update(leave_id, data, actor)
    emp = await _employees.get(doc.employee_id)
    if emp:
        await _apply_leave_effects(doc, emp, actor)
    from app.services.dashboard_service import invalidate_dashboard_cache
    invalidate_dashboard_cache()
    return item_response(StaffLeaveResponse, doc, "Leave updated")


@router.delete("/leaves/{leave_id}", summary="Delete leave record")
async def delete_leave(
    leave_id: str,
    actor: ActorContext = Depends(require_permission("payroll", "delete")),
) -> dict:
    existing = await _leaves.get_or_404(leave_id)
    ensure_can_access(actor, existing.location_id)

    if existing.assignment_id and existing.linked_break_id:
        asg = await Assignment.get(existing.assignment_id)
        if asg and asg.duty_breaks:
            from app.api.v1.assignments.router import _recompute_assignment_totals
            asg.duty_breaks = [b for b in asg.duty_breaks if b.id != existing.linked_break_id]
            _recompute_assignment_totals(asg)
            asg.touch()
            await asg.save()
            await payroll_service.sync_assignment_attendance(asg)

    end_d = existing.end_date or existing.leave_date
    await AttendanceRecord.get_motor_collection().delete_many(
        {
            "employee_id": existing.employee_id,
            "status": AttendanceStatus.LEAVE.value,
            "date": {
                "$gte": dt.datetime.combine(existing.leave_date, dt.time.min),
                "$lte": dt.datetime.combine(end_d, dt.time.max),
            },
        }
    )
    if existing.assignment_id:
        asg = await Assignment.get(existing.assignment_id)
        if asg:
            await payroll_service.sync_assignment_attendance(asg)

    await _leaves.delete(leave_id, actor)
    from app.services.dashboard_service import invalidate_dashboard_cache
    invalidate_dashboard_cache()
    return success_response(message="Leave deleted")
