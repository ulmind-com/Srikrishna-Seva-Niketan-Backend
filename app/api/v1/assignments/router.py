"""Assignment / daily-shift endpoints — staff engaged to customers."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.api.helpers import item_response, paginated_response
from app.core.exceptions import BadRequestException
from app.core.pagination import PaginationParams, pagination_params
from app.core.responses import success_response
from app.dependencies.auth import ActorContext, require_permission
from app.dependencies.scoping import ensure_can_access
from app.models.assignment import Assignment, DutyBreak, ShiftWindow
from app.models.attendance import AttendanceRecord
from app.models.customer import Customer
from app.models.employee import Employee
from app.models.enums import (
    AcceptanceStatus,
    AssignmentPlace,
    AssignmentStatus,
    AttendanceStatus,
    EmployeeStatus,
    NotificationType,
    PaymentMode,
    PaymentStatus,
    ShiftType,
    StaffVerificationStatus,
)
from app.models.location import Location
from app.repositories.base import BaseRepository
from app.schemas.assignment import (
    AssignmentCreate,
    AssignmentResponse,
    AssignmentUpdate,
    DutyBreakCreate,
    calculate_duty_days,
)
from app.services.crud import CrudService
from app.services.dashboard_service import invalidate_dashboard_cache
from app.services.email_service import email_service
from app.services.geo_service import geo_service
from app.services.notification_service import notification_service
from app.services.payroll_service import sync_assignment_attendance
from app.utils.bill_number import format_bill_no, next_receipt_no, next_sequential_bill_no
from app.utils.ist_time import now_ist, today_ist

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/assignments", tags=["Assignments"])

_assignments = CrudService(
    Assignment, entity="assignment",
    search_fields=("customer_name", "employee_name"),
)
_customers: BaseRepository[Customer] = BaseRepository(Customer)
_employees: BaseRepository[Employee] = BaseRepository(Employee)

# Statuses that keep a staff member occupied.
_OCCUPYING = {AssignmentStatus.SCHEDULED, AssignmentStatus.ACTIVE}


async def _set_employee_status(employee_id: str, status: EmployeeStatus) -> None:
    emp = await _employees.get(employee_id)
    if emp is not None and emp.status != status:
        emp.status = status
        emp.touch()
        await emp.save()


_PLACE_LABEL = {
    "home": "Customer's Home",
    "hospital": "Hospital",
    "nursing_home": "Nursing Home",
    "other": "Other",
}


def _place_text(doc: Assignment) -> str:
    """Readable posting place, e.g. 'Hospital — Apollo Gleneagles'."""
    label = _PLACE_LABEL.get(str(doc.place_type), str(doc.place_type))
    return f"{label} — {doc.place_name}" if doc.place_name else label


def _apply_duration(data: dict, start: Optional[dt.date]) -> dict:
    """Turn a ``duration_days`` input into a concrete ``end_date``.

    ``duration_days`` is a convenience for the admin ("assign for 10 days")
    and is never stored — the engagement's end date is the source of truth.
    An explicit ``end_date`` always wins.
    """
    days = data.pop("duration_days", None)
    if days and not data.get("end_date") and start is not None:
        data["end_date"] = start + dt.timedelta(days=days - 1)
    return data


def _sync_shift_times(data: dict) -> dict:
    """Mirror the first custom shift window into start_time/end_time (compat)."""
    windows = data.get("shift_windows")
    if windows:
        first = windows[0]
        start = first.get("start") if isinstance(first, dict) else getattr(first, "start", None)
        end = first.get("end") if isinstance(first, dict) else getattr(first, "end", None)
        data["start_time"] = start
        data["end_time"] = end
    return data


def _derive_amounts(
    data: dict,
    start: Optional[dt.date],
    end: Optional[dt.date],
    existing_cdr: Optional[int] = None,
    existing_edr: Optional[int] = None,
    days_worked: Optional[float] = None,
    existing: Optional[Assignment] = None,
) -> dict:
    """Set stored totals from the per-day rates: total = daily_rate × total_days.

    Supports fractional days (e.g. 14.5 days) calculated via calculate_duty_days.
    Only runs when the engagement has a concrete end_date (a closed span). An
    explicit ``customer_rate`` / ``employee_payout`` in the payload always wins.
    Open-ended engagements keep null totals and are billed per calendar month.
    """
    if not start:
        return data
    if not end:
        # Clearing end_date on edit turns a closed engagement back into ongoing.
        if existing is not None and "end_date" in data and data["end_date"] is None:
            data.setdefault("days_worked", None)
            data.setdefault("customer_rate", None)
            data.setdefault("employee_payout", None)
            if data.get("daily_other_expense", existing.daily_other_expense) is not None:
                data.setdefault("other_expense", None)
        return data

    if days_worked is not None and days_worked >= 0:
        total_days = days_worked
    elif "days_worked" in data and data["days_worked"] is not None:
        total_days = float(data["days_worked"])
    else:
        st = data.get("start_time") if "start_time" in data else (existing.start_time if existing else None)
        et = data.get("end_time") if "end_time" in data else (existing.end_time if existing else None)
        shift = data.get("shift") if "shift" in data else (existing.shift if existing else ShiftType.FULL_24H)
        windows = data.get("shift_windows") if "shift_windows" in data else (existing.shift_windows if existing else None)
        breaks = data.get("duty_breaks") if "duty_breaks" in data else (existing.duty_breaks if existing else None)
        status_val = data.get("status") if "status" in data else (existing.status if existing else None)
        total_days = calculate_duty_days(
            start,
            end,
            st,
            et,
            shift,
            breaks,
            windows,
            is_handover=(status_val == AssignmentStatus.REPLACED),
        )
        if "days_worked" not in data:
            data["days_worked"] = total_days

    if total_days < 0:
        return data
    cdr = data.get("customer_daily_rate", existing_cdr)
    edr = data.get("employee_daily_rate", existing_edr)
    doexp = data.get(
        "daily_other_expense",
        existing.daily_other_expense if existing else None,
    )
    contract_amt = data.get(
        "contract_amount",
        existing.contract_amount if existing else None,
    )
    if cdr is not None:
        data["customer_rate"] = int(round(cdr * total_days))
    elif contract_amt is not None and "customer_rate" not in data:
        data["customer_rate"] = int(contract_amt)
    if edr is not None:
        data["employee_payout"] = int(round(edr * total_days))
    if doexp is not None:
        data["other_expense"] = int(round(doexp * total_days))
    return data


def _recompute_assignment_totals(asg: Assignment) -> float:
    """Recompute days_worked, customer_rate, and employee_payout on an Assignment."""
    if not asg.end_date or asg.end_date < asg.start_date:
        duty_days = 0.0
    else:
        duty_days = calculate_duty_days(
            asg.start_date,
            asg.end_date,
            asg.start_time,
            asg.end_time,
            asg.shift,
            asg.duty_breaks,
            asg.shift_windows,
            is_handover=(asg.status == AssignmentStatus.REPLACED),
        )
    asg.days_worked = duty_days
    if asg.customer_daily_rate is not None:
        asg.customer_rate = int(round(asg.customer_daily_rate * duty_days))
    if asg.employee_daily_rate is not None:
        asg.employee_payout = int(round(asg.employee_daily_rate * duty_days))
    if asg.daily_other_expense is not None:
        asg.other_expense = int(round(asg.daily_other_expense * duty_days))
    return duty_days


async def _prune_attendance_outside_span(
    assignment_id: str,
    start_date: dt.date,
    end_date: Optional[dt.date],
) -> None:
    """Remove attendance logs that fall outside an edited assignment's date range."""
    try:
        or_clauses: list[dict] = [
            {"date": {"$lt": dt.datetime.combine(start_date, dt.time.min)}},
        ]
        if end_date is not None:
            or_clauses.append({"date": {"$gt": dt.datetime.combine(end_date, dt.time.max)}})
        await AttendanceRecord.get_motor_collection().delete_many(
            {"assignment_id": assignment_id, "$or": or_clauses}
        )
    except Exception as exc:
        logger.warning("Failed to prune attendance outside updated span for %s: %s", assignment_id, exc)


