"""Customer (client) endpoints — location-scoped."""

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
from app.models.customer import Customer
from app.schemas.customer import CustomerCreate, CustomerResponse, CustomerUpdate
from app.services.crud import CrudService
from app.services.geo_service import geo_service


async def _ensure_coords(data: dict) -> None:
    """Geocode the address into lat/lng when no pin was supplied (best-effort)."""
    if data.get("lat") is not None and data.get("lng") is not None:
        return
    address = data.get("address")
    if not address:
        return
    coords = await geo_service.geocode(address)
    if coords is not None:
        data["lat"], data["lng"] = coords

router = APIRouter(prefix="/customers", tags=["Customers"])

_customers = CrudService(
    Customer, entity="customer",
    search_fields=("name", "phone", "code", "city", "service_label"),
)

_ALPHABET = string.ascii_uppercase + string.digits


async def _unique_code() -> str:
    for _ in range(10):
        code = "CUST-" + "".join(secrets.choice(_ALPHABET) for _ in range(6))
        if await Customer.find_one({"code": code}) is None:
            return code
    return "CUST-" + secrets.token_hex(4).upper()


@router.get("", summary="List customers")
async def list_customers(
    params: PaginationParams = Depends(pagination_params),
    care_type: Optional[str] = Query(None),
    location_id: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(None),
    actor: ActorContext = Depends(require_permission("customers", "view")),
) -> dict:
    filters: dict = actor.scope_filter()
    if care_type is not None:
        filters["care_type"] = care_type
    if location_id is not None and actor.can_access_location(location_id):
        filters["location_id"] = location_id
    if is_active is not None:
        filters["is_active"] = is_active
    items, total = await _customers.paginate(
        page=params.page, page_size=params.page_size, search=params.search,
        sort_by=params.sort_by or "created_at", sort_order=params.sort_direction,
        filters=filters or None,
    )
    return paginated_response(CustomerResponse, items, total, params)


@router.get("/{customer_id}", summary="Get customer")
async def get_customer(
    customer_id: str,
    actor: ActorContext = Depends(require_permission("customers", "view")),
) -> dict:
    doc = await _customers.get_or_404(customer_id)
    ensure_can_access(actor, doc.location_id)
    return item_response(CustomerResponse, doc)


@router.post("", status_code=201, summary="Register customer")
async def create_customer(
    payload: CustomerCreate,
    actor: ActorContext = Depends(require_permission("customers", "create")),
) -> dict:
    data = payload.model_dump(exclude_unset=True)
    data["location_id"] = resolve_create_location(actor, data.get("location_id"))
    data["code"] = await _unique_code()
    await _ensure_coords(data)
    doc = await _customers.create(data, actor)
    return item_response(CustomerResponse, doc, "Customer registered")


@router.put("/{customer_id}", summary="Update customer")
async def update_customer(
    customer_id: str,
    payload: CustomerUpdate,
    actor: ActorContext = Depends(require_permission("customers", "update")),
) -> dict:
    existing = await _customers.get_or_404(customer_id)
    ensure_can_access(actor, existing.location_id)
    data = payload.model_dump(exclude_unset=True)
    if "location_id" in data and data["location_id"]:
        ensure_can_access(actor, data["location_id"])
    # Re-geocode only when the address changed and no explicit pin was supplied.
    if "address" in data and ("lat" not in data or data.get("lat") is None):
        if existing.address != data["address"] or existing.lat is None or existing.lng is None:
            await _ensure_coords(data)
    doc = await _customers.update(customer_id, data, actor)
    return item_response(CustomerResponse, doc, "Customer updated")


@router.delete("/{customer_id}", summary="Delete customer")
async def delete_customer(
    customer_id: str,
    actor: ActorContext = Depends(require_permission("customers", "delete")),
) -> dict:
    existing = await _customers.get_or_404(customer_id)
    ensure_can_access(actor, existing.location_id)
    await _customers.delete(customer_id, actor)
    return success_response(message="Customer deleted")
