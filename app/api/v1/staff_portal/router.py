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

import httpx

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile

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
from app.services.notification_service import notification_service
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
from app.models.assignment import Assignment
from app.models.customer import Customer
from app.models.email_otp import EmailOtp
from app.models.employee import Employee
from app.models.enums import (
    AcceptanceStatus,
    EmployeeStatus,
    NotificationType,
    StaffVerificationStatus,
)
from app.models.location import Location
from app.schemas.assignment import StaffAssignmentResponse
from app.schemas.staff_portal import (
    DeclineRequest,
    LocationPing,
    VerifyIdRequest,
    SendOtpRequest,
    StaffGoogleAuthRequest,
    StaffLoginRequest,
    StaffOnboardingRequest,
    StaffProfileResponse,
    StaffRegisterRequest,
    VerifyOtpRequest,
)
from app.services.cloudinary_service import cloudinary_service
from app.services.email_service import EmailService
from app.services.geo_service import geo_service
from app.services.id_vision_service import id_vision_service, match_id
from app.utils.files import MAX_IMAGE_BYTES, read_validated_image

router = APIRouter(prefix="/staff-portal", tags=["Staff Portal"])
email_service = EmailService()

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


@router.post("/send-otp", summary="Send email verification code")
@limiter.limit("5/minute")
async def send_otp(
    request: Request,
    response: Response,
    payload: SendOtpRequest,
) -> dict:
    """Generate and email a 6-digit verification code using Resend."""
    email = payload.email.lower().strip()

    # Check if a non-draft staff account already exists
    existing = await Employee.find_one({"email": email})
    if existing and existing.verification_status != StaffVerificationStatus.DRAFT:
        raise ConflictException("An active staff account with this email already exists")

    # Generate 6-digit OTP
    otp = f"{secrets.randbelow(900000) + 100000:06d}"
    expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10)

    # Invalidate unverified prior OTPs for this email
    await EmailOtp.find({"email": email, "is_verified": False}).delete()

    record = EmailOtp(
        email=email,
        otp=otp,
        purpose="staff_registration",
        is_verified=False,
        expires_at=expires_at,
    )
    await record.insert()

    # Send email via Resend / SMTP
    await email_service.send_staff_otp(to=email, otp=otp)

    return success_response(
        data={"email": email},
        message="Verification code sent to your email address",
    )


@router.post("/verify-otp", summary="Verify email OTP code")
@limiter.limit("10/minute")
async def verify_otp(
    request: Request,
    response: Response,
    payload: VerifyOtpRequest,
) -> dict:
    """Validate the 6-digit code."""
    email = payload.email.lower().strip()
    otp = payload.otp.strip()

    record = await EmailOtp.find_one(
        {
            "email": email,
            "otp": otp,
            "is_verified": False,
            "expires_at": {"$gt": dt.datetime.now(dt.timezone.utc)},
        }
    )

    if not record:
        raise BadRequestException("Invalid or expired verification code")

    record.is_verified = True
    record.verified_at = dt.datetime.now(dt.timezone.utc)
    record.touch()
    await record.save()

    return success_response(
        data={"email": email, "verified": True},
        message="Email verified successfully",
    )


@router.post("/register", status_code=201, summary="Staff self-registration")
@limiter.limit(settings.RATE_LIMIT_AUTH)
async def register(
    request: Request,
    response: Response,
    payload: StaffRegisterRequest,
) -> dict:
    """Create a staff account in ``DRAFT`` and sign them straight in."""
    email = payload.email.lower().strip()

    # Verify that email OTP was verified
    if settings.RESEND_API_KEY:
        verified_record = await EmailOtp.find_one(
            {
                "email": email,
                "is_verified": True,
                "verified_at": {
                    "$gt": dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=30)
                },
            }
        )
        if not verified_record:
            if payload.otp:
                otp_record = await EmailOtp.find_one(
                    {
                        "email": email,
                        "otp": payload.otp.strip(),
                        "expires_at": {"$gt": dt.datetime.now(dt.timezone.utc)},
                    }
                )
                if not otp_record:
                    raise BadRequestException("Invalid or expired verification code")
                otp_record.is_verified = True
                otp_record.verified_at = dt.datetime.now(dt.timezone.utc)
                otp_record.touch()
                await otp_record.save()
            else:
                raise BadRequestException("Please verify your email with the OTP code first")

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

    # The browser OCR audit (document_verification) is ADVISORY only: it often
    # misreads genuine government IDs (phone photos of Aadhaar, colored cards,
    # holograms), so it must not hard-block onboarding. The result is stored on
    # the record and surfaced to the super-admin, who reviews the uploaded
    # document and makes the final verify/reject call.

    staff.verification_status = StaffVerificationStatus.PENDING
    staff.submitted_at = dt.datetime.now(dt.timezone.utc)
    staff.rejection_reason = None
    staff.touch()
    await staff.save()

    return success_response(
        data=_profile(staff),
        message="Submitted — a super admin will review your profile shortly",
    )