async def _sync_replacement_chain(
    doc: Assignment,
    prev_snapshot: Assignment,
) -> None:
    """Keep handover dates/times and totals synced between replaced and replacement assignments."""
    # 1. Editing a replacement assignment's start_date / start_time syncs back to predecessor's end.
    start_changed = (
        doc.start_date != prev_snapshot.start_date
        or doc.start_time != prev_snapshot.start_time
    )
    if doc.is_replacement and start_changed:
        pred: Optional[Assignment] = None
        if doc.replacement_for_assignment_id:
            pred = await Assignment.get(doc.replacement_for_assignment_id)
        if pred is None and doc.replacement_for_employee_id:
            pred = (
                await Assignment.find(
                    {
                        "customer_id": doc.customer_id,
                        "employee_id": doc.replacement_for_employee_id,
                        "replaced_by_employee_id": doc.employee_id,
                        "status": {"$in": [AssignmentStatus.REPLACED.value, AssignmentStatus.CANCELLED.value]},
                    }
                )
                .sort("-updated_at")
                .first_or_none()
            )
            if pred is not None:
                doc.replacement_for_assignment_id = str(pred.id)
                doc.touch()
                await doc.save()

        if pred is not None and str(pred.id) != str(doc.id):
            pred.end_date = doc.start_date
            if doc.start_time:
                pred.end_time = doc.start_time
            if pred.end_date < pred.start_date:
                pred.status = AssignmentStatus.CANCELLED
                pred.days_worked = 0.0
                if pred.customer_daily_rate is not None:
                    pred.customer_rate = 0
                if pred.employee_daily_rate is not None:
                    pred.employee_payout = 0
                if pred.daily_other_expense is not None:
                    pred.other_expense = 0
            else:
                if pred.status == AssignmentStatus.CANCELLED:
                    pred.status = AssignmentStatus.REPLACED
                _recompute_assignment_totals(pred)
            pred.touch()
            await pred.save()
            await sync_assignment_attendance(pred)

        await sync_assignment_attendance(doc)

    # 2. Editing a replaced predecessor's end_date / end_time syncs forward to successor's start.
    end_changed = (
        doc.end_date != prev_snapshot.end_date
        or doc.end_time != prev_snapshot.end_time
    )
    if doc.replaced_by_employee_id and end_changed and doc.end_date is not None:
        succ = await Assignment.find(
            {"replacement_for_assignment_id": str(doc.id)}
        ).first_or_none()
        if succ is None:
            succ = (
                await Assignment.find(
                    {
                        "customer_id": doc.customer_id,
                        "employee_id": doc.replaced_by_employee_id,
                        "is_replacement": True,
                    }
                )
                .sort("-created_at")
                .first_or_none()
            )
        if succ is not None and str(succ.id) != str(doc.id):
            succ.start_date = doc.end_date
            if doc.end_time:
                succ.start_time = doc.end_time
            if succ.end_date is not None:
                _recompute_assignment_totals(succ)
            succ.touch()
            await succ.save()
            await sync_assignment_attendance(succ)


async def _check_employee_conflict(
    employee_id: str,
    start_date: dt.date,
    end_date: Optional[dt.date],
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    exclude_assignment_id: Optional[str] = None,
) -> Optional[Assignment]:
    """Check if the employee already has an active/scheduled assignment during the given dates."""
    query: dict = {
        "employee_id": employee_id,
        "status": {"$in": [AssignmentStatus.SCHEDULED.value, AssignmentStatus.ACTIVE.value]},
    }
    if exclude_assignment_id:
        from bson import ObjectId
        from bson.errors import InvalidId
        try:
            query["_id"] = {"$ne": ObjectId(exclude_assignment_id)}
        except (InvalidId, TypeError):
            pass

    existing_list = await Assignment.find(query).to_list()
    target_end = end_date or dt.date.max

    for a in existing_list:
        a_end = a.end_date or dt.date.max
        # No overlap if one ends before the other starts
        if start_date > a_end or a.start_date > target_end:
            continue

        # Check boundary edge cases if on the exact same date
        if start_date == a_end and a_end != dt.date.max:
            if start_time and a.end_time and a.end_time <= start_time:
                continue
        if target_end == a.start_date and target_end != dt.date.max:
            if end_time and a.start_time and end_time <= a.start_time:
                continue

        return a

    return None


@router.get("", summary="List assignments")
async def list_assignments(
    params: PaginationParams = Depends(pagination_params),
    status: Optional[str] = Query(None),
    employee_id: Optional[str] = Query(None),
    customer_id: Optional[str] = Query(None),
    location_id: Optional[str] = Query(None),
    active_on: Optional[dt.date] = Query(
        None, description="Only engagements active on this date (daily shift view)."
    ),
    actor: ActorContext = Depends(require_permission("assignments", "view")),
) -> dict:
    filters: dict = actor.scope_filter()
    if status is not None:
        filters["status"] = status
    if employee_id is not None:
        filters["employee_id"] = employee_id
    if customer_id is not None:
        filters["customer_id"] = customer_id
    if location_id is not None and actor.can_access_location(location_id):
        filters["location_id"] = location_id
    if active_on is not None:
        active_dt = dt.datetime.combine(active_on, dt.time.min)
        filters["start_date"] = {"$lte": active_dt}
        filters["$or"] = [{"end_date": None}, {"end_date": {"$gte": active_dt}}]
    items, total = await _assignments.paginate(
        page=params.page, page_size=params.page_size, search=params.search,
        sort_by=params.sort_by or "start_date", sort_order=params.sort_direction,
        filters=filters or None,
    )
    return paginated_response(AssignmentResponse, items, total, params)


