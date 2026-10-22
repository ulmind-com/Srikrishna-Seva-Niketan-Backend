"""Firm expense endpoints — location-scoped (feeds the P&L report)."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.api.helpers import item_response, paginated_response
from app.core.pagination import PaginationParams, pagination_params
from app.core.responses import success_response
from app.dependencies.auth import ActorContext, require_permission
from app.dependencies.scoping import ensure_can_access, resolve_create_location
from app.models.finance import Expense
from app.schemas.finance import ExpenseCreate, ExpenseResponse, ExpenseUpdate
from app.services.crud import CrudService

router = APIRouter(prefix="/expenses", tags=["Expenses"])

_expenses = CrudService(Expense, entity="expense", search_fields=("category", "note"))


@router.get("", summary="List expenses")
async def list_expenses(
    params: PaginationParams = Depends(pagination_params),
    category: Optional[str] = Query(None),
    location_id: Optional[str] = Query(None),
    actor: ActorContext = Depends(require_permission("expenses", "view")),
) -> dict:
    filters: dict = actor.scope_filter()
    if category is not None:
        filters["category"] = category
    if location_id is not None and actor.can_access_location(location_id):
        filters["location_id"] = location_id
    items, total = await _expenses.paginate(
        page=params.page, page_size=params.page_size, search=params.search,
        sort_by=params.sort_by or "date", sort_order=params.sort_direction,
        filters=filters or None,
    )
    return paginated_response(ExpenseResponse, items, total, params)


@router.get("/{expense_id}", summary="Get expense")
async def get_expense(
    expense_id: str,
    actor: ActorContext = Depends(require_permission("expenses", "view")),
) -> dict:
    doc = await _expenses.get_or_404(expense_id)
    ensure_can_access(actor, doc.location_id)
    return item_response(ExpenseResponse, doc)


@router.post("", status_code=201, summary="Record expense")
async def create_expense(
    payload: ExpenseCreate,
    actor: ActorContext = Depends(require_permission("expenses", "create")),
) -> dict:
    data = payload.model_dump(exclude_unset=True)
    data["location_id"] = resolve_create_location(actor, data.get("location_id"))
    doc = await _expenses.create(data, actor)
    return item_response(ExpenseResponse, doc, "Expense recorded")


@router.put("/{expense_id}", summary="Update expense")
async def update_expense(
    expense_id: str,
    payload: ExpenseUpdate,
    actor: ActorContext = Depends(require_permission("expenses", "update")),
) -> dict:
    existing = await _expenses.get_or_404(expense_id)
    ensure_can_access(actor, existing.location_id)
    data = payload.model_dump(exclude_unset=True)
    if "location_id" in data and data["location_id"]:
        ensure_can_access(actor, data["location_id"])
    doc = await _expenses.update(expense_id, data, actor)
    return item_response(ExpenseResponse, doc, "Expense updated")


@router.delete("/{expense_id}", summary="Delete expense")
async def delete_expense(
    expense_id: str,
    actor: ActorContext = Depends(require_permission("expenses", "delete")),
) -> dict:
    existing = await _expenses.get_or_404(expense_id)
    ensure_can_access(actor, existing.location_id)
    await _expenses.delete(expense_id, actor)
    return success_response(message="Expense deleted")
