"""Branch / office location document (multi-location staff management)."""

from __future__ import annotations

from typing import Optional

import pymongo
from beanie import Indexed
from pydantic import Field

from app.models.base import TimestampedDocument


class Location(TimestampedDocument):
    """A physical branch / office of the agency.

    Every operational record (employee, customer, assignment, expense,
    payment) carries a ``location_id`` pointing here. Managers are scoped to
    one or more locations; super-admins see them all.
    """

    name: str
    code: Indexed(str, unique=True)  # type: ignore[valid-type]  # e.g. "SSN-KOL"
    address: Optional[str] = None
    city: Optional[str] = None
    pincode: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    is_active: bool = True
    order: int = 0

    class Settings:
        name = "locations"
        indexes = [
            [("code", pymongo.ASCENDING)],
            [("is_active", pymongo.ASCENDING)],
            [("city", pymongo.ASCENDING)],
            [("order", pymongo.ASCENDING)],
        ]