@router.get("/daily", summary="Daily shift roster")
async def daily_roster(
    date: Optional[dt.date] = Query(None, description="Defaults to today."),
    actor: ActorContext = Depends(require_permission("assignments", "view")),
) -> dict:
    """Every shift running on one day, grouped by branch.

    Answers "who is on duty today, on which shift, and where" — the register a
    manager reads each morning. Scoped like the rest of the module, so a branch
    manager only gets their own branches.
    """
    day = date or today_ist()
    day_dt = dt.datetime.combine(day, dt.time.min)

    filters: dict = actor.scope_filter()
    filters["status"] = {"$in": [s.value for s in _OCCUPYING]}
    filters["start_date"] = {"$lte": day_dt}
    filters["$or"] = [{"end_date": None}, {"end_date": {"$gte": day_dt}}]

    items = await Assignment.find(filters).to_list()
    items.sort(key=lambda a: (a.employee_name or "").lower())

    locations = {str(loc.id): loc for loc in await Location.find({}).to_list()}

    shift_counts: dict[str, int] = {}
    groups: dict[str, dict] = {}
    for a in items:
        shift_counts[a.shift.value] = shift_counts.get(a.shift.value, 0) + 1
        group = groups.get(a.location_id)
        if group is None:
            loc = locations.get(a.location_id)
            group = {
                "location_id": a.location_id,
                "location_name": loc.name if loc else "Unknown branch",
                "location_code": loc.code if loc else "—",
                "city": loc.city if loc else None,
                "_order": loc.order if loc else 9999,
                "count": 0,
                "assignments": [],
            }
            groups[a.location_id] = group
        group["count"] += 1
        group["assignments"].append(
            AssignmentResponse.model_validate(a).model_dump(mode="json")
        )

    branches = sorted(groups.values(), key=lambda g: (g.pop("_order"), g["location_name"]))

    return success_response(
        data={
            "date": day.isoformat(),
            "totals": {
                "on_duty": len(items),
                "branches": len(branches),
                "by_shift": shift_counts,
            },
            "branches": branches,
        }
    )


@router.get("/deployment", summary="Branch-wise staff deployment overview")
async def deployment_overview(
    actor: ActorContext = Depends(require_permission("assignments", "view")),
) -> dict:
    """Who is deployed, who is free — per branch.

    Super-admins see every branch; a branch-scoped manager sees only their own,
    so the same screen answers "how many of my staff are out, how many are left,
    and how many days until each one is back".
    """
    today = dt.datetime.combine(today_ist(), dt.time.min)

    locations = await Location.find({"is_active": True}).sort("order").to_list()
    if actor.is_scoped:
        locations = [loc for loc in locations if str(loc.id) in actor.location_ids]

    branches: list[dict] = []
    for loc in locations:
        loc_id = str(loc.id)

        employees = await Employee.find(
            {"location_id": loc_id, "is_active": True}
        ).to_list()

        # Engagements that still occupy someone today (or start later).
        running = await Assignment.find(
            {
                "location_id": loc_id,
                "status": {"$in": [s.value for s in _OCCUPYING]},
                "$or": [{"end_date": None}, {"end_date": {"$gte": today}}],
            }
        ).to_list()
        running.sort(key=lambda a: (a.end_date is None, a.end_date or dt.date.max))

        busy_ids = {a.employee_id for a in running}
        free = [e for e in employees if str(e.id) not in busy_ids]

        branches.append(
            {
                "location_id": loc_id,
                "location_name": loc.name,
                "location_code": loc.code,
                "city": loc.city,
                "total_staff": len(employees),
                "assigned_count": len(busy_ids),
                "available_count": len(free),
                "assignments": [
                    AssignmentResponse.model_validate(a).model_dump(mode="json")
                    for a in running
                ],
                "available_staff": [
                    {
                        "id": str(e.id),
                        "name": e.name,
                        "code": e.code,
                        "category": e.category,
                        "phone": e.phone,
                        "alt_phone": e.alt_phone,
                        "address": e.address,
                        "city": e.city,
                        "pincode": e.pincode,
                        "photo": e.photo.model_dump(mode="json") if e.photo else None,
                    }
                    for e in sorted(free, key=lambda e: e.name.lower())
                ],
            }
        )

    totals = {
        "total_staff": sum(b["total_staff"] for b in branches),
        "assigned_count": sum(b["assigned_count"] for b in branches),
        "available_count": sum(b["available_count"] for b in branches),
        "branches": len(branches),
    }
    return success_response(data={"totals": totals, "branches": branches})


@router.get("/{assignment_id}", summary="Get assignment")
async def get_assignment(
    assignment_id: str,
    actor: ActorContext = Depends(require_permission("assignments", "view")),
) -> dict:
    doc = await _assignments.get_or_404(assignment_id)
    ensure_can_access(actor, doc.location_id)
    return item_response(AssignmentResponse, doc)