# ---- Notifications (the staff member's own live feed) ----

def _notif(n) -> dict:
    return {
        "id": str(n.id),
        "title": n.title,
        "message": n.message,
        "type": str(n.type),
        "is_read": n.is_read,
        "reference_id": n.reference_id,
        "created_at": n.created_at.isoformat() if n.created_at else None,
    }


@router.get("/me/notifications", summary="My notifications")
async def my_notifications(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    is_read: Optional[bool] = Query(None),
    staff: Employee = Depends(get_current_staff),
) -> dict:
    items, total = await notification_service.paginate_for_employee(
        str(staff.id), page=page, page_size=page_size, is_read=is_read
    )
    return success_response(
        data={"items": [_notif(n) for n in items], "total": total}
    )


@router.get("/me/notifications/unread-count", summary="My unread count")
async def my_unread_count(staff: Employee = Depends(get_current_staff)) -> dict:
    unread = await notification_service.unread_count_for_employee(str(staff.id))
    return success_response(data={"unread": unread})


@router.post("/me/notifications/{notification_id}/read", summary="Mark one read")
async def mark_my_notification_read(
    notification_id: str,
    staff: Employee = Depends(get_current_staff),
) -> dict:
    n = await notification_service.mark_read_for_employee(
        notification_id, str(staff.id)
    )
    return success_response(data=_notif(n))


@router.post("/me/notifications/read-all", summary="Mark all read")
async def mark_all_my_notifications_read(
    staff: Employee = Depends(get_current_staff),
) -> dict:
    updated = await notification_service.mark_all_read_for_employee(str(staff.id))
    return success_response(data={"updated": updated})


# ---- Duties (the nurse's own assignments: respond + route) ----

_PLACE_LABEL = {
    "home": "Customer's Home",
    "hospital": "Hospital",
    "nursing_home": "Nursing Home",
    "other": "Other",
}


def _place_text(a: Assignment) -> str:
    label = _PLACE_LABEL.get(str(a.place_type), str(a.place_type))
    return f"{label} — {a.place_name}" if a.place_name else label


async def _own_assignment(assignment_id: str, staff: Employee) -> Assignment:
    """Load an assignment, 404 unless it belongs to this nurse."""
    a = await Assignment.get(assignment_id)
    if a is None or a.employee_id != str(staff.id):
        raise NotFoundException("Assignment not found")
    return a


async def _staff_item(a: Assignment, phone: Optional[str]) -> dict:
    item = StaffAssignmentResponse.model_validate(a).model_dump(mode="json")
    item["customer_phone"] = phone
    return item


@router.get("/me/assignments", summary="My duty assignments (price-free)")
async def my_assignments(
    pending: Optional[bool] = Query(None),
    staff: Employee = Depends(get_current_staff),
) -> dict:
    filters: dict = {"employee_id": str(staff.id)}
    if pending:
        filters["acceptance_status"] = AcceptanceStatus.PENDING.value
    rows = await Assignment.find(filters).sort("-start_date").to_list()
    phones: dict[str, Optional[str]] = {}
    for cid in {a.customer_id for a in rows}:
        cust = await Customer.get(cid)
        phones[cid] = cust.phone if cust else None
    items = [await _staff_item(a, phones.get(a.customer_id)) for a in rows]
    return success_response(data={"items": items, "total": len(items)})


async def _customer_phone(a: Assignment) -> Optional[str]:
    cust = await Customer.get(a.customer_id)
    return cust.phone if cust else None


