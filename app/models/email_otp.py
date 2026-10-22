"""Email OTP verification document with auto-expiring TTL."""

from __future__ import annotations

import datetime as dt
from typing import Optional

import pymongo
from beanie import Indexed

from app.models.base import TimestampedDocument


class EmailOtp(TimestampedDocument):
    """Store of email verification OTP codes with TTL auto-cleanup."""

    email: Indexed(str)  # type: ignore[valid-type]
    otp: str
    purpose: str = "staff_registration"
    is_verified: bool = False
    verified_at: Optional[dt.datetime] = None
    expires_at: dt.datetime

    class Settings:
        name = "email_otps"
        indexes = [
            [("email", pymongo.ASCENDING)],
            [("email", pymongo.ASCENDING), ("is_verified", pymongo.ASCENDING)],
            pymongo.IndexModel(
                [("expires_at", pymongo.ASCENDING)], expireAfterSeconds=0
            ),
        ]