@router.post("", status_code=201, summary="Assign staff to customer")
async def create_assignment(
    payload: AssignmentCreate,
    actor: ActorContext = Depends(require_permission("assignments", "create")),
) -> dict:
    customer = await _customers.get(payload.customer_id)
    if customer is None:
        raise BadRequestException("Customer not found")
    employee = await _employees.get(payload.employee_id)
    if employee is None:
        raise BadRequestException("Employee not found")
    if employee.verification_status != StaffVerificationStatus.VERIFIED:
        raise BadRequestException("Cannot assign staff whose verification is pending or not verified. Please verify staff first.")
    # Branch auto-alignment: ensure customer and staff are aligned to a valid branch
    if not customer.location_id and employee.location_id:
        customer.location_id = employee.location_id
        await customer.save()
    elif not employee.location_id and customer.location_id:
        employee.location_id = customer.location_id
        await employee.save()
    elif (
        customer.location_id
        and employee.location_id
        and customer.location_id != employee.location_id
    ):
        if not actor.is_scoped or employee.location_id in actor.location_ids:
            customer.location_id = employee.location_id
            await customer.save()
        else:
            raise BadRequestException("Staff and customer must belong to the same branch")

    branch_id = customer.location_id or employee.location_id
    if not branch_id:
        from app.models.location import Location
        first_loc = await Location.find_one({"is_active": True})
        if first_loc:
            branch_id = str(first_loc.id)
            customer.location_id = branch_id
            await customer.save()

    if branch_id:
        ensure_can_access(actor, branch_id)

    data = payload.model_dump(exclude_unset=True)
    data = _apply_duration(data, payload.start_date)
    data = _sync_shift_times(data)
    if data.get("status") == AssignmentStatus.COMPLETED and not data.get("end_date"):
        windows = data.get("shift_windows")
        if windows and len(windows) > 1:
            data["end_date"] = payload.start_date + dt.timedelta(days=len(windows) - 1)
        elif data.get("end_time") and data.get("start_time") and data["end_time"] <= data["start_time"]:
            data["end_date"] = payload.start_date + dt.timedelta(days=1)
        else:
            data["end_date"] = payload.start_date
    data["location_id"] = branch_id
    data["customer_name"] = customer.name
    data["employee_name"] = employee.name
    # Default the per-day rates from the linked records, then derive the stored
    # totals (customer_rate / employee_payout) as daily_rate × total_days.
    data.setdefault("customer_daily_rate", customer.billing_rate)
    data.setdefault("employee_daily_rate", employee.daily_rate)
    _derive_amounts(data, data.get("start_date"), data.get("end_date"))

    # Snapshot the destination point so routing stays stable:
    # 1. Explicit admin pin on the assignment takes first priority
    # 2. Customer's pinned coords if set
    # 3. Geocode of the posting / customer address (best-effort, max 1.5s timeout)
    if data.get("dest_lat") is not None and data.get("dest_lng") is not None:
        pass  # Admin confirmed coordinates supplied in payload
    elif customer.lat is not None and customer.lng is not None:
        data["dest_lat"], data["dest_lng"] = customer.lat, customer.lng
    else:
        try:
            coords = await asyncio.wait_for(
                geo_service.geocode(data.get("address") or customer.address or ""),
                timeout=1.5,
            )
            if coords is not None:
                data["dest_lat"], data["dest_lng"] = coords
        except Exception:
            pass

    # Check for overlapping assignments (prevent double-booking staff)
    conflict = await _check_employee_conflict(
        payload.employee_id,
        payload.start_date,
        data.get("end_date"),
        data.get("start_time"),
        data.get("end_time"),
    )
    if conflict:
        c_end = conflict.end_date.isoformat() if conflict.end_date else "Ongoing"
        raise BadRequestException(
            f"{employee.name} is already assigned/on-duty for {conflict.customer_name} from {conflict.start_date.isoformat()} to {c_end}."
        )

    doc = await _assignments.create(data, actor)

    # Mark the staff member occupied.
    if doc.status in _OCCUPYING:
        await _set_employee_status(str(employee.id), EmployeeStatus.ASSIGNED)

    # Background task for portal notification, push notification, and transactional email
    # so the HTTP response returns immediately (<100ms) to the admin.
    async def _post_assignment_dispatches():
        try:
            await notification_service.create(
                title="New duty assignment",
                message=f"You are assigned to {customer.name} ({_place_text(doc)}, {doc.shift} shift) from {doc.start_date.isoformat()}.",
                type=NotificationType.ASSIGNMENT,
                employee_id=str(employee.id),
                reference_id=str(doc.id),
            )
        except Exception as err:
            logger.warning("Assignment notification dispatch note: %s", err)

        if employee.email:
            try:
                await email_service.send_assignment_notice(
                    to=employee.email,
                    staff_name=employee.name,
                    customer_name=customer.name,
                    shift=str(doc.shift),
                    place=_place_text(doc),
                    start_date=doc.start_date.isoformat(),
                    end_date=doc.end_date.isoformat() if doc.end_date else "Ongoing",
                )
            except Exception as err:
                logger.warning("Assignment email notice note: %s", err)

    asyncio.create_task(_post_assignment_dispatches())

    from app.services.dashboard_service import invalidate_dashboard_cache
    invalidate_dashboard_cache()

    return item_response(AssignmentResponse, doc, "Staff assigned")


@router.put("/{assignment_id}", summary="Update assignment")
async def update_assignment(
    assignment_id: str,
    payload: AssignmentUpdate,
    actor: ActorContext = Depends(require_permission("assignments", "update")),
) -> dict:
    existing = await _assignments.get_or_404(assignment_id)
    ensure_can_access(actor, existing.location_id)
    data = payload.model_dump(exclude_unset=True)
    data = _apply_duration(data, data.get("start_date") or existing.start_date)
    if "shift_windows" in data:
        data = _sync_shift_times(data)
    start_d = data.get("start_date") or existing.start_date
    current_status = data.get("status") or existing.status
    if current_status == AssignmentStatus.COMPLETED and not data.get("end_date") and not existing.end_date:
        windows = data.get("shift_windows") or existing.shift_windows
        if windows and len(windows) > 1:
            data["end_date"] = start_d + dt.timedelta(days=len(windows) - 1)
        elif (data.get("end_time") or existing.end_time) and (data.get("start_time") or existing.start_time) and (data.get("end_time") or existing.end_time) <= (data.get("start_time") or existing.start_time):
            data["end_date"] = start_d + dt.timedelta(days=1)
        else:
            data["end_date"] = start_d
    prev_schedule = type(
        "_ScheduleSnapshot",
        (),
        {
            "start_date": existing.start_date,
            "start_time": existing.start_time,
            "end_date": existing.end_date,
            "end_time": existing.end_time,
        },
    )()
    # Recompute stored totals when the dates, times, shift, or per-day rates change.
    _derive_amounts(
        data,
        data.get("start_date") or existing.start_date,
        data.get("end_date") if "end_date" in data else existing.end_date,
        existing.customer_daily_rate,
        existing.employee_daily_rate,
        existing=existing,
    )
    # Re-geocode if the posting address changed and no explicit pin was given
    if "address" in data and "dest_lat" not in data and data["address"] != existing.address:
        coords = await geo_service.geocode(data["address"] or "")
        if coords is not None:
            data["dest_lat"], data["dest_lng"] = coords
    # Validate conflict if employee or dates changed and assignment is active/scheduled
    target_emp_id = data.get("employee_id") or existing.employee_id
    if current_status in _OCCUPYING and ("employee_id" in data or "start_date" in data or "end_date" in data):
        conflict = await _check_employee_conflict(
            target_emp_id,
            start_d,
            data.get("end_date") if "end_date" in data else existing.end_date,
            data.get("start_time") or existing.start_time,
            data.get("end_time") or existing.end_time,
            exclude_assignment_id=assignment_id,
        )
        if conflict:
            c_end = conflict.end_date.isoformat() if conflict.end_date else "Ongoing"
            raise BadRequestException(
                f"Staff member is already assigned/on-duty for {conflict.customer_name} from {conflict.start_date.isoformat()} to {c_end}."
            )

    doc = await _assignments.update(assignment_id, data, actor)
    await _sync_replacement_chain(doc, prev_schedule)
    await sync_assignment_attendance(doc)

    # Keep the staff member's availability in sync + tell them about status changes.
    if "status" in data:
        if doc.status in _OCCUPYING:
            await _set_employee_status(doc.employee_id, EmployeeStatus.ASSIGNED)
        elif doc.status in (AssignmentStatus.COMPLETED, AssignmentStatus.CANCELLED):
            await _set_employee_status(doc.employee_id, EmployeeStatus.AVAILABLE)
            if doc.status == AssignmentStatus.CANCELLED:
                try:
                    from app.models.attendance import AttendanceRecord
                    await AttendanceRecord.get_motor_collection().delete_many({"assignment_id": assignment_id})
                except Exception:
                    pass
        await notification_service.create(
            title="Assignment updated",
            message=f"Your assignment with {doc.customer_name} is now {doc.status}.",
            type=NotificationType.ASSIGNMENT,
            employee_id=doc.employee_id,
            reference_id=str(doc.id),
        )
    from app.services.dashboard_service import invalidate_dashboard_cache
    invalidate_dashboard_cache()
    return item_response(AssignmentResponse, doc, "Assignment updated")


