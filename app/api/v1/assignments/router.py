"""Assignment / daily-shift endpoints — staff engaged to customers."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.api.helpers import item_response, paginated_response
from app.core.exceptions import BadRequestException
from app.core.pagination import PaginationParams, pagination_params
from app.core.responses import success_response
from app.dependencies.auth import ActorContext, require_permission
from app.dependencies.scoping import ensure_can_access
from app.models.assignment import Assignment, ShiftWindow
from app.models.customer import Customer
from app.models.employee import Employee
from app.models.enums import (
    AcceptanceStatus,
    AssignmentPlace,
    AssignmentStatus,
    EmployeeStatus,
    NotificationType,
    PaymentMode,
    PaymentStatus,
    ShiftType,
)
from app.models.location import Location
from app.repositories.base import BaseRepository
from app.schemas.assignment import (
    AssignmentCreate,
    AssignmentResponse,
    AssignmentUpdate,
)
from app.services.crud import CrudService
from app.services.email_service import email_service
from app.services.geo_service import geo_service
from app.services.notification_service import notification_service

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
) -> dict:
    """Set stored totals from the per-day rates: total = daily_rate × total_days.

    Only runs when the engagement has a concrete end_date (a closed span). An
    explicit ``customer_rate`` / ``employee_payout`` in the payload always wins.
    Open-ended engagements keep null totals and are billed per calendar month.
    """
    if not (start and end):
        return data
    total_days = (end - start).days + 1
    if total_days <= 0:
        return data
    cdr = data.get("customer_daily_rate", existing_cdr)
    edr = data.get("employee_daily_rate", existing_edr)
    if cdr is not None and "customer_rate" not in data:
        data["customer_rate"] = cdr * total_days
    if edr is not None and "employee_payout" not in data:
        data["employee_payout"] = edr * total_days
    return data


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
    day = date or dt.date.today()
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
    today = dt.datetime.combine(dt.date.today(), dt.time.min)

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
    # Recompute stored totals when the dates or per-day rates change.
    _derive_amounts(
        data,
        data.get("start_date") or existing.start_date,
        data.get("end_date") if "end_date" in data else existing.end_date,
        existing.customer_daily_rate,
        existing.employee_daily_rate,
    )
    # Re-geocode if the posting address changed and no explicit pin was given
    if "address" in data and "dest_lat" not in data and data["address"] != existing.address:
        coords = await geo_service.geocode(data["address"] or "")
        if coords is not None:
            data["dest_lat"], data["dest_lng"] = coords
    doc = await _assignments.update(assignment_id, data, actor)

    # Keep the staff member's availability in sync + tell them about status changes.
    if "status" in data:
        if doc.status in _OCCUPYING:
            await _set_employee_status(doc.employee_id, EmployeeStatus.ASSIGNED)
        elif doc.status in (AssignmentStatus.COMPLETED, AssignmentStatus.CANCELLED):
            await _set_employee_status(doc.employee_id, EmployeeStatus.AVAILABLE)
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


# First digital bill number. Kept well clear of the low numbers on the paper
# bill pad so the two series never collide.
_BILL_NO_BASE = 1000


async def _next_bill_no() -> int:
    """Atomically allocate the next sequential customer-bill number."""
    from pymongo import ReturnDocument

    try:
        from app.database.connection import get_database
        db = get_database()
    except Exception:
        db = Assignment.get_motor_collection().database

    res = await db["counters"].find_one_and_update(
        {"_id": "assignment_bill_no"},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return _BILL_NO_BASE + int(res["seq"])


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
    if doc.bill_no is None:
        doc.bill_no = await _next_bill_no()
        doc.touch()
        await doc.save()
    return success_response(data={"bill_no": doc.bill_no})


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

    now = dt.datetime.now(dt.timezone.utc)
    today = now.date()

    existing.status = AssignmentStatus.COMPLETED
    existing.acceptance_status = AcceptanceStatus.ACCEPTED
    existing.work_completed_at = now

    if existing.end_date is None:
        existing.end_date = today
        if not existing.end_time:
            existing.end_time = now.strftime("%H:%M")

    eff_days = max(1, (existing.end_date - existing.start_date).days + 1)
    existing.days_worked = eff_days

    if existing.customer_rate is None and existing.customer_daily_rate:
        existing.customer_rate = existing.customer_daily_rate * eff_days
    if existing.employee_payout is None and existing.employee_daily_rate:
        existing.employee_payout = existing.employee_daily_rate * eff_days

    ptype = str(getattr(existing, "payment_type", "prepaid"))
    is_postpaid = (ptype == "postpaid")
    amount = payload.cash_amount if payload.cash_amount is not None else int(existing.customer_rate or 0)
    actor_name = actor.user.name if (hasattr(actor, "user") and actor.user and actor.user.name) else "Super Admin"
    emp_name = existing.employee_name or "Staff"

    if is_postpaid or payload.cash_collected:
        existing.payment_status = PaymentStatus.PAID
        existing.payment_received_at = now
        existing.payment_received_by = payload.received_by or f"Super Admin ({actor_name}) on behalf of {emp_name}"
        if amount > 0:
            try:
                from app.models.finance import Payment
                pay = Payment(
                    customer_id=existing.customer_id,
                    customer_name=existing.customer_name,
                    assignment_id=str(existing.id),
                    location_id=existing.location_id,
                    amount=amount,
                    mode=PaymentMode.CASH,
                    status=PaymentStatus.PAID,
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
    notes: Optional[str] = None


class MultiStaffAssignmentRequest(BaseModel):
    customer_id: str
    location_id: Optional[str] = None
    place_type: AssignmentPlace = AssignmentPlace.HOME
    place_name: Optional[str] = None
    address: Optional[str] = None
    dest_lat: Optional[float] = None
    dest_lng: Optional[float] = None
    start_date: dt.date
    end_date: Optional[dt.date] = None
    duration_days: Optional[int] = None
    customer_daily_rate: Optional[int] = None
    employee_daily_rate: Optional[int] = None
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
    if str(new_emp.id) == existing.employee_id:
        raise BadRequestException("Replacement staff must be different from current staff")

    orig_end_date = existing.end_date
    today = dt.date.today()

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
        await _set_employee_status(existing.employee_id, EmployeeStatus.AVAILABLE)
    else:
        old_end_date = min(existing.end_date, payload.effective_date) if existing.end_date else payload.effective_date
        existing.end_date = old_end_date
        existing.replaced_by_employee_id = str(new_emp.id)
        existing.replaced_by_employee_name = new_emp.name
        existing.replacement_reason = payload.reason
        existing.days_worked = max(0, (old_end_date - existing.start_date).days + 1)
        totals_dict = _derive_amounts(
            {"customer_daily_rate": existing.customer_daily_rate, "employee_daily_rate": existing.employee_daily_rate},
            existing.start_date,
            old_end_date,
            existing.customer_daily_rate,
            existing.employee_daily_rate,
        )
        if "customer_rate" in totals_dict:
            existing.customer_rate = totals_dict["customer_rate"]
        if "employee_payout" in totals_dict:
            existing.employee_payout = totals_dict["employee_payout"]

        if payload.start_time:
            existing.end_time = payload.start_time

        if old_end_date <= today:
            existing.status = AssignmentStatus.COMPLETED
            await _set_employee_status(existing.employee_id, EmployeeStatus.AVAILABLE)

        note_suffix = f"Handed over to {new_emp.name} on {payload.effective_date}. Reason: {payload.reason or 'Staff emergency/leave'}"
        existing.notes = f"{existing.notes} | {note_suffix}" if existing.notes else note_suffix
        existing.touch()
        await existing.save()

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
        "status": AssignmentStatus.SCHEDULED,
        "acceptance_status": AcceptanceStatus.PENDING,
        "payment_type": existing.payment_type,
        "payment_status": PaymentStatus.PENDING,
        "is_replacement": True,
        "replacement_for_employee_id": existing.employee_id,
        "replacement_for_employee_name": existing.employee_name,
        "replacement_reason": payload.reason,
        "notes": f"Replacement for {existing.employee_name}. Reason: {payload.reason or 'Emergency/Leave'}"
        + (f" | {payload.notes}" if payload.notes else ""),
    }
    _derive_amounts(new_data, payload.effective_date, target_end_date)

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

        cdr = staff.customer_daily_rate if staff.customer_daily_rate is not None else payload.customer_daily_rate
        edr = staff.employee_daily_rate if staff.employee_daily_rate is not None else (payload.employee_daily_rate or emp.daily_rate)

        asg_data = {
            "customer_id": str(customer.id),
            "customer_name": customer.name,
            "employee_id": str(emp.id),
            "employee_name": emp.name,
            "location_id": loc_id,
            "place_type": payload.place_type,
            "place_name": payload.place_name or "",
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
    from app.services.dashboard_service import invalidate_dashboard_cache
    invalidate_dashboard_cache()
    return success_response(message="Assignment deleted")
