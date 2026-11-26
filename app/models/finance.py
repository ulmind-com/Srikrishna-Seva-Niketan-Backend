"""Finance documents: firm expenses and customer payments (location-scoped)."""

from __future__ import annotations

import datetime as dt
from typing import Optional

import pymongo
from beanie import Indexed
from pydantic import Field

from app.models.base import TimestampedDocument
from app.models.enums import PaymentMode, PaymentStatus
from app.utils.ist_time import today_ist


class Expense(TimestampedDocument):
    """A firm operating expense (used for the profit & loss report)."""

    category: str  # e.g. salary, rent, transport, marketing, misc
    amount: int = Field(ge=0)
    date: dt.date = Field(default_factory=today_ist)
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
    """A payment received from a customer (client receipt / partial & full tracking)."""

    receipt_no: Optional[str] = None  # sequential RCP-001, RCP-002...
    customer_id: Indexed(str)  # type: ignore[valid-type]
    customer_name: str  # denormalised
    assignment_id: Optional[str] = None
    monthly_bill_id: Optional[str] = None
    bill_no: Optional[int] = None
    amount: int = Field(ge=0)
    date: dt.date = Field(default_factory=today_ist)
    mode: PaymentMode = PaymentMode.CASH
    status: PaymentStatus = PaymentStatus.PAID
    transaction_id: Optional[str] = None
    received_by: Optional[str] = None
    location_id: Indexed(str)  # type: ignore[valid-type]
    note: Optional[str] = None

    class Settings:
        name = "payments"
        indexes = [
            [("location_id", pymongo.ASCENDING)],
            [("customer_id", pymongo.ASCENDING)],
            [("assignment_id", pymongo.ASCENDING)],
            [("status", pymongo.ASCENDING)],
            [("date", pymongo.DESCENDING)],
        ]


class StaffPayment(TimestampedDocument):
    """Actual salary / payout disbursed to a staff member (supports partial payments)."""

    voucher_no: Optional[str] = None  # sequential PAY-001, PAY-002...
    employee_id: Indexed(str)  # type: ignore[valid-type]
    employee_name: str
    month: Optional[str] = None  # YYYY-MM reference month
    assignment_id: Optional[str] = None
    amount: int = Field(ge=0)
    date: dt.date = Field(default_factory=today_ist)
    mode: PaymentMode = PaymentMode.CASH
    transaction_id: Optional[str] = None
    paid_by: Optional[str] = None
    location_id: Indexed(str)  # type: ignore[valid-type]
    note: Optional[str] = None

    class Settings:
        name = "staff_payments"
        indexes = [
            [("location_id", pymongo.ASCENDING)],
            [("employee_id", pymongo.ASCENDING)],
            [("month", pymongo.ASCENDING)],
            [("date", pymongo.DESCENDING)],
        ]
