"""Daily attendance record — the payroll source of truth.

One document per staff member per calendar day. Payslips are computed by
aggregating these records over a month (see
:mod:`app.services.payroll_service`); nothing about pay is stored twice, so a
payslip can never drift from the attendance it is derived from.

``pay_rate`` is snapshotted from the employee's ``daily_rate`` at the moment the
day is marked, so a later rate change never silently rewrites past pay.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

import pymongo
from beanie import Indexed
from pydantic import Field

from app.models.base import TimestampedDocument
from app.models.enums import AttendanceStatus, ShiftType


class AttendanceRecord(TimestampedDocument):
    """What one staff member did on one day — present / half / leave / off."""

    employee_id: Indexed(str)  # type: ignore[valid-type]
    employee_name: str  # denormalised for fast listing and payslip printing

    # Multi-location scoping, mirroring the rest of the operational modules.
    location_id: Indexed(str)  # type: ignore[valid-type]

    date: dt.date

    status: AttendanceStatus = AttendanceStatus.PRESENT
    # Shift worked that day. Meaningful only when ``status`` is present/half.
    shift: ShiftType = ShiftType.FULL_24H
    # Custom shift window ("HH:MM"), snapshotted from the assignment so the
    # payslip / attendance report can show the exact hours worked.
    start_time: Optional[str] = None
    end_time: Optional[str] = None

    # Optional link back to the customer engagement this day was served under,
    # so the payslip can show "where" the staff member worked. Populated when a
    # day is synced from a running assignment.
    assignment_id: Optional[str] = None
    customer_name: Optional[str] = None

    # Payout rate for this one day, snapshotted from ``Employee.daily_rate`` at
    # marking time. Leave / off days keep their rate for reference but earn 0.
    pay_rate: Optional[int] = Field(default=None, ge=0)

    note: Optional[str] = None

    class Settings:
        name = "attendance"
        indexes = [
            [("employee_id", pymongo.ASCENDING)],
            [("location_id", pymongo.ASCENDING)],
            [("date", pymongo.DESCENDING)],
            [("status", pymongo.ASCENDING)],
            # A staff member can have more than one row per day (e.g. a day shift
            # at one client and a night shift at another), so there is no unique
            # (employee, date) constraint. De-duplication is handled in code by
            # (employee, date, assignment_id) for synced rows and
            # (employee, date, shift) for hand-marked ones.
            [("employee_id", pymongo.ASCENDING), ("date", pymongo.ASCENDING)],
        ]