async def _next_bill_no() -> int:
    """Atomically allocate the next sequential customer-bill number (001, 002, ...)."""
    try:
        from app.database.connection import get_database
        db = get_database()
    except Exception:
        db = Assignment.get_motor_collection().database

    return await next_sequential_bill_no(db, "assignment")


@router.post("/{assignment_id}/issue-bill", summary="Allocate this engagement's bill number")
async def issue_bill(
    assignment_id: str,
    actor: ActorContext = Depends(require_permission("assignments", "view")),
) -> dict:
    """Return the engagement's customer-bill number, allocating one on first use.

    Idempotent: once a bill number is assigned it is reused, so re-opening or
    re-printing the bill always shows the same number.
    """
    doc = await _assignments.get_or_404(assignment_id)
    ensure_can_access(actor, doc.location_id)
    if doc.end_date is None and doc.status not in (
        AssignmentStatus.COMPLETED,
        AssignmentStatus.REPLACED,
    ):
        raise BadRequestException(
            "Bill cannot be generated yet — set an end date or mark the work completed first."
        )
    if doc.bill_no is None:
        doc.bill_no = await _next_bill_no()
        doc.touch()
        await doc.save()
    return success_response(data={"bill_no": doc.bill_no, "bill_no_formatted": format_bill_no(doc.bill_no)})


class AdminCompleteAssignmentRequest(BaseModel):
    cash_collected: bool = True
    cash_amount: Optional[int] = None
    note: Optional[str] = None
    received_by: Optional[str] = None


@router.post("/{assignment_id}/complete", summary="Super Admin mark duty completed and record cash collection")
async def admin_complete_assignment(
    assignment_id: str,
    payload: AdminCompleteAssignmentRequest = AdminCompleteAssignmentRequest(),
    actor: ActorContext = Depends(require_permission("assignments", "update")),
) -> dict:
    """Super Admin mark an assignment completed and record cash receipt.

    Designed for staff who use keypad phones or cannot use the mobile app.
    Sets status=COMPLETED, work_completed_at, records cash receipt if postpaid
    or cash_collected=True, inserts a Payment entry in the finance ledger,
    frees the staff member, and triggers real-time updates.
    """
    existing = await _assignments.get_or_404(assignment_id)
    ensure_can_access(actor, existing.location_id)

    now_utc = dt.datetime.now(dt.timezone.utc)
    now_local = now_ist()
    today = now_local.date()

    existing.status = AssignmentStatus.COMPLETED
    existing.acceptance_status = AcceptanceStatus.ACCEPTED
    existing.work_completed_at = now_utc

    if existing.end_date is None:
        existing.end_date = today
        if not existing.end_time:
            existing.end_time = now_local.strftime("%H:%M")

    eff_days = _recompute_assignment_totals(existing)

    ptype = str(getattr(existing, "payment_type", "prepaid"))
    is_postpaid = (ptype == "postpaid")
    amount = payload.cash_amount if payload.cash_amount is not None else int(existing.customer_rate or 0)
    actor_name = actor.user.name if (hasattr(actor, "user") and actor.user and actor.user.name) else "Super Admin"
    emp_name = existing.employee_name or "Staff"

    if is_postpaid or payload.cash_collected:
        existing.payment_status = PaymentStatus.PAID
        existing.payment_received_at = now_utc
        existing.payment_received_by = payload.received_by or f"Super Admin ({actor_name}) on behalf of {emp_name}"
        if amount > 0:
            try:
                from app.models.finance import Payment
                db = existing.get_motor_collection().database
                pay = Payment(
                    receipt_no=await next_receipt_no(db),
                    customer_id=existing.customer_id,
                    customer_name=existing.customer_name,
                    assignment_id=str(existing.id),
                    bill_no=existing.bill_no,
                    location_id=existing.location_id,
                    amount=amount,
                    mode=PaymentMode.CASH,
                    status=PaymentStatus.PAID,
                    received_by=actor_name,
                    date=today,
                    note=payload.note or f"Cash recorded by {actor_name} for duty by {emp_name} (Patient: {existing.customer_name})",
                )
                await pay.insert()
            except Exception as e:
                logger.warning("Payment insert error: %s", e)

    if payload.note:
        suffix = f"Admin update: {payload.note}"
        existing.notes = f"{existing.notes} | {suffix}" if existing.notes else suffix

    existing.touch()
    await existing.save()
    await sync_assignment_attendance(existing)

    # Free staff member
    await _set_employee_status(existing.employee_id, EmployeeStatus.AVAILABLE)

    # Sync booking status to COMPLETED if linked
    try:
        from app.models.booking import Booking, BookingStatus
        b = await Booking.find_one({"customer_id": existing.customer_id})
        if b:
            b.status = BookingStatus.COMPLETED
            await b.save()
    except Exception:
        pass

    from app.services.dashboard_service import invalidate_dashboard_cache
    invalidate_dashboard_cache()

    await notification_service.create(
        title="Duty Completed & Settled",
        message=f"Admin marked duty for {existing.customer_name} ({emp_name}) as completed. Cash ₹{amount} settled.",
        type=NotificationType.ASSIGNMENT,
        employee_id=existing.employee_id,
        reference_id=str(existing.id),
    )

    return item_response(AssignmentResponse, existing, "Duty marked as completed and cash collection recorded")


class ReplaceStaffRequest(BaseModel):
    new_employee_id: str
    effective_date: dt.date
    end_date: Optional[dt.date] = None
    shift: Optional[ShiftType] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    shift_windows: Optional[list[ShiftWindow]] = None
    reason: Optional[str] = None
    customer_daily_rate: Optional[int] = None
    employee_daily_rate: Optional[int] = None
    daily_other_expense: Optional[int] = None
    contract_amount: Optional[int] = None
    notes: Optional[str] = None


class BatchReplaceItem(BaseModel):
    assignment_id: str
    new_employee_id: str
    effective_date: dt.date
    end_date: Optional[dt.date] = None
    shift: Optional[ShiftType] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    shift_windows: Optional[list[ShiftWindow]] = None
    reason: Optional[str] = None
    customer_daily_rate: Optional[int] = None
    employee_daily_rate: Optional[int] = None
    daily_other_expense: Optional[int] = None
    contract_amount: Optional[int] = None
    notes: Optional[str] = None


