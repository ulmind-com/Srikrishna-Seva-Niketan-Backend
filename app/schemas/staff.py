"""Schemas for staff members."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

from app.models.base import ImageAsset
from app.schemas.common import IdTimestampSchema


# ---- Staff Member ----


class StaffCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)
    photo: Optional[ImageAsset] = None
    category: str = Field(..., min_length=2, max_length=60)
    rating: float = Field(5.0, ge=1.0, le=5.0)
    service_label: str = Field("", max_length=200)
    price_7_days: Optional[int] = Field(None, ge=0)
    price_15_days: Optional[int] = Field(None, ge=0)
    price_30_days: Optional[int] = Field(None, ge=0)
    experience: Optional[str] = Field(None, max_length=100)
    bio: Optional[str] = Field(None, max_length=2000)
    is_featured: bool = False
    is_active: bool = True
    order: int = 0

    @field_validator("price_7_days", "price_15_days", "price_30_days", mode="before")
    @classmethod
    def clean_price(cls, v: Any) -> Any:
        if v is None or v == "":
            return None
        if isinstance(v, (int, float)):
            import math
            if math.isnan(v):
                return None
            return int(v)
        if isinstance(v, str):
            v = v.strip()
            return int(v) if v else None
        return v

    @field_validator("rating", mode="before")
    @classmethod
    def clean_rating(cls, v: Any) -> Any:
        if v is None or v == "":
            return 5.0
        return float(v)

    @field_validator("order", mode="before")
    @classmethod
    def clean_order(cls, v: Any) -> Any:
        if v is None or v == "":
            return 0
        return int(v)

    @field_validator("experience", "bio", mode="before")
    @classmethod
    def clean_strings(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()
            return v if v else None
        return v


class StaffUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=120)
    photo: Optional[ImageAsset] = None
    category: Optional[str] = Field(None, min_length=2, max_length=60)
    rating: Optional[float] = Field(None, ge=1.0, le=5.0)
    service_label: Optional[str] = Field(None, max_length=200)
    price_7_days: Optional[int] = Field(None, ge=0)
    price_15_days: Optional[int] = Field(None, ge=0)
    price_30_days: Optional[int] = Field(None, ge=0)
    experience: Optional[str] = Field(None, max_length=100)
    bio: Optional[str] = Field(None, max_length=2000)
    is_featured: Optional[bool] = None
    is_active: Optional[bool] = None
    order: Optional[int] = None

    @field_validator("price_7_days", "price_15_days", "price_30_days", mode="before")
    @classmethod
    def clean_price(cls, v: Any) -> Any:
        if v is None or v == "":
            return None
        if isinstance(v, (int, float)):
            import math
            if math.isnan(v):
                return None
            return int(v)
        if isinstance(v, str):
            v = v.strip()
            return int(v) if v else None
        return v

    @field_validator("rating", mode="before")
    @classmethod
    def clean_rating(cls, v: Any) -> Any:
        if v is None or v == "":
            return None
        return float(v)

    @field_validator("order", mode="before")
    @classmethod
    def clean_order(cls, v: Any) -> Any:
        if v is None or v == "":
            return None
        return int(v)

    @field_validator("experience", "bio", mode="before")
    @classmethod
    def clean_strings(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()
            return v if v else None
        return v


class StaffResponse(IdTimestampSchema):
    name: str
    photo: Optional[ImageAsset] = None
    category: str
    rating: float
    service_label: str
    price_7_days: Optional[int] = None
    price_15_days: Optional[int] = None
    price_30_days: Optional[int] = None
    experience: Optional[str] = None
    bio: Optional[str] = None
    is_featured: bool
    is_active: bool
    order: int
