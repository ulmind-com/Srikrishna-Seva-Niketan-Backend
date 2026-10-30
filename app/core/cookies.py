"""Refresh-token cookie and CSRF (double-submit) helpers.

The refresh token is never returned in a response body or read by JavaScript —
it lives only in a ``Secure``/``HttpOnly`` cookie, scoped by path to the auth
endpoints that consume it. A second, JS-readable CSRF cookie is set alongside
it; clients echo its value in the ``X-CSRF-Token`` header on state-changing,
cookie-authenticated calls (refresh/logout). Because an attacker's site cannot
read our CSRF cookie across origins, it cannot forge the matching header, which
defends the ``SameSite=None`` refresh cookie against CSRF.
"""

from __future__ import annotations

import secrets

from fastapi import Request, Response

from app.config import settings
from app.core.exceptions import ForbiddenException

# Cookie paths: the refresh cookie is only sent to the router that needs it.
ADMIN_REFRESH_PATH = f"{settings.API_V1_PREFIX}/auth"
STAFF_REFRESH_PATH = f"{settings.API_V1_PREFIX}/staff-portal"

_REFRESH_MAX_AGE = settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60


def _domain() -> str | None:
    """Cookie ``Domain`` attribute, or ``None`` for a host-only cookie."""
    return settings.COOKIE_DOMAIN or None


def set_refresh_cookie(response: Response, token: str, *, path: str) -> None:
    """Persist the refresh token in a Secure, HttpOnly, path-scoped cookie."""
    response.set_cookie(
        key=settings.REFRESH_COOKIE_NAME
        if path == ADMIN_REFRESH_PATH
        else settings.STAFF_REFRESH_COOKIE_NAME,
        value=token,
        max_age=_REFRESH_MAX_AGE,
        path=path,
        domain=_domain(),
        secure=settings.COOKIE_SECURE,
        httponly=True,
        samesite=settings.COOKIE_SAMESITE,
    )


def clear_refresh_cookie(response: Response, *, path: str) -> None:
    """Remove the refresh cookie (logout / failed rotation)."""
    response.delete_cookie(
        key=settings.REFRESH_COOKIE_NAME
        if path == ADMIN_REFRESH_PATH
        else settings.STAFF_REFRESH_COOKIE_NAME,
        path=path,
        domain=_domain(),
        secure=settings.COOKIE_SECURE,
        httponly=True,
        samesite=settings.COOKIE_SAMESITE,
    )


def read_refresh_cookie(request: Request, *, path: str) -> str | None:
    """Return the refresh token from its cookie, if present."""
    name = (
        settings.REFRESH_COOKIE_NAME
        if path == ADMIN_REFRESH_PATH
        else settings.STAFF_REFRESH_COOKIE_NAME
    )
    return request.cookies.get(name)


def issue_csrf_cookie(response: Response) -> str:
    """Set (and return) a fresh double-submit CSRF token.

    The CSRF cookie is intentionally **not** HttpOnly so the SPA can read it and
    echo it in the request header. It carries no authority by itself.
    """
    token = secrets.token_urlsafe(32)
    response.set_cookie(
        key=settings.CSRF_COOKIE_NAME,
        value=token,
        max_age=_REFRESH_MAX_AGE,
        path="/",
        domain=_domain(),
        secure=settings.COOKIE_SECURE,
        httponly=False,
        samesite=settings.COOKIE_SAMESITE,
    )
    return token


def clear_csrf_cookie(response: Response) -> None:
    """Remove the CSRF cookie on logout."""
    response.delete_cookie(
        key=settings.CSRF_COOKIE_NAME,
        path="/",
        domain=_domain(),
        secure=settings.COOKIE_SECURE,
        samesite=settings.COOKIE_SAMESITE,
    )


def require_csrf(request: Request) -> None:
    """Enforce the double-submit CSRF check for cookie-authenticated calls.

    Both the CSRF cookie and the ``X-CSRF-Token`` header must be present and
    equal. Compared with ``secrets.compare_digest`` to avoid timing leaks.
    """
    cookie_token = request.cookies.get(settings.CSRF_COOKIE_NAME)
    header_token = request.headers.get(settings.CSRF_HEADER_NAME)
    if (
        not cookie_token
        or not header_token
        or not secrets.compare_digest(cookie_token, header_token)
    ):
        raise ForbiddenException("CSRF validation failed")
