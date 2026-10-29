"""Notification endpoints for admin users."""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.api.helpers import paginated_response
from app.core.exceptions import BadRequestException, NotFoundException
from app.core.pagination import PaginationParams, pagination_params
from app.core.responses import success_response
from app.dependencies.auth import ActorContext, get_actor, require_permission
from app.models.employee import Employee
from app.models.enums import NotificationType
from app.schemas.misc import NotificationResponse
from app.services.notification_service import notification_service
from app.services.webpush_service import webpush_service

router = APIRouter(prefix="/notifications", tags=["Notifications"])


class PushDispatchPayload(BaseModel):
    """Payload to dispatch push alert to staff/nurses or office team."""

    target: Literal["all_nurses", "employee", "all_admins"] = "all_nurses"
    employee_id: Optional[str] = None
    title: str = Field(..., min_length=1, max_length=120)
    message: str = Field(..., min_length=1, max_length=500)
    urgency: Literal["normal", "high"] = "high"
    link: Optional[str] = None


@router.get("", summary="List my notifications")
async def list_notifications(
    params: PaginationParams = Depends(pagination_params),
    is_read: Optional[bool] = Query(None),
    actor: ActorContext = Depends(get_actor),
) -> dict:
    items, total = await notification_service.paginate(
        user_id=actor.user_id, page=params.page, page_size=params.page_size,
        is_read=is_read,
    )
    return paginated_response(NotificationResponse, items, total, params)


@router.get("/unread-count", summary="Unread notification count")
async def unread_count(actor: ActorContext = Depends(get_actor)) -> dict:
    count = await notification_service.unread_count(actor.user_id)
    return success_response(data={"unread": count}, message="Unread count fetched")


@router.post("/{notification_id}/read", summary="Mark notification as read")
async def mark_read(
    notification_id: str,
    actor: ActorContext = Depends(get_actor),
) -> dict:
    notification = await notification_service.mark_read(notification_id, actor.user_id)
    return success_response(
        data=NotificationResponse.model_validate(notification).model_dump(mode="json"),
        message="Notification marked as read",
    )


@router.post("/read-all", summary="Mark all notifications as read")
async def mark_all_read(actor: ActorContext = Depends(get_actor)) -> dict:
    updated = await notification_service.mark_all_read(actor.user_id)
    return success_response(data={"updated": updated}, message="All marked as read")


@router.delete("/{notification_id}", summary="Delete a notification")
async def delete_notification(
    notification_id: str,
    _: ActorContext = Depends(require_permission("notifications", "delete")),
) -> dict:
    await notification_service.delete(notification_id)
    return success_response(message="Notification deleted")


@router.get("/push-status", summary="Get push notification stats")
async def get_push_status(_: ActorContext = Depends(get_actor)) -> dict:
    """Return count of registered active staff push devices."""
    count = await webpush_service.count_active_staff_devices()
    return success_response(
        data={"active_staff_devices": count},
        message="Push status retrieved",
    )


@router.post("/push-dispatch", summary="Dispatch push notification to staff/admins")
async def dispatch_push_notification(
    payload: PushDispatchPayload,
    actor: ActorContext = Depends(get_actor),
) -> dict:
    """Send an immediate Web Push notification to nurses or office team."""
    sent_count = 0
    total_count = 0

    if payload.target == "employee":
        if not payload.employee_id:
            raise BadRequestException("employee_id is required when target is 'employee'")
        emp = await Employee.get(payload.employee_id)
        if not emp:
            raise NotFoundException("Employee not found")

        # 1. Create in-app notification for the nurse
        await notification_service.create(
            title=payload.title,
            message=payload.message,
            type=NotificationType.SYSTEM,
            employee_id=str(emp.id),
            link=payload.link or "/staff/status",
        )
        # 2. Trigger webpush
        res = await webpush_service.send_to_employee(
            employee_id=str(emp.id),
            title=payload.title,
            message=payload.message,
            url=payload.link or "/staff/status",
            urgency=payload.urgency,
        )
        sent_count = res.get("sent", 0)
        total_count = res.get("total", 0)

    elif payload.target == "all_nurses":
        # 1. Web push to all active employee devices
        res = await webpush_service.send_to_all_employees(
            title=payload.title,
            message=payload.message,
            url=payload.link or "/staff/status",
            urgency=payload.urgency,
        )
        sent_count = res.get("sent", 0)
        total_count = res.get("total", 0)

        # 2. Also create in-app notification for active employees
        active_employees = await Employee.find({"status": {"$ne": "inactive"}}).to_list()
        for emp in active_employees:
            await notification_service.create(
                title=payload.title,
                message=payload.message,
                type=NotificationType.SYSTEM,
                employee_id=str(emp.id),
                link=payload.link or "/staff/status",
            )

    elif payload.target == "all_admins":
        await notification_service.create(
            title=payload.title,
            message=payload.message,
            type=NotificationType.SYSTEM,
            user_id=None,
            link=payload.link or "/notifications",
        )
        sent_count = 1

    return success_response(
        data={"sent": sent_count, "total_targets": total_count},
        message=f"Push notification dispatched ({sent_count} device(s) notified)",
    )
