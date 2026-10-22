"""Schemas for operational staff (employees)."""

from __future__ import annotations

import datetime as dt
from typing import Optional

from pydantic import BaseModel, Field

from app.models.base import ImageAsset
from app.models.enums import EmployeeStatus, Gender
from app.schemas.common import IdTimestampSchema


class EmployeeCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)
    phone: str = Field(..., min_length=4, max_length=20)
    alt_phone: Optional[str] = Field(None, max_length=20)
    address: Optional[str] = Field(None, max_length=300)
    city: Optional[str] = Field(None, max_length=80)
    pincode: Optional[str] = Field(None, max_length=12)
    category: str = Field("nurse", max_length=60)
    gender: Optional[Gender] = None
    experience: Optional[str] = Field(None, max_length=100)
    photo: Optional[ImageAsset] = None
    signature: Optional[ImageAsset] = None
    id_proof: Optional[ImageAsset] = None
    id_proof_type: Optional[str] = Field(None, max_length=40)
    id_proof_number: Optional[str] = Field(None, max_length=40)
    location_id: Optional[str] = None  # required for unscoped users; defaulted for managers
    status: EmployeeStatus = EmployeeStatus.AVAILABLE
    rating: float = Field(5.0, ge=1.0, le=5.0)
    daily_rate: Optional[int] = Field(None, ge=0)
    join_date: Optional[dt.date] = None
    notes: Optional[str] = Field(None, max_length=2000)
    is_active: bool = True


class EmployeeUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=120)
    phone: Optional[str] = Field(None, min_length=4, max_length=20)
    alt_phone: Optional[str] = Field(None, max_length=20)
    address: Optional[str] = Field(None, max_length=300)
    city: Optional[str] = Field(None, max_length=80)
    pincode: Optional[str] = Field(None, max_length=12)
    category: Optional[str] = Field(None, max_length=60)
    gender: Optional[Gender] = None
    experience: Optional[str] = Field(None, max_length=100)
    photo: Optional[ImageAsset] = None
    signature: Optional[ImageAsset] = None
    id_proof: Optional[ImageAsset] = None
    id_proof_type: Optional[str] = Field(None, max_length=40)
    id_proof_number: Optional[str] = Field(None, max_length=40)
    location_id: Optional[str] = None
    status: Optional[EmployeeStatus] = None
    rating: Optional[float] = Field(None, ge=1.0, le=5.0)
    daily_rate: Optional[int] = Field(None, ge=0)
    join_date: Optional[dt.date] = None
    notes: Optional[str] = Field(None, max_length=2000)
    is_active: Optional[bool] = None


class EmployeeResponse(IdTimestampSchema):
    code: str
    name: str
    phone: str
    alt_phone: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    pincode: Optional[str] = None
    category: str
    gender: Optional[Gender] = None
    experience: Optional[str] = None
    photo: Optional[ImageAsset] = None
    signature: Optional[ImageAsset] = None
    id_proof: Optional[ImageAsset] = None
    id_proof_type: Optional[str] = None
    id_proof_number: Optional[str] = None
    location_id: str
    status: EmployeeStatus
    rating: float
    daily_rate: Optional[int] = None
    join_date: Optional[dt.date] = None
    notes: Optional[str] = None
    is_active: bool
