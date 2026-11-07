"""Assignment / daily-shift document — staff engaged to a customer."""

from __future__ import annotations

import datetime as dt
from typing import Optional

import pymongo
from beanie import Indexed
from pydantic import Field

from app.models.base import TimestampedDocument
from app.models.enums import (
    AcceptanceStatus,
    AssignmentPlace,
    AssignmentStatus,
    PaymentStatus,
    PaymentType,
    ShiftType,
)


class Assignment(TimestampedDocument):
    """A staff member engaged to a customer for a shift over a date range."""

    customer_id: Indexed(str)  # type: ignore[valid-type]
    customer_name: str  # denormalised for fast listing
    employee_id: Indexed(str)  # type: ignore[valid-type]
    employee_name: str  # denormalised

    # Multi-location scoping (both staff & customer share this branch).
    location_id: Indexed(str)  # type: ignore[valid-type]

    # Where the staff member is posted — the customer's home, a hospital, a
    # nursing home, etc. ``place_name`` carries the hospital / nursing-home name.
    place_type: AssignmentPlace = AssignmentPlace.HOME
    place_name: Optional[str] = None
    address: Optional[str] = None

    shift: ShiftType = ShiftType.FULL_24H
    # Custom shift window as "HH:MM" (24-hour). Used when shift == CUSTOM, e.g.
    # start_time "06:00", end_time "18:00"; an overnight window (end <= start,
    # e.g. 20:00 -> 06:00) is read as finishing the next day.
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    start_date: dt.date
    end_date: Optional[dt.date] = None

    status: AssignmentStatus = AssignmentStatus.SCHEDULED

    # Nurse's response to the posting (rings in the staff portal until answered).
    acceptance_status: AcceptanceStatus = AcceptanceStatus.PENDING
    accepted_at: Optional[dt.datetime] = None
    declined_at: Optional[dt.datetime] = None
    decline_reason: Optional[str] = None

    # Destination point, snapshotted at create from the customer's pin or a
    # geocode of the address — so routing stays stable even if the customer
    # record is edited later.
    dest_lat: Optional[float] = None
    dest_lng: Optional[float] = None

    # Per-day rates — the admin enters these; the stored totals below are
    # derived as daily_rate × total_days so all existing reports keep working.
    customer_daily_rate: Optional[int] = None  # charged to the customer per day
    employee_daily_rate: Optional[int] = None  # paid to the staff member per day

    customer_rate: Optional[int] = None  # contract value — revenue for this engagement
    employee_payout: Optional[int] = None  # paid to the staff member — an expense
    other_expense: Optional[int] = None  # misc cost for this engagement (travel, kit…)

    # Sequential customer-bill number, allocated the first time a bill is
    # generated for this engagement and kept thereafter so the bill is stable.
    bill_no: Optional[int] = None

    payment_type: PaymentType = PaymentType.PREPAID
    payment_status: PaymentStatus = PaymentStatus.PENDING
    payment_received_at: Optional[dt.datetime] = None
    payment_received_by: Optional[str] = None
    work_completed_at: Optional[dt.datetime] = None

    notes: Optional[str] = None

    class Settings:
        name = "assignments"
        indexes = [
            [("location_id", pymongo.ASCENDING)],
            [("customer_id", pymongo.ASCENDING)],
            [("employee_id", pymongo.ASCENDING)],
            [("status", pymongo.ASCENDING)],
            [("acceptance_status", pymongo.ASCENDING)],
            [("start_date", pymongo.DESCENDING)],
            [("created_at", pymongo.DESCENDING)],
        ]
