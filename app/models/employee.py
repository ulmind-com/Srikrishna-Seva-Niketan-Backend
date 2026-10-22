"""Operational staff member (nurse / aaya / attendant) — HR record.

Distinct from :class:`app.models.staff.StaffMember`, which is the public
website's marketing catalogue. This is the internal, location-scoped HR record
used for assignments, ID cards, billing and reports.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

import pymongo
from beanie import Indexed
from pydantic import EmailStr, Field

from app.models.base import ImageAsset, TimestampedDocument
from app.models.enums import EmployeeStatus, Gender, StaffVerificationStatus


class Employee(TimestampedDocument):
    """A registered field staff member belonging to a branch.

    An employee may be created two ways:

    * **By an admin** through ``POST /employees`` (the classic HR flow), or
    * **By the staff member themselves** through the staff portal
      (``/staff-portal/register``), which creates the account in
      ``verification_status=DRAFT``, collects an onboarding profile, and then
      waits for a super-admin to verify it before the ID card and application
      forms become available.
    """

    code: Indexed(str, unique=True)  # type: ignore[valid-type]  # e.g. EMP-20260728-A1B2
    name: str
    phone: str = ""  # collected during onboarding for self-registered staff
    alt_phone: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    pincode: Optional[str] = None

    category: str = "nurse"  # nurse / aaya / attendant / physio / japa / other
    gender: Optional[Gender] = None
    experience: Optional[str] = None  # e.g. "3 years"

    photo: Optional[ImageAsset] = None
    signature: Optional[ImageAsset] = None
    id_proof: Optional[ImageAsset] = None
    id_proof_type: Optional[str] = None  # Aadhaar / Voter / PAN ...
    id_proof_number: Optional[str] = None

    # Multi-location scoping.
    location_id: Indexed(str)  # type: ignore[valid-type]

    status: EmployeeStatus = EmployeeStatus.AVAILABLE
    rating: float = Field(default=5.0, ge=1.0, le=5.0)
    daily_rate: Optional[int] = None  # payout to the staff member (expense)
    join_date: Optional[dt.date] = None
    notes: Optional[str] = None
    is_active: bool = True

    # ---- Staff-portal account -------------------------------------------
    # Only present for self-registered staff. ``email`` carries a *sparse*
    # unique index so the many admin-created employees without one do not
    # collide on null.
    email: Optional[EmailStr] = None
    hashed_password: Optional[str] = None
    auth_provider: str = "password"  # "password" | "google"
    google_sub: Optional[str] = None  # Google account subject id
    last_login_at: Optional[dt.datetime] = None

    # ---- Onboarding profile ---------------------------------------------
    dob: Optional[dt.date] = None
    designation: Optional[str] = None  # printed on the ID card
    hrms_code: Optional[str] = None  # E-HRMS code printed on the ID card
    work_history: Optional[str] = None  # "ki kaj koreche" — past work summary
    previous_employer: Optional[str] = None

    # ---- Verification workflow ------------------------------------------
    verification_status: StaffVerificationStatus = StaffVerificationStatus.DRAFT
    submitted_at: Optional[dt.datetime] = None
    verified_at: Optional[dt.datetime] = None
    verified_by: Optional[str] = None  # admin user id
    rejection_reason: Optional[str] = None
    document_verification: Optional[dict] = None

    class Settings:
        name = "employees"
        indexes = [
            [("code", pymongo.ASCENDING)],
            [("location_id", pymongo.ASCENDING)],
            [("status", pymongo.ASCENDING)],
            [("category", pymongo.ASCENDING)],
            [("is_active", pymongo.ASCENDING)],
            [("verification_status", pymongo.ASCENDING)],
            [("name", pymongo.TEXT), ("phone", pymongo.TEXT)],
            pymongo.IndexModel(
                [("email", pymongo.ASCENDING)],
                name="uniq_employee_email",
                unique=True,
                sparse=True,
            ),
        ]
