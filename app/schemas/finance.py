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
    monthly_bill_id: Optional[str] = None
    bill_no: Optional[int] = None
    amount: int = Field(..., ge=0)
    date: Optional[dt.date] = None
    mode: PaymentMode = PaymentMode.CASH
    status: PaymentStatus = PaymentStatus.PAID
    transaction_id: Optional[str] = Field(None, max_length=120)
    received_by: Optional[str] = Field(None, max_length=120)
    note: Optional[str] = Field(None, max_length=2000)


class PaymentUpdate(BaseModel):
    amount: Optional[int] = Field(None, ge=0)
    date: Optional[dt.date] = None
    mode: Optional[PaymentMode] = None
    status: Optional[PaymentStatus] = None
    transaction_id: Optional[str] = Field(None, max_length=120)
    note: Optional[str] = Field(None, max_length=2000)


class PaymentResponse(IdTimestampSchema):
    receipt_no: Optional[str] = None
    customer_id: str
    customer_name: str
    assignment_id: Optional[str] = None
    monthly_bill_id: Optional[str] = None
    bill_no: Optional[int] = None
    amount: int
    date: dt.date
    mode: PaymentMode
    status: PaymentStatus
    transaction_id: Optional[str] = None
    received_by: Optional[str] = None
    location_id: str
    note: Optional[str] = None


# ---- Staff Salary Payment ----


class StaffPaymentCreate(BaseModel):
    employee_id: str
    month: Optional[str] = Field(None, max_length=10)
    assignment_id: Optional[str] = None
    amount: int = Field(..., gt=0)
    date: Optional[dt.date] = None
    mode: PaymentMode = PaymentMode.CASH
    transaction_id: Optional[str] = Field(None, max_length=120)
    paid_by: Optional[str] = Field(None, max_length=120)
    note: Optional[str] = Field(None, max_length=2000)


class StaffPaymentUpdate(BaseModel):
    amount: Optional[int] = Field(None, gt=0)
    date: Optional[dt.date] = None
    month: Optional[str] = Field(None, max_length=10)
    mode: Optional[PaymentMode] = None
    transaction_id: Optional[str] = Field(None, max_length=120)
    note: Optional[str] = Field(None, max_length=2000)


class StaffPaymentResponse(IdTimestampSchema):
    voucher_no: Optional[str] = None
    employee_id: str
    employee_name: str
    month: Optional[str] = None
    assignment_id: Optional[str] = None
    amount: int
    date: dt.date
    mode: PaymentMode
    transaction_id: Optional[str] = None
    paid_by: Optional[str] = None
    location_id: str
    note: Optional[str] = None