class BatchReplaceRequest(BaseModel):
    replacements: list[BatchReplaceItem]


class MultiStaffItem(BaseModel):
    employee_id: str
    shift: ShiftType = ShiftType.FULL_24H
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    customer_daily_rate: Optional[int] = None
    employee_daily_rate: Optional[int] = None
    daily_other_expense: Optional[int] = None
    contract_amount: Optional[int] = None
    notes: Optional[str] = None


class MultiStaffAssignmentRequest(BaseModel):
    customer_id: str
    location_id: Optional[str] = None
    place_type: AssignmentPlace = AssignmentPlace.HOME
    place_name: Optional[str] = None
    purpose: Optional[str] = None
    address: Optional[str] = None
    dest_lat: Optional[float] = None
    dest_lng: Optional[float] = None
    start_date: dt.date
    end_date: Optional[dt.date] = None
    duration_days: Optional[int] = None
    customer_daily_rate: Optional[int] = None
    employee_daily_rate: Optional[int] = None
    daily_other_expense: Optional[int] = None
    contract_amount: Optional[int] = None
    payment_type: Optional[str] = "prepaid"
    notes: Optional[str] = None
    staff_list: list[MultiStaffItem]


async def _perform_staff_replacement(
    assignment_id: str,
    payload: ReplaceStaffRequest,
    actor: ActorContext,
) -> tuple[Assignment, Assignment]:
    existing = await _assignments.get_or_404(assignment_id)
    ensure_can_access(actor, existing.location_id)

    if payload.effective_date < existing.start_date:
        raise BadRequestException(
            f"Replacement date ({payload.effective_date}) cannot be before current start date ({existing.start_date})"
        )

    new_emp = await _employees.get(payload.new_employee_id)
    if not new_emp:
        raise BadRequestException("Replacement employee not found")
    if new_emp.verification_status != StaffVerificationStatus.VERIFIED:
        raise BadRequestException("Replacement staff member is not verified. Please select a verified staff member.")
    if str(new_emp.id) == existing.employee_id:
        raise BadRequestException("Replacement staff must be different from current staff")

    orig_end_date = existing.end_date
    today = today_ist()

    # 1. Update previous staff's assignment
    if payload.effective_date == existing.start_date:
        existing.status = AssignmentStatus.CANCELLED
        existing.replaced_by_employee_id = str(new_emp.id)
        existing.replaced_by_employee_name = new_emp.name
        existing.replacement_reason = payload.reason
        existing.days_worked = 0
        note_suffix = f"Replaced by {new_emp.name} before starting. Reason: {payload.reason or 'Not specified'}"
        existing.notes = f"{existing.notes} | {note_suffix}" if existing.notes else note_suffix
        existing.touch()
        await existing.save()
        await sync_assignment_attendance(existing)
        await _set_employee_status(existing.employee_id, EmployeeStatus.AVAILABLE)
    else:
        old_end_date = min(existing.end_date, payload.effective_date) if existing.end_date else payload.effective_date
        existing.end_date = old_end_date
        existing.replaced_by_employee_id = str(new_emp.id)
        existing.replaced_by_employee_name = new_emp.name
        existing.replacement_reason = payload.reason
        if payload.start_time:
            existing.end_time = payload.start_time

        existing.status = AssignmentStatus.REPLACED
        duty_days = _recompute_assignment_totals(existing)
        if old_end_date <= today:
            await _set_employee_status(existing.employee_id, EmployeeStatus.AVAILABLE)

        note_suffix = f"Handed over to {new_emp.name} on {payload.effective_date}. Reason: {payload.reason or 'Staff emergency/leave'}"
        existing.notes = f"{existing.notes} | {note_suffix}" if existing.notes else note_suffix
        existing.touch()
        await existing.save()
        await sync_assignment_attendance(existing)

    # 2. Create the new replacement assignment
    cdr = payload.customer_daily_rate if payload.customer_daily_rate is not None else existing.customer_daily_rate
    edr = payload.employee_daily_rate if payload.employee_daily_rate is not None else (new_emp.daily_rate or existing.employee_daily_rate)

    target_shift = payload.shift or existing.shift
    target_start_time = payload.start_time if payload.start_time is not None else existing.start_time
    target_end_time = payload.end_time if payload.end_time is not None else existing.end_time
    target_end_date = payload.end_date if "end_date" in payload.model_fields_set else orig_end_date
    target_windows = None
    if payload.shift_windows is not None:
        target_windows = [w.model_dump() for w in payload.shift_windows if w.start or w.end]
        if target_windows:
            target_shift = ShiftType.CUSTOM
            target_start_time = target_windows[0].get("start") or target_start_time
            target_end_time = target_windows[-1].get("end") or target_end_time
        else:
            target_windows = None

    new_data: dict = {
        "customer_id": existing.customer_id,
        "customer_name": existing.customer_name,
        "employee_id": str(new_emp.id),
        "employee_name": new_emp.name,
        "location_id": existing.location_id,
        "place_type": existing.place_type,
        "place_name": existing.place_name,
        "purpose": existing.purpose,
        "address": existing.address,
        "dest_lat": existing.dest_lat,
        "dest_lng": existing.dest_lng,
        "shift": target_shift,
        "start_time": target_start_time,
        "end_time": target_end_time,
        "shift_windows": target_windows if target_windows is not None else ([w.model_dump() for w in existing.shift_windows] if existing.shift_windows else None),
        "start_date": payload.effective_date,
        "end_date": target_end_date,
        "customer_daily_rate": cdr,
        "employee_daily_rate": edr,
        "daily_other_expense": existing.daily_other_expense,
        "contract_amount": existing.contract_amount,
        "status": AssignmentStatus.SCHEDULED,
        "acceptance_status": AcceptanceStatus.PENDING,
        "payment_type": existing.payment_type,
        "payment_status": PaymentStatus.PENDING,
        "is_replacement": True,
        "replacement_for_assignment_id": str(existing.id),
        "replacement_for_employee_id": existing.employee_id,
        "replacement_for_employee_name": existing.employee_name,
        "replacement_reason": payload.reason,
        "notes": f"Replacement for {existing.employee_name}. Reason: {payload.reason or 'Emergency/Leave'}"
        + (f" | {payload.notes}" if payload.notes else ""),
    }
    _derive_amounts(new_data, payload.effective_date, target_end_date)

    # Check conflict for the replacement staff member
    conflict = await _check_employee_conflict(
        str(new_emp.id),
        payload.effective_date,
        target_end_date,
        target_start_time,
        target_end_time,
        exclude_assignment_id=assignment_id,
    )
    if conflict:
        c_end = conflict.end_date.isoformat() if conflict.end_date else "Ongoing"
        raise BadRequestException(
            f"Replacement staff {new_emp.name} is already assigned to {conflict.customer_name} from {conflict.start_date.isoformat()} to {c_end}."
        )

    new_assignment = await _assignments.create(new_data, actor)
    await _set_employee_status(str(new_emp.id), EmployeeStatus.ASSIGNED)

    # 3. Dispatches and alerts to new staff member and relieved staff member
    async def _dispatch_replacement_alerts():
        shift_str = target_shift.value if hasattr(target_shift, "value") else str(target_shift)
        if target_windows:
            win_str = ", ".join(f"{w.get('start', '?')} - {w.get('end', '?')}" for w in target_windows)
            timing_str = f" (Custom Windows: {win_str})"
        else:
            timing_str = f" ({target_start_time} - {target_end_time})" if (target_start_time and target_end_time) else ""
        date_str = f"from {new_assignment.start_date.isoformat()}" + (f" to {target_end_date.isoformat()}" if target_end_date else " (Ongoing)")

        # 1. Alert to the new replacement nurse
        try:
            await notification_service.create(
                title="🚨 New Duty Assignment (Replacement)",
                message=f"You are assigned to {existing.customer_name} ({_place_text(new_assignment)}, Shift: {shift_str}{timing_str}, {date_str}) replacing {existing.employee_name}.",
                type=NotificationType.ASSIGNMENT,
                employee_id=str(new_emp.id),
                reference_id=str(new_assignment.id),
                link="/staff/status",
            )
        except Exception as err:
            logger.warning("Replacement notification dispatch note: %s", err)

        # 2. Notice to the relieved previous nurse
        try:
            await notification_service.create(
                title="ℹ️ Duty Handover / Replacement Notice",
                message=f"Your duty for {existing.customer_name} has been handed over to {new_emp.name} effective {payload.effective_date.isoformat()}.",
                type=NotificationType.ASSIGNMENT,
                employee_id=str(existing.employee_id),
                reference_id=str(existing.id),
                link="/staff/status",
            )
        except Exception as err:
            logger.warning("Handover notice dispatch note: %s", err)

        if new_emp.email:
            try:
                await email_service.send_assignment_notice(
                    to=new_emp.email,
                    staff_name=new_emp.name,
                    customer_name=existing.customer_name,
                    shift=f"{shift_str}{timing_str}",
                    place=_place_text(new_assignment),
                    start_date=new_assignment.start_date.isoformat(),
                    end_date=new_assignment.end_date.isoformat() if new_assignment.end_date else "Ongoing",
                )
            except Exception as err:
                logger.warning("Replacement email notice note: %s", err)

    asyncio.create_task(_dispatch_replacement_alerts())

    # 4. Sync linked booking if any
    try:
        from app.models.booking import Booking, BookingStatus
        booking = await Booking.find_one({
            "customer_id": existing.customer_id,
            "status": {"$in": [BookingStatus.ASSIGNED, BookingStatus.IN_PROGRESS, BookingStatus.APPROVED]},
        })
        if booking:
            booking.assigned_staff_id = str(new_emp.id)
            booking.assigned_staff_name = new_emp.name
            booking.touch()
            await booking.save()
    except Exception as err:
        logger.warning("Could not sync booking staff on replacement: %s", err)

    return existing, new_assignment


