"""Authentication dependency for the self-service staff portal.

Staff tokens are deliberately *separate* from admin tokens: they carry a
``scope: "staff"`` claim and resolve to an :class:`~app.models.employee.Employee`
rather than a :class:`~app.models.user.User`. A staff token therefore cannot be
replayed against an admin endpoint (``get_current_user`` looks the subject up in
the ``users`` collection and would fail), and an admin token cannot be used here.
"""

from __future__ import annotations

from typing import Optional

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.exceptions import ForbiddenException, UnauthorizedException
from app.core.security import ACCESS_TOKEN_TYPE, decode_token
from app.models.employee import Employee
from app.models.enums import StaffVerificationStatus

_bearer = HTTPBearer(auto_error=False, description="Staff portal JWT access token")

#: Claim marking a token as belonging to the staff portal.
STAFF_SCOPE = "staff"


async def get_current_staff(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> Employee:
    """Resolve the staff member behind a bearer token.

    Raises:
        UnauthorizedException: missing/invalid token, wrong scope, or the
            account no longer exists.
        ForbiddenException: the account has been deactivated.
    """
    if credentials is None or not credentials.credentials:
        raise UnauthorizedException("Not authenticated")

    payload = decode_token(credentials.credentials, expected_type=ACCESS_TOKEN_TYPE)
    if payload.get("scope") != STAFF_SCOPE:
        raise UnauthorizedException("Invalid token scope")

    subject = payload.get("sub")
    if not subject:
        raise UnauthorizedException("Invalid token")

    staff = await Employee.get(subject)
    if staff is None:
        raise UnauthorizedException("Account no longer exists")
    if not staff.is_active:
        raise ForbiddenException("Your account has been deactivated")

    return staff


async def get_verified_staff(
    staff: Employee = Depends(get_current_staff),
) -> Employee:
    """Like :func:`get_current_staff` but requires an approved account.

    Used to gate the ID card and application forms, which must not be
    downloadable before a super-admin has verified the profile.
    """
    if staff.verification_status != StaffVerificationStatus.VERIFIED:
        raise ForbiddenException("Your profile has not been verified yet")
    return staff
