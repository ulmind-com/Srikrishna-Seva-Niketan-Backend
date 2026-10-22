"""Schemas for branch / office locations."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from app.schemas.common import IdTimestampSchema


class LocationCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)
    code: Optional[str] = Field(None, max_length=30)  # auto-generated when blank
    address: Optional[str] = Field(None, max_length=300)
    city: Optional[str] = Field(None, max_length=80)
    pincode: Optional[str] = Field(None, max_length=12)
    phone: Optional[str] = Field(None, max_length=20)
    email: Optional[str] = Field(None, max_length=120)
    is_active: bool = True
    order: int = 0


class LocationUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=120)
    code: Optional[str] = Field(None, max_length=30)
    address: Optional[str] = Field(None, max_length=300)
    city: Optional[str] = Field(None, max_length=80)
    pincode: Optional[str] = Field(None, max_length=12)
    phone: Optional[str] = Field(None, max_length=20)
    email: Optional[str] = Field(None, max_length=120)
    is_active: Optional[bool] = None
    order: Optional[int] = None


class LocationResponse(IdTimestampSchema):
    name: str
    code: str
    address: Optional[str] = None
    city: Optional[str] = None
    pincode: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    is_active: bool
    order: int
