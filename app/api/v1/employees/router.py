"""Operational staff (employee) endpoints — location-scoped HR records."""

from __future__ import annotations

import secrets
import string
from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.api.helpers import item_response, paginated_response
from app.core.pagination import PaginationParams, pagination_params
from app.core.responses import success_response
from app.dependencies.auth import ActorContext, require_permission
from app.dependencies.scoping import ensure_can_access, resolve_create_location
from app.models.employee import Employee
from app.schemas.employee import EmployeeCreate, EmployeeResponse, EmployeeUpdate
from app.services.crud import CrudService

router = APIRouter(prefix="/employees", tags=["Employees"])

_employees = CrudService(
    Employee, entity="employee",
    search_fields=("name", "phone", "code", "category", "city"),
)

_ALPHABET = string.ascii_uppercase + string.digits


async def _unique_code() -> str:
    """Generate a unique staff code like ``EMP-4F2A9C``."""
    for _ in range(10):
        code = "EMP-" + "".join(secrets.choice(_ALPHABET) for _ in range(6))
        if await Employee.find_one({"code": code}) is None:
            return code
    return "EMP-" + secrets.token_hex(4).upper()


@router.get("", summary="List employees")
async def list_employees(
    params: PaginationParams = Depends(pagination_params),
    category: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    location_id: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(None),
    actor: ActorContext = Depends(require_permission("employees", "view")),
) -> dict:
    filters: dict = actor.scope_filter()
    if category is not None:
        filters["category"] = category
    if status is not None:
        filters["status"] = status
    if location_id is not None and actor.can_access_location(location_id):
        filters["location_id"] = location_id
    if is_active is not None:
        filters["is_active"] = is_active
    items, total = await _employees.paginate(
        page=params.page, page_size=params.page_size, search=params.search,
        sort_by=params.sort_by or "created_at", sort_order=params.sort_direction,
        filters=filters or None,
    )
    return paginated_response(EmployeeResponse, items, total, params)


@router.get("/{employee_id}", summary="Get employee")
async def get_employee(
    employee_id: str,
    actor: ActorContext = Depends(require_permission("employees", "view")),
) -> dict:
    doc = await _employees.get_or_404(employee_id)
    ensure_can_access(actor, doc.location_id)
    return item_response(EmployeeResponse, doc)


@router.post("", status_code=201, summary="Register employee")
async def create_employee(
    payload: EmployeeCreate,
    actor: ActorContext = Depends(require_permission("employees", "create")),
) -> dict:
    data = payload.model_dump(exclude_unset=True)
    data["location_id"] = resolve_create_location(actor, data.get("location_id"))
    data["code"] = await _unique_code()
    doc = await _employees.create(data, actor)
    return item_response(EmployeeResponse, doc, "Employee registered")


@router.put("/{employee_id}", summary="Update employee")
async def update_employee(
    employee_id: str,
    payload: EmployeeUpdate,
    actor: ActorContext = Depends(require_permission("employees", "update")),
) -> dict:
    existing = await _employees.get_or_404(employee_id)
    ensure_can_access(actor, existing.location_id)
    data = payload.model_dump(exclude_unset=True)
    if "location_id" in data and data["location_id"]:
        ensure_can_access(actor, data["location_id"])
    doc = await _employees.update(employee_id, data, actor)
    return item_response(EmployeeResponse, doc, "Employee updated")


@router.delete("/{employee_id}", summary="Delete employee")
async def delete_employee(
    employee_id: str,
    actor: ActorContext = Depends(require_permission("employees", "delete")),
) -> dict:
    existing = await _employees.get_or_404(employee_id)
    ensure_can_access(actor, existing.location_id)
    await _employees.delete(employee_id, actor)
    return success_response(message="Employee deleted")
