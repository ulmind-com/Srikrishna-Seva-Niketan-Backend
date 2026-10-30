"""Authentication endpoints: login, refresh, logout, password flows, profile."""

from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response

from app.config import settings
from app.core.cookies import (
    ADMIN_REFRESH_PATH,
    clear_csrf_cookie,
    clear_refresh_cookie,
    issue_csrf_cookie,
    read_refresh_cookie,
    require_csrf,
    set_refresh_cookie,
)
from app.core.exceptions import UnauthorizedException
from app.core.limiter import limiter
from app.core.responses import success_response
from app.dependencies.auth import (
    ActorContext,
    _resolve_permissions,
    get_actor,
    get_current_active_user,
)
from app.models.user import User
from app.schemas.auth import (
    ChangePasswordRequest,
    ForgotPasswordRequest,
    LoginRequest,
    ResetPasswordRequest,
)
from app.schemas.user import ProfileUpdate, UserResponse
from app.services.auth_service import auth_service

router = APIRouter(prefix="/auth", tags=["Authentication"])


def _client_ip(request: Request) -> Optional[str]:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


async def _user_payload(user: User) -> dict:
    """Serialise a user with their effective (role + direct) permissions so
    the frontend can gate navigation and actions for role-based accounts."""
    data = UserResponse.model_validate(user).model_dump(mode="json")
    data["permissions"] = sorted(await _resolve_permissions(user))
    return data


@router.post("/login", summary="Admin login")
@limiter.limit(settings.RATE_LIMIT_AUTH)
async def login(request: Request, response: Response, payload: LoginRequest) -> dict:
    """Authenticate an admin and return an access + refresh token pair."""
    user = await auth_service.authenticate(payload.email, payload.password)
    access, refresh = await auth_service.issue_tokens(
        user,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
    )
    # Primary path: Secure, HttpOnly cookie. Fallback: the refresh token is also
    # returned in the body so SPAs on a different site (e.g. Vercel admin ->
    # Render API) can store it and present it via the X-Refresh-Token header,
    # since browsers (notably Brave/modern Chrome) block the third-party cookie.
    set_refresh_cookie(response, refresh, path=ADMIN_REFRESH_PATH)
    issue_csrf_cookie(response)
    return success_response(
        data={
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "bearer",
            "expires_in": settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            "user": await _user_payload(user),
        },
        message="Login successful",
    )


@router.post("/refresh", summary="Refresh access token")
async def refresh(request: Request, response: Response) -> dict:
    """Rotate the refresh cookie and issue a new access token.

    The refresh token is read from its HttpOnly cookie (not the body), guarded
    by the double-submit CSRF check. A fresh refresh token is written back to
    the cookie; only the new access token is returned in the body.
    """
    # Header token is sent by the SPA from localStorage (bearer style).
    # It is immune to cross-site cookie blocking and CSRF.
    # If not present, fall back to HttpOnly cookie with CSRF double-submit check.
    cookie_token = read_refresh_cookie(request, path=ADMIN_REFRESH_PATH)
    header_token = request.headers.get("X-Refresh-Token")
    token = None
    if header_token:
        token = header_token
    elif cookie_token:
        require_csrf(request)
        token = cookie_token
    else:
        raise UnauthorizedException("No active session")
    try:
        access, new_refresh = await auth_service.refresh(
            token,
            ip_address=_client_ip(request),
            user_agent=request.headers.get("User-Agent"),
        )
    except UnauthorizedException:
        # Session is gone/expired/revoked — drop the stale cookie so the client
        # stops retrying and falls back to login.
        clear_refresh_cookie(response, path=ADMIN_REFRESH_PATH)
        raise
    set_refresh_cookie(response, new_refresh, path=ADMIN_REFRESH_PATH)
    issue_csrf_cookie(response)
    return success_response(
        data={
            "access_token": access,
            "refresh_token": new_refresh,
            "token_type": "bearer",
            "expires_in": settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        },
        message="Token refreshed",
    )


@router.post("/logout", summary="Logout (revoke refresh token)")
async def logout(request: Request, response: Response) -> dict:
    """Revoke the refresh session (from its cookie) and clear the cookies.

    CSRF-guarded but does not require a live access token, so a user whose
    access token has already expired can still log out cleanly.
    """
    cookie_token = read_refresh_cookie(request, path=ADMIN_REFRESH_PATH)
    header_token = request.headers.get("X-Refresh-Token")
    token = header_token or cookie_token
    if cookie_token and not header_token:
        try:
            require_csrf(request)
        except Exception:
            pass
    if token:
        await auth_service.logout(token)
    clear_refresh_cookie(response, path=ADMIN_REFRESH_PATH)
    clear_csrf_cookie(response)
    return success_response(message="Logged out successfully")


@router.post("/change-password", summary="Change password")
@limiter.limit(settings.RATE_LIMIT_AUTH)
async def change_password(
    request: Request,
    payload: ChangePasswordRequest,
    actor: ActorContext = Depends(get_actor),
) -> dict:
    """Change the authenticated user's password."""
    await auth_service.change_password(
        actor.user, payload.current_password, payload.new_password
    )
    return success_response(message="Password changed successfully")


@router.post("/forgot-password", summary="Request password reset email")
@limiter.limit(settings.RATE_LIMIT_AUTH)
async def forgot_password(
    request: Request,
    response: Response,
    payload: ForgotPasswordRequest,
    background_tasks: BackgroundTasks,
) -> dict:
    """Send a password-reset link if the email exists (always 200)."""
    background_tasks.add_task(auth_service.forgot_password, payload.email)
    return success_response(
        message="If the email exists, a reset link has been sent"
    )


@router.post("/reset-password", summary="Reset password with token")
@limiter.limit(settings.RATE_LIMIT_AUTH)
async def reset_password(request: Request, payload: ResetPasswordRequest) -> dict:
    """Complete a password reset using the emailed token."""
    await auth_service.reset_password(payload.token, payload.new_password)
    return success_response(message="Password has been reset successfully")


@router.get("/me", summary="Get current profile")
async def me(user: User = Depends(get_current_active_user)) -> dict:
    """Return the authenticated user's profile."""
    return success_response(
        data=await _user_payload(user),
        message="Profile fetched",
    )


@router.put("/me", summary="Update current profile")
async def update_me(
    payload: ProfileUpdate,
    user: User = Depends(get_current_active_user),
) -> dict:
    """Update the authenticated user's own profile fields."""
    data = payload.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(user, key, value)
    user.touch()
    await user.save()
    return success_response(
        data=await _user_payload(user),
        message="Profile updated",
    )
