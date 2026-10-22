"""Self-service staff portal: registration, login and onboarding.

Flow
----
1. ``POST /staff-portal/register`` — the staff member creates an account
   (name, email, password, branch). The record lands in ``DRAFT``.
2. ``PUT  /staff-portal/me`` — they fill the onboarding profile (address,
   experience, phone, past work, photo, drawn signature). Saveable repeatedly.
3. ``POST /staff-portal/me/submit`` — they submit; status becomes ``PENDING``.
4. A super-admin verifies via ``POST /employees/{id}/verify`` (see the
   employees router), which flips the record to ``VERIFIED`` and unlocks the
   ID card and application forms.

Staff refresh tokens are stateless (decoded and re-issued, not persisted);
revocation on the portal is a client-side token clear. Admin sessions, which
need auditing and server-side revocation, keep using the admin auth service.

.. warning::
   Do **not** add ``from __future__ import annotations`` to this module. The
   rate-limited routes below are wrapped by slowapi's ``@limiter.limit``; with
   PEP 563 string annotations FastAPI resolves the wrapper's annotations
   outside this module's namespace, fails to recognise the Pydantic body
   models and silently demotes them to query parameters — every POST then
   rejects its body with ``query.payload: Field required``.
"""

import datetime as dt
import secrets
import string
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile

from app.api.helpers import item_response
from app.config import settings
from app.core.exceptions import (
    BadRequestException,
    ConflictException,
    NotFoundException,
    UnauthorizedException,
)
from app.core.limiter import limiter
from app.core.responses import success_response
from app.core.security import (
    ACCESS_TOKEN_TYPE,
    REFRESH_TOKEN_TYPE,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.dependencies.staff_auth import STAFF_SCOPE, get_current_staff
from app.models.employee import Employee
from app.models.enums import StaffVerificationStatus
from app.models.location import Location
from app.schemas.staff_portal import (
    StaffGoogleAuthRequest,
    StaffLoginRequest,
    StaffOnboardingRequest,
    StaffProfileResponse,
    StaffRegisterRequest,
)
from app.services.cloudinary_service import cloudinary_service
from app.utils.files import MAX_IMAGE_BYTES, read_validated_image

router = APIRouter(prefix="/staff-portal", tags=["Staff Portal"])

_ALPHABET = string.ascii_uppercase + string.digits

#: Fields that must be filled before a profile can be submitted for review.
REQUIRED_FOR_SUBMIT = (
    "phone",
    "address",
    "experience",
    "work_history",
    "id_proof_type",
    "id_proof_number",
)


async def _unique_code() -> str:
    """Generate a unique staff code like ``EMP-4F2A9C``."""
    for _ in range(10):
        code = "EMP-" + "".join(secrets.choice(_ALPHABET) for _ in range(6))
        if await Employee.find_one({"code": code}) is None:
            return code
    return "EMP-" + secrets.token_hex(4).upper()


def _issue_tokens(staff: Employee) -> dict:
    """Mint a staff-scoped access + refresh token pair."""
    staff_id = str(staff.id)
    claims = {"scope": STAFF_SCOPE, "email": staff.email}
    access, _, _ = create_access_token(staff_id, claims)
    refresh, _, _ = create_refresh_token(staff_id, {"scope": STAFF_SCOPE})
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "bearer",
        "expires_in": settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    }


def _profile(staff: Employee) -> dict:
    return StaffProfileResponse.model_validate(staff).model_dump(mode="json")


async def _assert_branch_exists(location_id: str) -> Location:
    location = await Location.get(location_id)
    if location is None or not location.is_active:
        raise BadRequestException("Select a valid branch")
    return location


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


@router.get("/branches", summary="List branches for the signup form")
async def list_branches() -> dict:
    """Public, unauthenticated branch list used by the registration dropdown.

    Only the fields the signup form needs are exposed — not the full branch
    record, which stays behind the admin ``locations`` permission.
    """
    branches = await Location.find({"is_active": True}).sort("order").to_list()
    return success_response(
        data=[
            {
                "id": str(b.id),
                "name": b.name,
                "code": b.code,
                "city": b.city,
            }
            for b in branches
        ]
    )


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


