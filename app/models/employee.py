"""Operational staff member (nurse / aaya / attendant) — HR record.

Distinct from :class:`app.models.staff.StaffMember`, which is the public
website's marketing catalogue. This is the internal, location-scoped HR record
used for assignments, ID cards, billing and reports.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

import pymongo
from beanie import Indexed
from pydantic import Field

from app.models.base import ImageAsset, TimestampedDocument
from app.models.enums import EmployeeStatus, Gender


class Employee(TimestampedDocument):
    """A registered field staff member belonging to a branch."""

    code: Indexed(str, unique=True)  # type: ignore[valid-type]  # e.g. EMP-20260728-A1B2
    name: str
    phone: str
    alt_phone: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    pincode: Optional[str] = None

    category: str = "nurse"  # nurse / aaya / attendant / physio / japa / other
    gender: Optional[Gender] = None
    experience: Optional[str] = None  # e.g. "3 years"

    photo: Optional[ImageAsset] = None
    signature: Optional[ImageAsset] = None
    id_proof: Optional[ImageAsset] = None
    id_proof_type: Optional[str] = None  # Aadhaar / Voter / PAN ...
    id_proof_number: Optional[str] = None

    # Multi-location scoping.
    location_id: Indexed(str)  # type: ignore[valid-type]

    status: EmployeeStatus = EmployeeStatus.AVAILABLE
    rating: float = Field(default=5.0, ge=1.0, le=5.0)
    daily_rate: Optional[int] = None  # payout to the staff member (expense)
    join_date: Optional[dt.date] = None
    notes: Optional[str] = None
    is_active: bool = True

    class Settings:
        name = "employees"
        indexes = [
            [("code", pymongo.ASCENDING)],
            [("location_id", pymongo.ASCENDING)],
            [("status", pymongo.ASCENDING)],
            [("category", pymongo.ASCENDING)],
            [("is_active", pymongo.ASCENDING)],
            [("name", pymongo.TEXT), ("phone", pymongo.TEXT)],
        ]
