"""Notification service — in-app admin notifications."""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import List, Optional, Tuple

from app.core.exceptions import NotFoundException
from app.core.logging import get_logger
from app.models.enums import NotificationType
from app.models.notification import Notification
from app.repositories.base import BaseRepository
from app.services.webpush_service import webpush_service

logger = get_logger(__name__)


class NotificationService:
    """Create and manage admin notifications."""

    def __init__(self) -> None:
        self.repo: BaseRepository[Notification] = BaseRepository(Notification)
        self.repo.search_fields = ("title", "message")

    async def create(
        self,
        title: str,
        message: str = "",
        *,
        type: NotificationType = NotificationType.SYSTEM,
        user_id: Optional[str] = None,
        employee_id: Optional[str] = None,
        link: Optional[str] = None,
        reference_id: Optional[str] = None,
    ) -> Notification:
        """Create a notification.

        Broadcast to all admins when ``user_id`` is ``None``; set ``employee_id``
        to target a staff-portal employee instead of an admin.
        """
        notification = Notification(
            title=title,
            message=message,
            type=type,
            user_id=user_id,
            employee_id=employee_id,
            link=link,
            reference_id=reference_id,
        )
        created = await self.repo.create(notification)

        if employee_id:
            try:
                # Dispatch background web push to registered nurse devices
                asyncio.create_task(
                    webpush_service.send_to_employee(
                        employee_id=employee_id,
                        title=title,
                        message=message,
                        url=link or "/staff/status",
                        data={"reference_id": reference_id, "type": type.value},
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Background webpush trigger caught exception",
                    extra={"error": str(exc), "employee_id": employee_id},
                )
        else:
            try:
                # Broadcast web push to registered admin devices
                asyncio.create_task(
                    webpush_service.send_to_admins(
                        title=title,
                        message=message,
                        url=link or "/bookings",
                        data={"reference_id": reference_id, "type": type.value},
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Admin webpush trigger caught exception",
                    extra={"error": str(exc)},
                )

        return created

    async def paginate(
        self,
        user_id: str,
        page: int,
        page_size: int,
        is_read: Optional[bool] = None,
    ) -> Tuple[List[Notification], int]:
        """List notifications visible to ``user_id`` (own + broadcast).

        Staff-targeted notifications (``employee_id`` set) are excluded.
        """
        filters: dict = {
            "employee_id": None,
            "$or": [{"user_id": user_id}, {"user_id": None}],
        }
        if is_read is not None:
            filters = {"$and": [filters, {"is_read": is_read}]}
        return await self.repo.paginate(
            filters=filters, page=page, page_size=page_size
        )

    async def unread_count(self, user_id: str) -> int:
        """Count unread admin notifications for ``user_id`` (own + broadcast)."""
        return await self.repo.count(
            {
                "$and": [
                    {"employee_id": None},
                    {"$or": [{"user_id": user_id}, {"user_id": None}]},
                    {"is_read": False},
                ]
            }
        )

    # ---- Staff-portal (employee) notifications ----

    async def paginate_for_employee(
        self,
        employee_id: str,
        page: int,
        page_size: int,
        is_read: Optional[bool] = None,
    ) -> Tuple[List[Notification], int]:
        """List a staff-portal employee's own notifications."""
        filters: dict = {"employee_id": employee_id}
        if is_read is not None:
            filters["is_read"] = is_read
        return await self.repo.paginate(
            filters=filters, page=page, page_size=page_size
        )

    async def unread_count_for_employee(self, employee_id: str) -> int:
        """Count an employee's unread notifications."""
        return await self.repo.count(
            {"employee_id": employee_id, "is_read": False}
        )

    async def mark_read_for_employee(
        self, notification_id: str, employee_id: str
    ) -> Notification:
        """Mark one of the employee's own notifications as read."""
        notification = await self.repo.get(notification_id)
        if notification is None or notification.employee_id != employee_id:
            raise NotFoundException("Notification not found")
        notification.is_read = True
        notification.read_at = dt.datetime.now(dt.timezone.utc)
        notification.touch()
        await notification.save()
        return notification

    async def mark_all_read_for_employee(self, employee_id: str) -> int:
        """Mark all of an employee's notifications as read."""
        now = dt.datetime.now(dt.timezone.utc)
        result = await Notification.find(
            {"employee_id": employee_id, "is_read": False}
        ).update({"$set": {"is_read": True, "read_at": now, "updated_at": now}})
        return getattr(result, "modified_count", 0)

    async def mark_read(self, notification_id: str, user_id: str) -> Notification:
        """Mark a single notification as read."""
        notification = await self.repo.get(notification_id)
        if notification is None:
            raise NotFoundException("Notification not found")
        notification.is_read = True
        notification.read_at = dt.datetime.now(dt.timezone.utc)
        notification.touch()
        await notification.save()
        return notification

    async def mark_all_read(self, user_id: str) -> int:
        """Mark all of a user's notifications as read; returns updated count."""
        now = dt.datetime.now(dt.timezone.utc)
        result = await Notification.find(
            {
                "$and": [
                    {"employee_id": None},
                    {"$or": [{"user_id": user_id}, {"user_id": None}]},
                    {"is_read": False},
                ]
            }
        ).update({"$set": {"is_read": True, "read_at": now, "updated_at": now}})
        return getattr(result, "modified_count", 0)

    async def mark_read_by_reference(self, reference_id: str) -> int:
        """Mark all notifications for a specific entity reference as read."""
        now = dt.datetime.now(dt.timezone.utc)
        result = await Notification.find(
            {"reference_id": reference_id, "is_read": False}
        ).update({"$set": {"is_read": True, "read_at": now, "updated_at": now}})
        return getattr(result, "modified_count", 0)

    async def delete(self, notification_id: str) -> None:
        """Delete a notification."""
        deleted = await self.repo.delete_by_id(notification_id)
        if not deleted:
            raise NotFoundException("Notification not found")


notification_service = NotificationService()
