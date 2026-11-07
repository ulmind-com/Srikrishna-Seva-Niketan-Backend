"""Schemas for daily attendance marking and payslip responses."""

from __future__ import annotations

import datetime as dt
from typing import List, Optional

from pydantic import BaseModel, Field, computed_field, field_validator

from app.models.enums import AttendanceStatus, ShiftType
from app.schemas.common import IdTimestampSchema


def _parse_flexible_date(v):
    """Accept ISO, dd/mm/yyyy, yyyy-mm-dd and empty-as-None date inputs."""
    if v is None or v == "":
        return None
    if isinstance(v, dt.date):
        return v
    if isinstance(v, str):
        v = v.strip()
        if not v:
            return None
        if "T" in v:
            v = v.split("T")[0]
        for sep in ("/", "-"):
            if sep in v:
                parts = v.split(sep)
                if len(parts) == 3:
                    if len(parts[2]) == 4:
                        return dt.date(int(parts[2]), int(parts[1]), int(parts[0]))
                    if len(parts[0]) == 4:
                        return dt.date(int(parts[0]), int(parts[1]), int(parts[2]))
                break
    return v


def _day_amount(status: AttendanceStatus, pay_rate: Optional[int]) -> int:
    """Rupees earned for a single day given its status and snapshotted rate."""
    rate = pay_rate or 0
    if status == AttendanceStatus.PRESENT:
        return rate
    if status == AttendanceStatus.HALF_DAY:
        return round(rate / 2)
    return 0  # leave / off earn nothing


# --------------------------------------------------------------------------
# Attendance marking
# --------------------------------------------------------------------------

class AttendanceCreate(BaseModel):
    employee_id: str
    date: dt.date
    status: AttendanceStatus = AttendanceStatus.PRESENT
    shift: ShiftType = ShiftType.FULL_24H
    start_time: Optional[str] = Field(None, max_length=5)
    end_time: Optional[str] = Field(None, max_length=5)
    assignment_id: Optional[str] = None
    customer_name: Optional[str] = Field(None, max_length=200)
    pay_rate: Optional[int] = Field(None, ge=0)
    note: Optional[str] = Field(None, max_length=1000)

    @field_validator("date", mode="before")
    @classmethod
    def _validate_date(cls, v):
        return _parse_flexible_date(v)

    @field_validator("customer_name", "note", "assignment_id", "pay_rate", "start_time", "end_time", mode="before")
    @classmethod
    def _empty_to_none(cls, v):
        return None if v == "" else v


class AttendanceBulkItem(BaseModel):
    """One staff member's marking inside a bulk (whole-roster) submission."""

    employee_id: str
    status: AttendanceStatus = AttendanceStatus.PRESENT
    shift: ShiftType = ShiftType.FULL_24H
    start_time: Optional[str] = Field(None, max_length=5)
    end_time: Optional[str] = Field(None, max_length=5)
    pay_rate: Optional[int] = Field(None, ge=0)
    note: Optional[str] = Field(None, max_length=1000)


class AttendanceBulkCreate(BaseModel):
    """Mark many staff for a single day at once — the morning-roster screen."""

    date: dt.date
    items: List[AttendanceBulkItem] = Field(..., min_length=1)

    @field_validator("date", mode="before")
    @classmethod
    def _validate_date(cls, v):
        return _parse_flexible_date(v)


class AttendanceUpdate(BaseModel):
    status: Optional[AttendanceStatus] = None
    shift: Optional[ShiftType] = None
    start_time: Optional[str] = Field(None, max_length=5)
    end_time: Optional[str] = Field(None, max_length=5)
    customer_name: Optional[str] = Field(None, max_length=200)
    pay_rate: Optional[int] = Field(None, ge=0)
    note: Optional[str] = Field(None, max_length=1000)

    @field_validator("customer_name", "note", "pay_rate", "start_time", "end_time", mode="before")
    @classmethod
    def _empty_to_none(cls, v):
        return None if v == "" else v


class AttendanceResponse(IdTimestampSchema):
    employee_id: str
    employee_name: str
    location_id: Optional[str] = None
    date: dt.date
    status: AttendanceStatus
    shift: ShiftType
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    assignment_id: Optional[str] = None
    customer_name: Optional[str] = None
    pay_rate: Optional[int] = None
    note: Optional[str] = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def payable(self) -> bool:
        """Whether this day earns anything (present or half-day)."""
        return self.status in (AttendanceStatus.PRESENT, AttendanceStatus.HALF_DAY)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def amount(self) -> int:
        """Rupees earned for this day."""
        return _day_amount(self.status, self.pay_rate)