@router.post("/register", status_code=201, summary="Staff self-registration")
@limiter.limit(settings.RATE_LIMIT_AUTH)
async def register(
    request: Request,
    response: Response,
    payload: StaffRegisterRequest,
) -> dict:
    """Create a staff account in ``DRAFT`` and sign them straight in."""
    email = payload.email.lower()
    existing = await Employee.find_one({"email": email})
    if existing is not None:
        # If the account is still in DRAFT and the password matches, seamlessly resume
        # onboarding instead of dead-ending the user with a conflict error.
        if (
            existing.verification_status == StaffVerificationStatus.DRAFT
            and existing.hashed_password
            and verify_password(payload.password, existing.hashed_password)
        ):
            existing.last_login_at = dt.datetime.now(dt.timezone.utc)
            if payload.location_id and existing.location_id != payload.location_id:
                await _assert_branch_exists(payload.location_id)
                existing.location_id = payload.location_id
            if payload.name.strip():
                existing.name = payload.name.strip()
            existing.touch()
            await existing.save()
            return success_response(
                data={**_issue_tokens(existing), "staff": _profile(existing)},
                message="Account resumed — complete your profile next",
            )
        raise ConflictException("An account with this email already exists")

    await _assert_branch_exists(payload.location_id)

    staff = Employee(
        code=await _unique_code(),
        name=payload.name.strip(),
        email=email,
        hashed_password=hash_password(payload.password),
        auth_provider="password",
        location_id=payload.location_id,
        verification_status=StaffVerificationStatus.DRAFT,
        last_login_at=dt.datetime.now(dt.timezone.utc),
    )
    await staff.insert()

    return success_response(
        data={**_issue_tokens(staff), "staff": _profile(staff)},
        message="Account created — complete your profile next",
    )


@router.post("/login", summary="Staff login")
@limiter.limit(settings.RATE_LIMIT_AUTH)
async def login(
    request: Request,
    response: Response,
    payload: StaffLoginRequest,
) -> dict:
    """Authenticate a staff member with email + password."""
    staff = await Employee.find_one({"email": payload.email.lower()})
    # Same generic message whether the email is unknown or the password is
    # wrong, so the endpoint cannot be used to enumerate registered staff.
    if staff is None or not staff.hashed_password:
        raise UnauthorizedException("Incorrect email or password")
    if not verify_password(payload.password, staff.hashed_password):
        raise UnauthorizedException("Incorrect email or password")
    if not staff.is_active:
        raise UnauthorizedException("Your account has been deactivated")

    staff.last_login_at = dt.datetime.now(dt.timezone.utc)
    staff.touch()
    await staff.save()

    return success_response(
        data={**_issue_tokens(staff), "staff": _profile(staff)},
        message="Login successful",
    )


@router.post("/refresh", summary="Refresh the staff access token")
async def refresh(payload: dict) -> dict:
    """Exchange a staff refresh token for a fresh token pair."""
    token = (payload or {}).get("refresh_token")
    if not token:
        raise BadRequestException("refresh_token is required")

    claims = decode_token(token, expected_type=REFRESH_TOKEN_TYPE)
    if claims.get("scope") != STAFF_SCOPE:
        raise UnauthorizedException("Invalid token scope")

    staff = await Employee.get(claims.get("sub"))
    if staff is None or not staff.is_active:
        raise UnauthorizedException("Account is no longer active")

    return success_response(data=_issue_tokens(staff))


@router.post("/google", summary="Sign in with Google (not configured yet)")
async def google_auth(payload: StaffGoogleAuthRequest) -> dict:
    """Google sign-in hook.

    The full flow is implemented client-side and lands here with a Google ID
    token. Verification needs a Google OAuth **Client ID**; until one is set in
    ``settings.GOOGLE_CLIENT_ID`` this endpoint reports that it is unavailable
    rather than trusting an unverified token.

    To finish the wiring: set ``GOOGLE_CLIENT_ID``, verify ``payload.id_token``
    with ``google.oauth2.id_token.verify_oauth2_token``, then look up or create
    the Employee by the token's ``email`` / ``sub`` and return ``_issue_tokens``.
    """
    client_id = getattr(settings, "GOOGLE_CLIENT_ID", None)
    if not client_id:
        raise BadRequestException(
            "Google sign-in is not configured yet — please use email and password"
        )
    raise BadRequestException("Google sign-in is not available yet")


# ---------------------------------------------------------------------------
# Profile & onboarding
# ---------------------------------------------------------------------------


