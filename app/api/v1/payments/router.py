"""Customer receipts, partial payments, staff salary payouts & unified transaction history."""

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
from app.models.finance import Payment, StaffPayment
from app.repositories.base import BaseRepository
from app.schemas.finance import (
    PaymentCreate,
    PaymentResponse,
    PaymentUpdate,
    StaffPaymentCreate,
    StaffPaymentResponse,
)
from app.services import finance_service
from app.services.crud import CrudService
from app.utils.bill_number import format_bill_no, next_receipt_no, next_staff_voucher_no
from app.utils.ist_time import today_ist

router = APIRouter(prefix="/payments", tags=["Payments"])

_payments = CrudService(Payment, entity="payment", search_fields=("customer_name", "note", "receipt_no", "transaction_id"))
_staff_payments = CrudService(StaffPayment, entity="staff_payment", search_fields=("employee_name", "note", "voucher_no", "transaction_id"))
_customers: BaseRepository[Customer] = BaseRepository(Customer)
_employees: BaseRepository[Employee] = BaseRepository(Employee)


def _db():
    try:
        from app.database.connection import get_database
        return get_database()
    except Exception:
        return Payment.get_motor_collection().database


def _effective_scope(actor: ActorContext, location_id: Optional[str]) -> dict:
    if location_id and actor.can_access_location(location_id):
        return {"location_id": location_id}
    return actor.scope_filter()


# ---- Ledger & History endpoints (defined BEFORE /{payment_id}) ----


@router.get("/client-ledger", summary="Client-wise Billed vs Received vs Outstanding ledger")
async def client_ledger(
    start: Optional[dt.date] = Query(None),
    end: Optional[dt.date] = Query(None),
    location_id: Optional[str] = Query(None),
    customer_id: Optional[str] = Query(None),
    actor: ActorContext = Depends(require_permission("payments", "view")),
) -> dict:
    data = await finance_service.compute_client_ledger(
        _effective_scope(actor, location_id), start=start, end=end, customer_id=customer_id
    )
    return success_response(data=data)


@router.get("/staff-ledger", summary="Staff-wise Salary Due vs Paid vs Remaining ledger")
async def staff_ledger(
    month: Optional[str] = Query(None),
    start: Optional[dt.date] = Query(None),
    end: Optional[dt.date] = Query(None),
    location_id: Optional[str] = Query(None),
    actor: ActorContext = Depends(require_permission("payments", "view")),
) -> dict:
    data = await finance_service.compute_staff_ledger(
        _effective_scope(actor, location_id),
        month=month,
        start=start,
        end=end,
        location_id=location_id if (location_id and actor.can_access_location(location_id)) else None,
    )
    return success_response(data=data)


@router.get("/staff", summary="List staff salary payouts")
async def list_staff_payments(
    params: PaginationParams = Depends(pagination_params),
    employee_id: Optional[str] = Query(None),
    month: Optional[str] = Query(None),
    location_id: Optional[str] = Query(None),
    actor: ActorContext = Depends(require_permission("payments", "view")),
) -> dict:
    filters = _effective_scope(actor, location_id)
    if employee_id:
        filters["employee_id"] = employee_id
    if month:
        filters["month"] = month
    items, total = await _staff_payments.paginate(
        page=params.page,
        page_size=params.page_size,
        search=params.search,
        sort_by=params.sort_by or "date",
        sort_order=params.sort_direction,
        filters=filters or None,
    )
    return paginated_response(StaffPaymentResponse, items, total, params)


@router.post("/staff", status_code=201, summary="Record staff salary payment (supports partial)")
async def create_staff_payment(
    payload: StaffPaymentCreate,
    actor: ActorContext = Depends(require_permission("payments", "create")),
) -> dict:
    emp = await _employees.get(payload.employee_id)
    if emp is None:
        raise BadRequestException("Staff member not found")
    ensure_can_access(actor, emp.location_id)

    data = payload.model_dump(exclude_unset=True)
    pay_date = data.get("date") or today_ist()
    data["date"] = pay_date
    data.setdefault("month", f"{pay_date.year:04d}-{pay_date.month:02d}")
    data["location_id"] = emp.location_id
    data["employee_name"] = emp.name
    data["voucher_no"] = await next_staff_voucher_no(_db())
    if not data.get("paid_by") and actor.user:
        data["paid_by"] = actor.user.name

    doc = await _staff_payments.create(data, actor)
    from app.services.dashboard_service import invalidate_dashboard_cache
    invalidate_dashboard_cache()
    return item_response(StaffPaymentResponse, doc, "Staff salary payment recorded")


