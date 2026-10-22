"""Assignment / daily-shift endpoints — staff engaged to customers."""

from __future__ import annotations

import datetime as dt
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
from app.repositories.base import BaseRepository
from app.schemas.assignment import (
    AssignmentCreate,
    AssignmentResponse,
    AssignmentUpdate,
)
from app.services.crud import CrudService
from app.services.notification_service import notification_service

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
    if customer.location_id != employee.location_id:
        raise BadRequestException("Staff and customer must belong to the same branch")

    ensure_can_access(actor, customer.location_id)

    data = payload.model_dump(exclude_unset=True)
    data["location_id"] = customer.location_id
    data["customer_name"] = customer.name
    data["employee_name"] = employee.name
    # Default rates from the linked records when not explicitly provided.
    data.setdefault("customer_rate", customer.billing_rate)
    data.setdefault("employee_payout", employee.daily_rate)

    doc = await _assignments.create(data, actor)

    # Mark the staff member occupied + notify the branch.
    if doc.status in _OCCUPYING:
        await _set_employee_status(str(employee.id), EmployeeStatus.ASSIGNED)
    await notification_service.create(
        title="New assignment",
        message=f"{employee.name} assigned to {customer.name} ({doc.shift} shift).",
        type=NotificationType.ASSIGNMENT,
        link="/assignments",
        reference_id=str(doc.id),
    )
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
    doc = await _assignments.update(assignment_id, data, actor)

    # Keep the staff member's availability in sync with the engagement status.
    if "status" in data:
        if doc.status in _OCCUPYING:
            await _set_employee_status(doc.employee_id, EmployeeStatus.ASSIGNED)
        elif doc.status in (AssignmentStatus.COMPLETED, AssignmentStatus.CANCELLED):
            await _set_employee_status(doc.employee_id, EmployeeStatus.AVAILABLE)
    return item_response(AssignmentResponse, doc, "Assignment updated")


@router.delete("/{assignment_id}", summary="Delete assignment")
async def delete_assignment(
    assignment_id: str,
    actor: ActorContext = Depends(require_permission("assignments", "delete")),
) -> dict:
    existing = await _assignments.get_or_404(assignment_id)
    ensure_can_access(actor, existing.location_id)
    await _assignments.delete(assignment_id, actor)
    return success_response(message="Assignment deleted")