@router.post("/batch-replace", summary="Replace multiple staff members simultaneously")
async def batch_replace_staff(
    payload: BatchReplaceRequest,
    actor: ActorContext = Depends(require_permission("assignments", "update")),
) -> dict:
    if not payload.replacements:
        raise BadRequestException("No replacements provided")

    results = []
    for item in payload.replacements:
        req = ReplaceStaffRequest(
            new_employee_id=item.new_employee_id,
            effective_date=item.effective_date,
            end_date=item.end_date,
            shift=item.shift,
            start_time=item.start_time,
            end_time=item.end_time,
            shift_windows=item.shift_windows,
            reason=item.reason,
            customer_daily_rate=item.customer_daily_rate,
            employee_daily_rate=item.employee_daily_rate,
            notes=item.notes,
        )
        existing, new_asg = await _perform_staff_replacement(item.assignment_id, req, actor)
        results.append({
            "previous_assignment": AssignmentResponse.model_validate(existing).model_dump(mode="json"),
            "new_assignment": AssignmentResponse.model_validate(new_asg).model_dump(mode="json"),
        })

    from app.services.dashboard_service import invalidate_dashboard_cache
    invalidate_dashboard_cache()

    return success_response(
        data={"items": results, "count": len(results)},
        message=f"Successfully replaced {len(results)} staff members",
    )


