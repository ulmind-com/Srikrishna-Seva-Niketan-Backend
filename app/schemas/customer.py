"""Schemas for customers (care recipients / clients)."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from app.models.base import ImageAsset
from app.models.enums import CareType
from app.schemas.common import IdTimestampSchema


class CustomerCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)
    phone: str = Field(..., min_length=4, max_length=20)
    alt_phone: Optional[str] = Field(None, max_length=20)
    address: Optional[str] = Field(None, max_length=300)
    city: Optional[str] = Field(None, max_length=80)
    pincode: Optional[str] = Field(None, max_length=12)
    id_proof: Optional[ImageAsset] = None
    id_proof_type: Optional[str] = Field(None, max_length=40)
    id_proof_number: Optional[str] = Field(None, max_length=40)
    care_type: CareType = CareType.HOME
    service_label: Optional[str] = Field(None, max_length=200)
    duration_days: Optional[int] = Field(None, ge=0)
    one_to_one: bool = True
    location_id: Optional[str] = None
    billing_rate: Optional[int] = Field(None, ge=0)
    notes: Optional[str] = Field(None, max_length=2000)
    is_active: bool = True


class CustomerUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=120)
    phone: Optional[str] = Field(None, min_length=4, max_length=20)
    alt_phone: Optional[str] = Field(None, max_length=20)
    address: Optional[str] = Field(None, max_length=300)
    city: Optional[str] = Field(None, max_length=80)
    pincode: Optional[str] = Field(None, max_length=12)
    id_proof: Optional[ImageAsset] = None
    id_proof_type: Optional[str] = Field(None, max_length=40)
    id_proof_number: Optional[str] = Field(None, max_length=40)
    care_type: Optional[CareType] = None
    service_label: Optional[str] = Field(None, max_length=200)
    duration_days: Optional[int] = Field(None, ge=0)
    one_to_one: Optional[bool] = None
    location_id: Optional[str] = None
    billing_rate: Optional[int] = Field(None, ge=0)
    notes: Optional[str] = Field(None, max_length=2000)
    is_active: Optional[bool] = None


class CustomerResponse(IdTimestampSchema):
    code: str
    name: str
    phone: str
    alt_phone: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    pincode: Optional[str] = None
    id_proof: Optional[ImageAsset] = None
    id_proof_type: Optional[str] = None
    id_proof_number: Optional[str] = None
    care_type: CareType
    service_label: Optional[str] = None
    duration_days: Optional[int] = None
    one_to_one: bool
    location_id: str
    billing_rate: Optional[int] = None
    notes: Optional[str] = None
    is_active: bool
