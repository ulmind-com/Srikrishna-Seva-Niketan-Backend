"""Schemas for assignments (staff-to-customer daily shifts)."""

from __future__ import annotations

import datetime as dt
from typing import Optional

from pydantic import BaseModel, Field

from app.models.enums import AssignmentStatus, ShiftType
from app.schemas.common import IdTimestampSchema


class AssignmentCreate(BaseModel):
    customer_id: str
    employee_id: str
    shift: ShiftType = ShiftType.FULL_24H
    start_date: dt.date
    end_date: Optional[dt.date] = None
    status: AssignmentStatus = AssignmentStatus.SCHEDULED
    customer_rate: Optional[int] = Field(None, ge=0)
    employee_payout: Optional[int] = Field(None, ge=0)
    notes: Optional[str] = Field(None, max_length=2000)
    # location_id is derived from the customer/employee, not supplied directly.


class AssignmentUpdate(BaseModel):
    shift: Optional[ShiftType] = None
    start_date: Optional[dt.date] = None
    end_date: Optional[dt.date] = None
    status: Optional[AssignmentStatus] = None
    customer_rate: Optional[int] = Field(None, ge=0)
    employee_payout: Optional[int] = Field(None, ge=0)
    notes: Optional[str] = Field(None, max_length=2000)


class AssignmentResponse(IdTimestampSchema):
    customer_id: str
    customer_name: str
    employee_id: str
    employee_name: str
    location_id: str
    shift: ShiftType
    start_date: dt.date
    end_date: Optional[dt.date] = None
    status: AssignmentStatus
    customer_rate: Optional[int] = None
    employee_payout: Optional[int] = None
    notes: Optional[str] = None
