"""Customer payment endpoints — location-scoped (income / receivables)."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.api.helpers import item_response, paginated_response
from app.core.exceptions import BadRequestException
from app.core.pagination import PaginationParams, pagination_params
from app.core.responses import success_response
from app.dependencies.auth import ActorContext, require_permission
from app.dependencies.scoping import ensure_can_access
from app.models.customer import Customer
from app.models.finance import Payment
from app.repositories.base import BaseRepository
from app.schemas.finance import PaymentCreate, PaymentResponse, PaymentUpdate
from app.services.crud import CrudService

router = APIRouter(prefix="/payments", tags=["Payments"])

_payments = CrudService(Payment, entity="payment", search_fields=("customer_name", "note"))
_customers: BaseRepository[Customer] = BaseRepository(Customer)


@router.get("", summary="List payments")
async def list_payments(
    params: PaginationParams = Depends(pagination_params),
    status: Optional[str] = Query(None),
    customer_id: Optional[str] = Query(None),
    location_id: Optional[str] = Query(None),
    actor: ActorContext = Depends(require_permission("payments", "view")),
) -> dict:
    filters: dict = actor.scope_filter()
    if status is not None:
        filters["status"] = status
    if customer_id is not None:
        filters["customer_id"] = customer_id
    if location_id is not None and actor.can_access_location(location_id):
        filters["location_id"] = location_id
    items, total = await _payments.paginate(
        page=params.page, page_size=params.page_size, search=params.search,
        sort_by=params.sort_by or "date", sort_order=params.sort_direction,
        filters=filters or None,
    )
    return paginated_response(PaymentResponse, items, total, params)


@router.get("/{payment_id}", summary="Get payment")
async def get_payment(
    payment_id: str,
    actor: ActorContext = Depends(require_permission("payments", "view")),
) -> dict:
    doc = await _payments.get_or_404(payment_id)
    ensure_can_access(actor, doc.location_id)
    return item_response(PaymentResponse, doc)


@router.post("", status_code=201, summary="Record payment")
async def create_payment(
    payload: PaymentCreate,
    actor: ActorContext = Depends(require_permission("payments", "create")),
) -> dict:
    customer = await _customers.get(payload.customer_id)
    if customer is None:
        raise BadRequestException("Customer not found")
    ensure_can_access(actor, customer.location_id)
    data = payload.model_dump(exclude_unset=True)
    data["location_id"] = customer.location_id
    data["customer_name"] = customer.name
    doc = await _payments.create(data, actor)
    return item_response(PaymentResponse, doc, "Payment recorded")


@router.put("/{payment_id}", summary="Update payment")
async def update_payment(
    payment_id: str,
    payload: PaymentUpdate,
    actor: ActorContext = Depends(require_permission("payments", "update")),
) -> dict:
    existing = await _payments.get_or_404(payment_id)
    ensure_can_access(actor, existing.location_id)
    doc = await _payments.update(payment_id, payload.model_dump(exclude_unset=True), actor)
    return item_response(PaymentResponse, doc, "Payment updated")


@router.delete("/{payment_id}", summary="Delete payment")
async def delete_payment(
    payment_id: str,
    actor: ActorContext = Depends(require_permission("payments", "delete")),
) -> dict:
    existing = await _payments.get_or_404(payment_id)
    ensure_can_access(actor, existing.location_id)
    await _payments.delete(payment_id, actor)
    return success_response(message="Payment deleted")