@router.get("/me", summary="Get my staff profile")
async def me(staff: Employee = Depends(get_current_staff)) -> dict:
    return success_response(data=_profile(staff))


@router.put("/me", summary="Save my onboarding profile")
async def update_me(
    payload: StaffOnboardingRequest,
    staff: Employee = Depends(get_current_staff),
) -> dict:
    """Save the onboarding form.

    Editing is blocked once the profile is verified — a verified record backs
    a printed ID card, so changes must go through an admin.
    """
    if staff.verification_status == StaffVerificationStatus.VERIFIED:
        raise BadRequestException(
            "Your profile is verified — contact your branch manager to change it"
        )

    data = payload.model_dump(exclude_unset=True, exclude_none=True)
    for field, value in data.items():
        setattr(staff, field, value)

    # Re-submitting after a rejection clears the previous reason.
    if staff.verification_status == StaffVerificationStatus.REJECTED:
        staff.verification_status = StaffVerificationStatus.DRAFT
        staff.rejection_reason = None

    staff.touch()
    await staff.save()
    return success_response(data=_profile(staff), message="Profile saved")


@router.post("/me/upload", summary="Upload my photo / signature / ID proof")
async def upload_my_asset(
    file: UploadFile = File(...),
    kind: str = Form("photo", description="photo | signature | id_proof"),
    staff: Employee = Depends(get_current_staff),
) -> dict:
    """Staff-scoped image upload.

    The admin ``/uploads`` endpoints require the ``media:create`` permission,
    which staff deliberately do not have — so onboarding gets its own narrow
    endpoint. Files land in a per-staff Cloudinary folder and the resulting
    asset is written straight onto the staff member's own record, so this
    cannot be used to touch anybody else's data.
    """
    if kind not in {"photo", "signature", "id_proof"}:
        raise BadRequestException("kind must be photo, signature or id_proof")
    if staff.verification_status == StaffVerificationStatus.VERIFIED:
        raise BadRequestException("Your profile is verified and can no longer be edited")

    contents = await read_validated_image(file, MAX_IMAGE_BYTES)
    asset = await cloudinary_service.upload_image(
        contents, folder=f"ssn/staff/{staff.code}/{kind}"
    )

    setattr(staff, kind, asset)
    staff.touch()
    await staff.save()

    return success_response(data=asset.model_dump(), message=f"{kind} uploaded")


@router.post("/me/submit", summary="Submit my profile for verification")
async def submit_me(staff: Employee = Depends(get_current_staff)) -> dict:
    """Hand the completed profile to the super-admin review queue."""
    if staff.verification_status == StaffVerificationStatus.VERIFIED:
        raise BadRequestException("Your profile is already verified")
    if staff.verification_status == StaffVerificationStatus.PENDING:
        raise BadRequestException("Your profile is already awaiting review")

    missing = [f for f in REQUIRED_FOR_SUBMIT if not getattr(staff, f, None)]
    if not staff.photo:
        missing.append("photo")
    if not staff.signature:
        missing.append("signature")
    if not staff.id_proof:
        missing.append("government ID document")
    if missing:
        raise BadRequestException(
            "Please complete these first: " + ", ".join(m.replace("_", " ") for m in missing)
        )

    # Server-side guard: reject submissions where document was flagged as fraud or name mismatch
    if staff.document_verification:
        doc_ver = staff.document_verification
        if doc_ver.get("fraudRisk") == "high":
            flags = doc_ver.get("fraudFlags") or []
            reason = flags[0] if flags else "Failed anti-fraud security scan"
            raise BadRequestException(
                f"Government ID verification failed: {reason}. Please upload an authentic, unblurred original document."
            )
        if not doc_ver.get("nameMatched", True):
            raise BadRequestException(
                f"Government ID verification failed: The name on the uploaded document does not match your registered name ('{staff.name}')."
            )
        if not doc_ver.get("idNumberMatched", True):
            raise BadRequestException(
                "Government ID verification failed: The ID number on the document does not match the entered number."
            )

    staff.verification_status = StaffVerificationStatus.PENDING
    staff.submitted_at = dt.datetime.now(dt.timezone.utc)
    staff.rejection_reason = None
    staff.touch()
    await staff.save()

    return success_response(
        data=_profile(staff),
        message="Submitted — a super admin will review your profile shortly",
    )
