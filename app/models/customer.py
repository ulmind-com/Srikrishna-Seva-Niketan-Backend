"""Customer (care recipient / client) document — location-scoped."""

from __future__ import annotations

from typing import Optional

import pymongo
from beanie import Indexed
from pydantic import Field

from app.models.base import ImageAsset, TimestampedDocument
from app.models.enums import CareType


class Customer(TimestampedDocument):
    """A client who books staff for home / nursing-home / hospital care."""

    code: Indexed(str, unique=True)  # type: ignore[valid-type]  # e.g. CUST-4F2A9C
    name: str
    phone: str
    alt_phone: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    pincode: Optional[str] = None

    id_proof: Optional[ImageAsset] = None
    id_proof_type: Optional[str] = None
    id_proof_number: Optional[str] = None

    care_type: CareType = CareType.HOME
    service_label: Optional[str] = None  # e.g. "24 hrs Health Attendant"
    duration_days: Optional[int] = None  # requested engagement length
    one_to_one: bool = True  # one dedicated staff member per customer

    # Multi-location scoping.
    location_id: Indexed(str)  # type: ignore[valid-type]

    billing_rate: Optional[int] = None  # amount charged to the customer (income)
    notes: Optional[str] = None
    is_active: bool = True

    class Settings:
        name = "customers"
        indexes = [
            [("code", pymongo.ASCENDING)],
            [("location_id", pymongo.ASCENDING)],
            [("care_type", pymongo.ASCENDING)],
            [("is_active", pymongo.ASCENDING)],
            [("name", pymongo.TEXT), ("phone", pymongo.TEXT)],
        ]
