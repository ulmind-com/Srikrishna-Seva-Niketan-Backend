"""Schemas for expenses and customer payments."""

from __future__ import annotations

import datetime as dt
from typing import Optional

from pydantic import BaseModel, Field

from app.models.enums import PaymentMode, PaymentStatus
from app.schemas.common import IdTimestampSchema


# ---- Expense ----


class ExpenseCreate(BaseModel):
    category: str = Field(..., min_length=1, max_length=60)
    amount: int = Field(..., ge=0)
    date: Optional[dt.date] = None
    location_id: Optional[str] = None
    note: Optional[str] = Field(None, max_length=2000)


class ExpenseUpdate(BaseModel):
    category: Optional[str] = Field(None, min_length=1, max_length=60)
    amount: Optional[int] = Field(None, ge=0)
    date: Optional[dt.date] = None
    location_id: Optional[str] = None
    note: Optional[str] = Field(None, max_length=2000)


class ExpenseResponse(IdTimestampSchema):
    category: str
    amount: int
    date: dt.date
    location_id: str
    note: Optional[str] = None


# ---- Payment ----


class PaymentCreate(BaseModel):
    customer_id: str
    assignment_id: Optional[str] = None
    amount: int = Field(..., ge=0)
    date: Optional[dt.date] = None
    mode: PaymentMode = PaymentMode.CASH
    status: PaymentStatus = PaymentStatus.PAID
    note: Optional[str] = Field(None, max_length=2000)
    # location_id + customer_name derived from the customer.


class PaymentUpdate(BaseModel):
    amount: Optional[int] = Field(None, ge=0)
    date: Optional[dt.date] = None
    mode: Optional[PaymentMode] = None
    status: Optional[PaymentStatus] = None
    note: Optional[str] = Field(None, max_length=2000)


class PaymentResponse(IdTimestampSchema):
    customer_id: str
    customer_name: str
    assignment_id: Optional[str] = None
    amount: int
    date: dt.date
    mode: PaymentMode
    status: PaymentStatus
    location_id: str
    note: Optional[str] = None
