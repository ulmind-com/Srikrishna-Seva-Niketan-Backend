"""Schemas for the self-service staff portal.

Staff members register themselves, complete an onboarding profile (address,
experience, work history, photo and a drawn signature) and submit it for
super-admin verification. Once verified their ID card and application forms
become available.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.models.base import ImageAsset
from app.models.enums import EmployeeStatus, Gender, StaffVerificationStatus
from app.schemas.common import IdTimestampSchema


def _validate_password(v: str) -> str:
    """Shared password-strength rule (mirrors the admin auth schemas)."""
    if not re.search(r"[A-Z]", v):
        raise ValueError("Password must contain at least one uppercase letter")
    if not re.search(r"[a-z]", v):
        raise ValueError("Password must contain at least one lowercase letter")
    if not re.search(r"\d", v):
        raise ValueError("Password must contain at least one number")
    if not re.search(r"[\W_]", v):
        raise ValueError("Password must contain at least one special character")
    return v


class SendOtpRequest(BaseModel):
    """Request a 6-digit email verification OTP."""

    email: EmailStr


class VerifyOtpRequest(BaseModel):
    """Verify the received 6-digit email OTP."""

    email: EmailStr
    otp: str = Field(..., min_length=4, max_length=8)


class StaffRegisterRequest(BaseModel):
    """Step 1 — create the staff account."""

    name: str = Field(..., min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    location_id: str = Field(..., description="Branch the staff member belongs to")
    otp: Optional[str] = Field(None, description="6-digit verification code")

    _check_password = field_validator("password")(_validate_password)


class StaffLoginRequest(BaseModel):
    """Staff portal credentials."""

    email: EmailStr
    password: str = Field(..., min_length=1)


class StaffGoogleAuthRequest(BaseModel):
    """Sign in with a Google ID token (wired once a Client ID is configured)."""

    id_token: str = Field(..., min_length=10)
    location_id: Optional[str] = Field(
        None, description="Branch to join — required on first sign-in only"
    )


class StaffOnboardingRequest(BaseModel):
    """Step 2 — the onboarding profile the staff member fills in.

    Every field is optional so the form can be saved as a draft; the
    completeness check happens on submit.
    """

    phone: Optional[str] = Field(None, min_length=4, max_length=20)
    alt_phone: Optional[str] = Field(None, max_length=20)
    address: Optional[str] = Field(None, max_length=300)
    city: Optional[str] = Field(None, max_length=80)
    pincode: Optional[str] = Field(None, max_length=12)
    dob: Optional[dt.date] = None
    gender: Optional[Gender] = None
    category: Optional[str] = Field(None, max_length=60)
    designation: Optional[str] = Field(None, max_length=80)
    experience: Optional[str] = Field(None, max_length=100)
    work_history: Optional[str] = Field(None, max_length=2000)
    previous_employer: Optional[str] = Field(None, max_length=160)
    photo: Optional[ImageAsset] = None
    signature: Optional[ImageAsset] = None
    id_proof: Optional[ImageAsset] = None
    id_proof_type: Optional[str] = Field(None, max_length=40)
    id_proof_number: Optional[str] = Field(None, max_length=40)
    document_verification: Optional[dict] = None


class StaffVerifyRequest(BaseModel):
    """Super-admin approval — may assign the printed ID-card details."""

    designation: Optional[str] = Field(None, max_length=80)
    hrms_code: Optional[str] = Field(None, max_length=40)
    join_date: Optional[dt.date] = None
    daily_rate: Optional[int] = Field(None, ge=0)


class StaffRejectRequest(BaseModel):
    """Super-admin rejection with a reason shown back to the staff member."""

    reason: str = Field(..., min_length=3, max_length=500)


class StaffProfileResponse(IdTimestampSchema):
    """The staff member's own view of their record."""

    code: str
    name: str
    email: Optional[EmailStr] = None
    phone: str
    alt_phone: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    pincode: Optional[str] = None
    dob: Optional[dt.date] = None
    gender: Optional[Gender] = None
    category: str
    designation: Optional[str] = None
    hrms_code: Optional[str] = None
    experience: Optional[str] = None
    work_history: Optional[str] = None
    previous_employer: Optional[str] = None
    photo: Optional[ImageAsset] = None
    signature: Optional[ImageAsset] = None
    id_proof: Optional[ImageAsset] = None
    id_proof_type: Optional[str] = None
    id_proof_number: Optional[str] = None
    location_id: str
    status: EmployeeStatus
    verification_status: StaffVerificationStatus
    submitted_at: Optional[dt.datetime] = None
    verified_at: Optional[dt.datetime] = None
    rejection_reason: Optional[str] = None
    join_date: Optional[dt.date] = None
    document_verification: Optional[dict] = None
    is_active: bool


class LocationStaffStat(BaseModel):
    """Per-branch staff counts for the super-admin overview."""

    location_id: str
    location_name: str
    location_code: str
    city: Optional[str] = None
    total: int = 0
    verified: int = 0
    pending: int = 0
    rejected: int = 0
    draft: int = 0
    active: int = 0
    working: int = 0
    available: int = 0
    revenue: float = 0.0


class DeclineRequest(BaseModel):
    """Optional reason a nurse gives when declining a posting."""

    reason: Optional[str] = Field(None, max_length=500)


class LocationPing(BaseModel):
    """The nurse's current GPS, captured when opening a duty route."""

    lat: float = Field(..., ge=-90, le=90)
    lng: float = Field(..., ge=-180, le=180)


class VerifyIdRequest(BaseModel):
    """Ask the backend to read + verify an uploaded government-ID image."""

    image_url: str
    id_type: str = Field(..., max_length=40)
    id_number: str = Field(..., max_length=40)
    dob: Optional[str] = Field(None, max_length=20)
