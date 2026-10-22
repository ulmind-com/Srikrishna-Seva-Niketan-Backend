"""Finance documents: firm expenses and customer payments (location-scoped)."""

from __future__ import annotations

import datetime as dt
from typing import Optional

import pymongo
from beanie import Indexed
from pydantic import Field

from app.models.base import TimestampedDocument, utcnow
from app.models.enums import PaymentMode, PaymentStatus


class Expense(TimestampedDocument):
    """A firm operating expense (used for the profit & loss report)."""

    category: str  # e.g. salary, rent, transport, marketing, misc
    amount: int = Field(ge=0)
    date: dt.date = Field(default_factory=lambda: utcnow().date())
    location_id: Indexed(str)  # type: ignore[valid-type]
    note: Optional[str] = None

    class Settings:
        name = "expenses"
        indexes = [
            [("location_id", pymongo.ASCENDING)],
            [("category", pymongo.ASCENDING)],
            [("date", pymongo.DESCENDING)],
        ]


class Payment(TimestampedDocument):
    """A payment received from a customer (income / receivables tracking)."""

    customer_id: Indexed(str)  # type: ignore[valid-type]
    customer_name: str  # denormalised
    assignment_id: Optional[str] = None
    amount: int = Field(ge=0)
    date: dt.date = Field(default_factory=lambda: utcnow().date())
    mode: PaymentMode = PaymentMode.CASH
    status: PaymentStatus = PaymentStatus.PAID
    location_id: Indexed(str)  # type: ignore[valid-type]
    note: Optional[str] = None

    class Settings:
        name = "payments"
        indexes = [
            [("location_id", pymongo.ASCENDING)],
            [("customer_id", pymongo.ASCENDING)],
            [("status", pymongo.ASCENDING)],
            [("date", pymongo.DESCENDING)],
        ]