@router.post("/me/assignments/{assignment_id}/accept", summary="Accept a duty")
async def accept_assignment(
    assignment_id: str,
    staff: Employee = Depends(get_current_staff),
) -> dict:
    a = await _own_assignment(assignment_id, staff)
    a.acceptance_status = AcceptanceStatus.ACCEPTED
    a.accepted_at = dt.datetime.now(dt.timezone.utc)
    a.declined_at = None
    a.decline_reason = None
    a.touch()
    await a.save()
    await notification_service.create(
        title="Duty accepted",
        message=f"{a.employee_name} accepted {a.customer_name} ({_place_text(a)}).",
        type=NotificationType.ASSIGNMENT,
        link="/assignments",
        reference_id=str(a.id),
    )
    return success_response(
        data=await _staff_item(a, await _customer_phone(a)), message="Duty accepted"
    )


@router.post("/me/assignments/{assignment_id}/decline", summary="Decline a duty")
async def decline_assignment(
    assignment_id: str,
    payload: DeclineRequest,
    staff: Employee = Depends(get_current_staff),
) -> dict:
    a = await _own_assignment(assignment_id, staff)
    a.acceptance_status = AcceptanceStatus.DECLINED
    a.declined_at = dt.datetime.now(dt.timezone.utc)
    a.decline_reason = payload.reason
    a.accepted_at = None
    a.touch()
    await a.save()
    # Free the nurse so the office can reassign; the assignment is kept.
    if staff.status != EmployeeStatus.AVAILABLE:
        staff.status = EmployeeStatus.AVAILABLE
        staff.touch()
        await staff.save()
    reason = f" Reason: {payload.reason}." if payload.reason else ""
    await notification_service.create(
        title="Duty declined — reassign",
        message=f"{a.employee_name} declined {a.customer_name} ({_place_text(a)}).{reason}",
        type=NotificationType.ASSIGNMENT,
        link="/assignments",
        reference_id=str(a.id),
    )
    return success_response(
        data=await _staff_item(a, await _customer_phone(a)), message="Duty declined"
    )


@router.get("/me/assignments/{assignment_id}/route", summary="Route to the duty location")
async def assignment_route(
    assignment_id: str,
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    preference: str = Query("shortest", regex="^(shortest|fastest)$"),
    staff: Employee = Depends(get_current_staff),
) -> dict:
    a = await _own_assignment(assignment_id, staff)
    dest_lat, dest_lng = a.dest_lat, a.dest_lng
    if dest_lat is None or dest_lng is None:
        coords = await geo_service.geocode(a.address or "")
        if coords is not None:
            dest_lat, dest_lng = coords
    route = None
    if dest_lat is not None and dest_lng is not None:
        route = await geo_service.directions(
            (lat, lng), (dest_lat, dest_lng), route_preference=preference
        )
    return success_response(
        data={
            "distance_m": route["distance_m"] if route else None,
            "duration_s": route["duration_s"] if route else None,
            "readable_distance": route.get("readable_distance") if route else None,
            "readable_duration": route.get("readable_duration") if route else None,
            "geometry": route["geometry"] if route else None,
            "dest_lat": dest_lat,
            "dest_lng": dest_lng,
            "preference": preference,
        }
    )


@router.post("/me/location", summary="Record my current GPS")
async def ping_location(
    payload: LocationPing,
    staff: Employee = Depends(get_current_staff),
) -> dict:
    staff.last_lat = payload.lat
    staff.last_lng = payload.lng
    staff.located_at = dt.datetime.now(dt.timezone.utc)
    staff.touch()
    await staff.save()
    return success_response(data={"ok": True})


# ---- Government-ID AI verification (server-side vision) ----

async def _fetch_image(url: str) -> tuple[bytes, str]:
    """Download the uploaded ID image; empty bytes on any failure."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            mime = (resp.headers.get("content-type") or "image/jpeg").split(";")[0]
            return resp.content, mime
    except Exception:  # noqa: BLE001 — a fetch failure is just an unreadable doc
        return b"", "image/jpeg"


@router.post("/me/verify-id", summary="AI-verify an uploaded government ID")
async def verify_id(
    payload: VerifyIdRequest,
    staff: Employee = Depends(get_current_staff),
) -> dict:
    """Read the uploaded ID with the vision model and match name + DOB + number.

    Advisory-free hard check lives in the client gate; here we just return the
    structured match result. The key never leaves the server.
    """
    img, mime = await _fetch_image(payload.image_url)
    extracted = await id_vision_service.extract(img, mime)
    if extracted is None:
        extracted = {"readable": False, "is_government_id": False}
    result = match_id(
        extracted,
        name=staff.name,
        id_number=payload.id_number,
        dob=payload.dob or "",
        id_type=payload.id_type,
    )
    return success_response(data=result)
