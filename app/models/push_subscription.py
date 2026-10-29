"""Web push subscription document."""

from __future__ import annotations

import datetime as dt
from typing import Optional

import pymongo

from app.models.base import TimestampedDocument


class PushSubscription(TimestampedDocument):
    """A browser Web Push subscription for staff nurses and admins."""

    endpoint: str
    p256dh: str
    auth: str
    employee_id: Optional[str] = None
    user_id: Optional[str] = None
    user_agent: Optional[str] = None
    is_active: bool = True
    last_pushed_at: Optional[dt.datetime] = None
    failure_count: int = 0

    class Settings:
        name = "push_subscriptions"
        indexes = [
            [("endpoint", pymongo.ASCENDING)],
            [("employee_id", pymongo.ASCENDING)],
            [("user_id", pymongo.ASCENDING)],
            [("is_active", pymongo.ASCENDING)],
            [("created_at", pymongo.DESCENDING)],
        ]
