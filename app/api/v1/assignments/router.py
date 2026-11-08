"""Assignment / daily-shift endpoints — staff engaged to customers."""

from __future__ import annotations

import asyncio
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
from app.models.assignment import Assignment
from app.models.customer import Customer
from app.models.employee import Employee
from app.models.enums import AssignmentStatus, EmployeeStatus, NotificationType
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

    from app.database.connection import get_database

    db = get_database()
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
