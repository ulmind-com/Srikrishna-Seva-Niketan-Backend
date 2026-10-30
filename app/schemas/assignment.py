"""Schemas for assignments (staff-to-customer daily shifts)."""

from __future__ import annotations

import datetime as dt
from typing import Optional

from pydantic import BaseModel, Field, computed_field, field_validator

from app.models.enums import (
    AcceptanceStatus,
    AssignmentPlace,
    AssignmentStatus,
    PaymentStatus,
    PaymentType,
    ShiftType,
)
from app.schemas.common import IdTimestampSchema


class AssignmentCreate(BaseModel):
    customer_id: str
    employee_id: str
    place_type: AssignmentPlace = AssignmentPlace.HOME
    place_name: Optional[str] = Field(None, max_length=200)
    address: Optional[str] = Field(None, max_length=500)
    shift: ShiftType = ShiftType.FULL_24H
    start_date: dt.date
    end_date: Optional[dt.date] = None
    # Convenience input: how many days the engagement runs for. When given
    # without an explicit end_date the router derives end_date from it.
    duration_days: Optional[int] = Field(None, ge=1, le=3650)
    status: AssignmentStatus = AssignmentStatus.SCHEDULED
    payment_type: Optional[PaymentType] = PaymentType.PREPAID
    payment_status: Optional[PaymentStatus] = PaymentStatus.PENDING
    customer_rate: Optional[int] = Field(None, ge=0)
    employee_payout: Optional[int] = Field(None, ge=0)
    other_expense: Optional[int] = Field(None, ge=0)
    notes: Optional[str] = Field(None, max_length=2000)
    # Explicit confirmed coordinates (admin pin drop on the assignment)
    dest_lat: Optional[float] = Field(None, ge=-90, le=90)
    dest_lng: Optional[float] = Field(None, ge=-180, le=180)

    @field_validator(
        "end_date",
        "duration_days",
        "customer_rate",
        "employee_payout",
        "other_expense",
        "dest_lat",
        "dest_lng",
        "place_name",
        "address",
        "notes",
        mode="before",
    )
    @classmethod
    def sanitize_empty_values(cls, v):
        if v == "" or v is None:
            return None
        return v
    # location_id is derived from the customer/employee, not supplied directly.


class AssignmentUpdate(BaseModel):
    place_type: Optional[AssignmentPlace] = None
    place_name: Optional[str] = Field(None, max_length=200)
    address: Optional[str] = Field(None, max_length=500)
    shift: Optional[ShiftType] = None
    start_date: Optional[dt.date] = None
    end_date: Optional[dt.date] = None
    duration_days: Optional[int] = Field(None, ge=1, le=3650)
    status: Optional[AssignmentStatus] = None
    payment_type: Optional[PaymentType] = None
    payment_status: Optional[PaymentStatus] = None
    payment_received_at: Optional[dt.datetime] = None
    payment_received_by: Optional[str] = None
    work_completed_at: Optional[dt.datetime] = None
    customer_rate: Optional[int] = Field(None, ge=0)
    employee_payout: Optional[int] = Field(None, ge=0)
    other_expense: Optional[int] = Field(None, ge=0)
    notes: Optional[str] = Field(None, max_length=2000)
    dest_lat: Optional[float] = Field(None, ge=-90, le=90)
    dest_lng: Optional[float] = Field(None, ge=-180, le=180)

    @field_validator(
        "end_date",
        "duration_days",
        "customer_rate",
        "employee_payout",
        "other_expense",
        "dest_lat",
        "dest_lng",
        "place_name",
        "address",
        "notes",
        mode="before",
    )
    @classmethod
    def sanitize_empty_values(cls, v):
        if v == "" or v is None:
            return None
        return v


class AssignmentResponse(IdTimestampSchema):
    customer_id: str
    customer_name: str
    employee_id: str
    employee_name: str
    location_id: str
    place_type: AssignmentPlace = AssignmentPlace.HOME
    place_name: Optional[str] = None
    address: Optional[str] = None
    shift: ShiftType
    start_date: dt.date
    end_date: Optional[dt.date] = None
    status: AssignmentStatus
    payment_type: PaymentType = PaymentType.PREPAID
    payment_status: PaymentStatus = PaymentStatus.PENDING
    payment_received_at: Optional[dt.datetime] = None
    payment_received_by: Optional[str] = None
    work_completed_at: Optional[dt.datetime] = None
    acceptance_status: AcceptanceStatus = AcceptanceStatus.PENDING
    accepted_at: Optional[dt.datetime] = None
    declined_at: Optional[dt.datetime] = None
    decline_reason: Optional[str] = None
    dest_lat: Optional[float] = None
    dest_lng: Optional[float] = None
    customer_rate: Optional[int] = None
    employee_payout: Optional[int] = None
    other_expense: Optional[int] = None
    notes: Optional[str] = None

    # ---- Derived progress (so the admin sees how much of the engagement is
    # done and how many days are still left, without computing it client-side).

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_days(self) -> Optional[int]:
        """Length of the engagement in days, or None when open-ended."""
        if self.end_date is None:
            return None
        return (self.end_date - self.start_date).days + 1

    @computed_field  # type: ignore[prop-decorator]
    @property
    def days_elapsed(self) -> int:
        """Days already completed — today is still counted as remaining."""
        today = dt.date.today()
        if today <= self.start_date:
            return 0
        if self.end_date is not None and today > self.end_date:
            return self.total_days or 0
        return (today - self.start_date).days

    @computed_field  # type: ignore[prop-decorator]
    @property
    def days_remaining(self) -> Optional[int]:
        """Days still to serve including today, or None when open-ended."""
        total = self.total_days
        if total is None:
            return None
        return max(0, total - self.days_elapsed)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def progress_pct(self) -> Optional[int]:
        """How far through the engagement we are, 0-100."""
        total = self.total_days
        if not total:
            return None
        return min(100, round(self.days_elapsed / total * 100))


class StaffAssignmentResponse(IdTimestampSchema):
    """Assignment as the nurse sees it — deliberately omits every money field.

    No ``customer_rate`` / ``employee_payout`` / ``other_expense`` / ``notes``
    exists on this class, so price can never leak to the staff portal.
    ``customer_phone`` is denormalised by the router from the Customer record.
    """

    customer_name: str
    customer_phone: Optional[str] = None
    place_type: AssignmentPlace = AssignmentPlace.HOME
    place_name: Optional[str] = None
    address: Optional[str] = None
    shift: ShiftType
    start_date: dt.date
    end_date: Optional[dt.date] = None
    acceptance_status: AcceptanceStatus
    status: AssignmentStatus = AssignmentStatus.SCHEDULED
    payment_type: PaymentType = PaymentType.PREPAID
    payment_status: PaymentStatus = PaymentStatus.PENDING
    collect_amount: Optional[int] = None
    work_completed_at: Optional[dt.datetime] = None
    dest_lat: Optional[float] = None
    dest_lng: Optional[float] = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_days(self) -> Optional[int]:
        if self.end_date is None:
            return None
        return (self.end_date - self.start_date).days + 1

    @computed_field  # type: ignore[prop-decorator]
    @property
    def days_remaining(self) -> Optional[int]:
        total = self.total_days
        if total is None:
            return None
        today = dt.date.today()
        if today <= self.start_date:
            return total
        if self.end_date is not None and today > self.end_date:
            return 0
        return max(0, total - (today - self.start_date).days)