@router.delete("/staff/{payout_id}", summary="Delete a staff salary payment")
async def delete_staff_payment(
    payout_id: str,
    actor: ActorContext = Depends(require_permission("payments", "delete")),
) -> dict:
    existing = await _staff_payments.get_or_404(payout_id)
    ensure_can_access(actor, existing.location_id)
    await _staff_payments.delete(payout_id, actor)
    from app.services.dashboard_service import invalidate_dashboard_cache
    invalidate_dashboard_cache()
    return success_response(message="Staff payment deleted")


@router.get("/history", summary="Unified incoming & outgoing transaction history")
async def payment_history(
    start: Optional[dt.date] = Query(None),
    end: Optional[dt.date] = Query(None),
    location_id: Optional[str] = Query(None),
    direction: str = Query("all"),
    mode: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    actor: ActorContext = Depends(require_permission("payments", "view")),
) -> dict:
    data = await finance_service.get_payment_history(
        _effective_scope(actor, location_id),
        start=start,
        end=end,
        direction=direction,
        mode=mode,
        search=search,
    )
    return success_response(data=data)


# ---- Client Payment Receipts CRUD ----


@router.get("", summary="List payments")
async def list_payments(
    params: PaginationParams = Depends(pagination_params),
    status: Optional[str] = Query(None),
    customer_id: Optional[str] = Query(None),
    assignment_id: Optional[str] = Query(None),
    location_id: Optional[str] = Query(None),
    actor: ActorContext = Depends(require_permission("payments", "view")),
) -> dict:
    filters: dict = actor.scope_filter()
    if status is not None:
        filters["status"] = status
    if customer_id is not None:
        filters["customer_id"] = customer_id
    if assignment_id is not None:
        filters["assignment_id"] = assignment_id
    if location_id is not None and actor.can_access_location(location_id):
        filters["location_id"] = location_id
    items, total = await _payments.paginate(
        page=params.page,
        page_size=params.page_size,
        search=params.search,
        sort_by=params.sort_by or "date",
        sort_order=params.sort_direction,
        filters=filters or None,
    )
    return paginated_response(PaymentResponse, items, total, params)


@router.get("/{payment_id}", summary="Get payment / receipt")
async def get_payment(
    payment_id: str,
    actor: ActorContext = Depends(require_permission("payments", "view")),
) -> dict:
    doc = await _payments.get_or_404(payment_id)
    ensure_can_access(actor, doc.location_id)
    payload = PaymentResponse.model_validate(doc).model_dump(mode="json")
    payload["bill_no_formatted"] = format_bill_no(doc.bill_no)
    return success_response(data=payload)


@router.post("", status_code=201, summary="Record client payment & generate receipt")
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
    data.setdefault("date", today_ist())
    data["receipt_no"] = await next_receipt_no(_db())
    if not data.get("received_by") and actor.user:
        data["received_by"] = actor.user.name

    if payload.assignment_id and not data.get("bill_no"):
        asg = await Assignment.get(payload.assignment_id)
        if asg and asg.bill_no is not None:
            data["bill_no"] = asg.bill_no

    doc = await _payments.create(data, actor)
    if doc.assignment_id:
        await finance_service.sync_assignment_payment_status(doc.assignment_id)

    from app.services.dashboard_service import invalidate_dashboard_cache
    invalidate_dashboard_cache()

    resp_data = PaymentResponse.model_validate(doc).model_dump(mode="json")
    resp_data["bill_no_formatted"] = format_bill_no(doc.bill_no)
    return success_response(data=resp_data, message="Payment receipt recorded")


@router.put("/{payment_id}", summary="Update payment")
async def update_payment(
    payment_id: str,
    payload: PaymentUpdate,
    actor: ActorContext = Depends(require_permission("payments", "update")),
) -> dict:
    existing = await _payments.get_or_404(payment_id)
    ensure_can_access(actor, existing.location_id)
    doc = await _payments.update(payment_id, payload.model_dump(exclude_unset=True), actor)
    if doc.assignment_id:
        await finance_service.sync_assignment_payment_status(doc.assignment_id)
    from app.services.dashboard_service import invalidate_dashboard_cache
    invalidate_dashboard_cache()
    return item_response(PaymentResponse, doc, "Payment updated")


@router.delete("/{payment_id}", summary="Delete payment")
async def delete_payment(
    payment_id: str,
    actor: ActorContext = Depends(require_permission("payments", "delete")),
) -> dict:
    existing = await _payments.get_or_404(payment_id)
    ensure_can_access(actor, existing.location_id)
    asg_id = existing.assignment_id
    await _payments.delete(payment_id, actor)
    if asg_id:
        await finance_service.sync_assignment_payment_status(asg_id)
    from app.services.dashboard_service import invalidate_dashboard_cache
    invalidate_dashboard_cache()
    return success_response(message="Payment deleted")