@router.post("/multi-assign", summary="Assign multiple nurses/staff to a customer simultaneously")
async def multi_staff_assign(
    payload: MultiStaffAssignmentRequest,
    actor: ActorContext = Depends(require_permission("assignments", "create")),
) -> dict:
    if not payload.staff_list:
        raise BadRequestException("Please select at least one staff member")

    customer = await _customers.get(payload.customer_id)
    if not customer:
        raise BadRequestException("Customer not found")
    loc_id = payload.location_id or customer.location_id
    ensure_can_access(actor, loc_id)

    calculated_end_date = payload.end_date
    if not calculated_end_date and payload.duration_days:
        calculated_end_date = payload.start_date + dt.timedelta(days=payload.duration_days - 1)

    created_assignments = []
    for staff in payload.staff_list:
        emp = await _employees.get(staff.employee_id)
        if not emp:
            raise BadRequestException(f"Employee {staff.employee_id} not found")
        if emp.verification_status != StaffVerificationStatus.VERIFIED:
            raise BadRequestException(f"Staff member {emp.name} is not verified. Please select verified staff.")

        cdr = staff.customer_daily_rate if staff.customer_daily_rate is not None else payload.customer_daily_rate
        edr = staff.employee_daily_rate if staff.employee_daily_rate is not None else (payload.employee_daily_rate or emp.daily_rate)

        conflict = await _check_employee_conflict(
            str(emp.id),
            payload.start_date,
            calculated_end_date,
            staff.start_time,
            staff.end_time,
        )
        if conflict:
            c_end = conflict.end_date.isoformat() if conflict.end_date else "Ongoing"
            raise BadRequestException(
                f"{emp.name} is already assigned/on-duty for {conflict.customer_name} from {conflict.start_date.isoformat()} to {c_end}."
            )

        asg_data = {
            "customer_id": str(customer.id),
            "customer_name": customer.name,
            "employee_id": str(emp.id),
            "employee_name": emp.name,
            "location_id": loc_id,
            "place_type": payload.place_type,
            "place_name": payload.place_name or "",
            "purpose": payload.purpose,
            "address": payload.address or customer.address or "",
            "dest_lat": payload.dest_lat or customer.lat,
            "dest_lng": payload.dest_lng or customer.lng,
            "shift": staff.shift,
            "start_time": staff.start_time,
            "end_time": staff.end_time,
            "start_date": payload.start_date,
            "end_date": calculated_end_date,
            "customer_daily_rate": cdr,
            "employee_daily_rate": edr,
            "daily_other_expense": payload.daily_other_expense,
            "contract_amount": staff.contract_amount if staff.contract_amount is not None else payload.contract_amount,
            "status": AssignmentStatus.SCHEDULED,
            "acceptance_status": AcceptanceStatus.PENDING,
            "payment_type": payload.payment_type or "prepaid",
            "payment_status": PaymentStatus.PENDING,
            "notes": staff.notes or payload.notes,
        }
        _derive_amounts(asg_data, payload.start_date, calculated_end_date)
        asg = await _assignments.create(asg_data, actor)
        await _set_employee_status(str(emp.id), EmployeeStatus.ASSIGNED)
        created_assignments.append(asg)

        # Dispatch alert and transactional notifications
        async def _alert(target_emp=emp, target_asg=asg):
            try:
                shift_desc = f"{target_asg.shift}"
                if target_asg.start_time and target_asg.end_time:
                    shift_desc += f" ({target_asg.start_time} - {target_asg.end_time})"
                await notification_service.create(
                    title="🚨 New Duty Assignment",
                    message=f"You are assigned to {customer.name} (Shift: {shift_desc}) from {target_asg.start_date.isoformat()}.",
                    type=NotificationType.ASSIGNMENT,
                    employee_id=str(target_emp.id),
                    reference_id=str(target_asg.id),
                    link="/staff/status",
                )
            except Exception as e:
                logger.warning("Push alert note: %s", e)

            if target_emp.email:
                try:
                    await email_service.send_assignment_notice(
                        to=target_emp.email,
                        staff_name=target_emp.name,
                        customer_name=customer.name,
                        shift=str(target_asg.shift),
                        place=_place_text(target_asg),
                        start_date=target_asg.start_date.isoformat(),
                        end_date=target_asg.end_date.isoformat() if target_asg.end_date else "Ongoing",
                    )
                except Exception as e:
                    logger.warning("Assignment email notice note: %s", e)

        asyncio.create_task(_alert())

    from app.services.dashboard_service import invalidate_dashboard_cache
    invalidate_dashboard_cache()

    return success_response(
        data={
            "items": [AssignmentResponse.model_validate(a).model_dump(mode="json") for a in created_assignments],
            "count": len(created_assignments),
        },
        message=f"Successfully assigned {len(created_assignments)} staff members to {customer.name}",
    )


@router.post("/{assignment_id}/replace-staff", summary="Replace staff on an assignment with handover")
async def replace_assignment_staff(
    assignment_id: str,
    payload: ReplaceStaffRequest,
    actor: ActorContext = Depends(require_permission("assignments", "update")),
) -> dict:
    existing, new_assignment = await _perform_staff_replacement(assignment_id, payload, actor)
    from app.services.dashboard_service import invalidate_dashboard_cache
    invalidate_dashboard_cache()
    return success_response(
        data={
            "previous_assignment": AssignmentResponse.model_validate(existing).model_dump(mode="json"),
            "new_assignment": AssignmentResponse.model_validate(new_assignment).model_dump(mode="json"),
        },
        message=f"Successfully replaced {existing.employee_name} with {new_assignment.employee_name} from {payload.effective_date.isoformat()}",
    )


@router.delete("/{assignment_id}", summary="Delete assignment")
async def delete_assignment(
    assignment_id: str,
    actor: ActorContext = Depends(require_permission("assignments", "delete")),
) -> dict:
    existing = await _assignments.get_or_404(assignment_id)
    ensure_can_access(actor, existing.location_id)

    await _assignments.delete(assignment_id, actor)

    from app.services.cascade_delete import cascade_delete_assignment

    await cascade_delete_assignment(assignment_id, existing.employee_id)
    return success_response(message="Assignment deleted and records cleaned up")


@router.post("/{assignment_id}/breaks", summary="Add a temporary duty break / pause to an assignment")
async def add_duty_break(
    assignment_id: str,
    payload: DutyBreakCreate,
    actor: ActorContext = Depends(require_permission("assignments", "update")),
) -> dict:
    """Add a temporary break / pause (ছুটি / গ্যাপ) to an ongoing duty."""
    assignment = await _assignments.get_or_404(assignment_id)
    ensure_can_access(actor, assignment.location_id)

    if payload.start_date < assignment.start_date:
        raise BadRequestException(f"Break start date ({payload.start_date}) cannot be before duty start date ({assignment.start_date})")
    if assignment.end_date and payload.end_date > assignment.end_date:
        raise BadRequestException(f"Break end date ({payload.end_date}) cannot be after duty end date ({assignment.end_date})")
    if payload.end_date < payload.start_date:
        raise BadRequestException("Break end date cannot be earlier than start date")

    new_break = DutyBreak(
        id=uuid.uuid4().hex[:8],
        start_date=payload.start_date,
        end_date=payload.end_date,
        start_time=payload.start_time,
        end_time=payload.end_time,
        reason=payload.reason or "Customer request / staff leave",
    )
    if assignment.duty_breaks is None:
        assignment.duty_breaks = []
    assignment.duty_breaks.append(new_break)

    # Recalculate days worked and rates
    _recompute_assignment_totals(assignment)

    assignment.touch()
    await assignment.save()

    # Sync attendance records accurately against IST break intervals
    try:
        await sync_assignment_attendance(assignment)
    except Exception as exc:
        logger.warning("Failed to sync attendance on break addition: %s", exc)

    invalidate_dashboard_cache()

    return item_response(AssignmentResponse, assignment, "Duty break added successfully")


@router.delete("/{assignment_id}/breaks/{break_id}", summary="Remove a temporary duty break from an assignment")
async def remove_duty_break(
    assignment_id: str,
    break_id: str,
    actor: ActorContext = Depends(require_permission("assignments", "update")),
) -> dict:
    """Remove a duty break so the staff member resumes normal paid duty."""
    assignment = await _assignments.get_or_404(assignment_id)
    ensure_can_access(actor, assignment.location_id)

    if not assignment.duty_breaks:
        raise BadRequestException("No duty breaks found on this assignment")

    matched = [b for b in assignment.duty_breaks if b.id == break_id]
    if not matched:
        raise BadRequestException(f"Duty break {break_id} not found")

    assignment.duty_breaks = [b for b in assignment.duty_breaks if b.id != break_id]

    _recompute_assignment_totals(assignment)

    assignment.touch()
    await assignment.save()

    # Reconcile attendance records back to active IST schedule
    try:
        await sync_assignment_attendance(assignment)
    except Exception as exc:
        logger.warning("Failed to reset attendance on break removal: %s", exc)

    invalidate_dashboard_cache()

    return item_response(AssignmentResponse, assignment, "Duty break removed successfully")
