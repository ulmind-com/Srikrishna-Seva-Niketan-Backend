"""Schemas for customers (care recipients / clients)."""

from __future__ import annotations

import datetime as dt
from typing import Any, List, Optional

from pydantic import BaseModel, Field, field_validator

from app.models.base import IdentityDocument, ImageAsset
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
    documents: Optional[List[IdentityDocument]] = None
    care_type: CareType = CareType.HOME
    purpose: Optional[str] = Field(None, max_length=200)
    agreement_date: Optional[dt.date] = None
    service_label: Optional[str] = Field(None, max_length=200)
    duration_days: Optional[int] = Field(None, ge=0)
    one_to_one: bool = True
    location_id: Optional[str] = None
    lat: Optional[float] = Field(None, ge=-90, le=90)
    lng: Optional[float] = Field(None, ge=-180, le=180)
    billing_rate: Optional[int] = Field(None, ge=0)
    notes: Optional[str] = Field(None, max_length=2000)
    is_active: bool = True

    @field_validator("agreement_date", mode="before")
    @classmethod
    def clean_agreement_date(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()
            return v if v else None
        return v

    @field_validator("purpose", mode="before")
    @classmethod
    def clean_purpose(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()
            return v if v else None
        return v


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
    documents: Optional[List[IdentityDocument]] = None
    care_type: Optional[CareType] = None
    purpose: Optional[str] = Field(None, max_length=200)
    agreement_date: Optional[dt.date] = None
    service_label: Optional[str] = Field(None, max_length=200)
    duration_days: Optional[int] = Field(None, ge=0)
    one_to_one: Optional[bool] = None
    location_id: Optional[str] = None
    lat: Optional[float] = Field(None, ge=-90, le=90)
    lng: Optional[float] = Field(None, ge=-180, le=180)
    billing_rate: Optional[int] = Field(None, ge=0)
    notes: Optional[str] = Field(None, max_length=2000)
    is_active: Optional[bool] = None

    @field_validator("agreement_date", mode="before")
    @classmethod
    def clean_agreement_date(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()
            return v if v else None
        return v

    @field_validator("purpose", mode="before")
    @classmethod
    def clean_purpose(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()
            return v if v else None
        return v


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
    documents: List[IdentityDocument] = Field(default_factory=list)
    care_type: CareType
    purpose: Optional[str] = None
    agreement_date: Optional[dt.date] = None
    service_label: Optional[str] = None
    duration_days: Optional[int] = None
    one_to_one: bool
    location_id: str
    lat: Optional[float] = None
    lng: Optional[float] = None
    billing_rate: Optional[int] = None
    notes: Optional[str] = None
    is_active: bool
