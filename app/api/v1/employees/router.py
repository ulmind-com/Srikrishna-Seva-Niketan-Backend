"""Operational staff (employee) endpoints — location-scoped HR records."""

from __future__ import annotations

import datetime as dt
import secrets
import string
from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.api.helpers import item_response, paginated_response
from app.core.exceptions import BadRequestException
from app.core.pagination import PaginationParams, pagination_params
from app.core.responses import success_response
from app.dependencies.auth import ActorContext, require_permission, require_superuser
from app.dependencies.scoping import ensure_can_access, resolve_create_location
from app.models.employee import Employee
from app.models.enums import StaffVerificationStatus
from app.models.location import Location
from app.schemas.employee import EmployeeCreate, EmployeeResponse, EmployeeUpdate
from app.schemas.staff_portal import (
    LocationStaffStat,
    StaffRejectRequest,
    StaffVerifyRequest,
)
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
    verification_status: Optional[StaffVerificationStatus] = Query(None),
    is_active: Optional[bool] = Query(None),
    actor: ActorContext = Depends(require_permission("employees", "view")),
) -> dict:
    filters: dict = actor.scope_filter()
    if category is not None:
        filters["category"] = category
    if status is not None:
        filters["status"] = status
    if verification_status is not None:
        filters["verification_status"] = verification_status.value
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


# ---------------------------------------------------------------------------
# Staff-portal verification (super-admin only)
# ---------------------------------------------------------------------------


async def _unique_hrms_code() -> str:
    """Generate an unused 6-digit E-HRMS code for the printed ID card."""
    for _ in range(10):
        code = "".join(secrets.choice(string.digits) for _ in range(6))
        if await Employee.find_one({"hrms_code": code}) is None:
            return code
    return secrets.token_hex(3).upper()


@router.post("/{employee_id}/verify", summary="Verify a staff profile")
async def verify_employee(
    employee_id: str,
    payload: StaffVerifyRequest,
    actor: ActorContext = Depends(require_superuser),
) -> dict:
    """Approve a submitted staff profile.

    On approval the record gets its printed-document identifiers (E-HRMS code,
    designation, join date), which unlocks the ID card and the two application
    forms for that staff member.
    """
    doc = await _employees.get_or_404(employee_id)
    if doc.verification_status == StaffVerificationStatus.VERIFIED:
        raise BadRequestException("This profile is already verified")

    data = payload.model_dump(exclude_unset=True, exclude_none=True)
    for field, value in data.items():
        setattr(doc, field, value)

    if not doc.hrms_code:
        doc.hrms_code = await _unique_hrms_code()
    if not doc.designation:
        doc.designation = doc.category.replace("_", " ").title()
    if not doc.join_date:
        doc.join_date = dt.date.today()

    doc.verification_status = StaffVerificationStatus.VERIFIED
    doc.verified_at = dt.datetime.now(dt.timezone.utc)
    doc.verified_by = actor.user_id
    doc.rejection_reason = None
    doc.touch()
    await doc.save()

    return item_response(EmployeeResponse, doc, "Staff verified — documents are ready")


@router.post("/{employee_id}/reject", summary="Reject a staff profile")
async def reject_employee(
    employee_id: str,
    payload: StaffRejectRequest,
    actor: ActorContext = Depends(require_superuser),
) -> dict:
    """Send a submitted profile back to the staff member with a reason."""
    doc = await _employees.get_or_404(employee_id)
    if doc.verification_status == StaffVerificationStatus.VERIFIED:
        raise BadRequestException("Cannot reject an already-verified profile")

    doc.verification_status = StaffVerificationStatus.REJECTED
    doc.rejection_reason = payload.reason.strip()
    doc.verified_by = actor.user_id
    doc.verified_at = None
    doc.touch()
    await doc.save()

    return item_response(EmployeeResponse, doc, "Staff profile rejected")


@router.get("/stats/by-location", summary="Staff counts per branch")
async def staff_stats_by_location(
    actor: ActorContext = Depends(require_permission("employees", "view")),
) -> dict:
    """Branch-wise staff counts.

    Super-admins get every branch; a branch-scoped manager gets only the
    branches assigned to them, so the same screen serves both roles.
    """
    locations = await Location.find({"is_active": True}).sort("order").to_list()
    if actor.is_scoped:
        locations = [loc for loc in locations if str(loc.id) in actor.location_ids]

    stats: list[dict] = []
    for loc in locations:
        loc_id = str(loc.id)
        base = {"location_id": loc_id}

        async def _count(extra: dict) -> int:
            return await Employee.find({**base, **extra}).count()

        stats.append(
            LocationStaffStat(
                location_id=loc_id,
                location_name=loc.name,
                location_code=loc.code,
                city=loc.city,
                total=await Employee.find(base).count(),
                verified=await _count(
                    {"verification_status": StaffVerificationStatus.VERIFIED.value}
                ),
                pending=await _count(
                    {"verification_status": StaffVerificationStatus.PENDING.value}
                ),
                rejected=await _count(
                    {"verification_status": StaffVerificationStatus.REJECTED.value}
                ),
                draft=await _count(
                    {"verification_status": StaffVerificationStatus.DRAFT.value}
                ),
                active=await _count({"is_active": True}),
            ).model_dump(mode="json")
        )

    return success_response(data=stats)
