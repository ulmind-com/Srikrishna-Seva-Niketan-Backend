"""Authentication & authorization dependencies.

``get_current_user`` decodes the bearer access token and loads the user.
``require_permission`` builds a dependency enforcing a ``resource:action``
permission (resolving the user's role permissions + direct grants).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.exceptions import ForbiddenException, UnauthorizedException
from app.core.permissions import ALL
from app.core.security import ACCESS_TOKEN_TYPE, decode_token
from app.models.rbac import Role
from app.models.user import User
from app.repositories.base import BaseRepository

_bearer = HTTPBearer(auto_error=False, description="JWT access token")

_users: BaseRepository[User] = BaseRepository(User)
_roles: BaseRepository[Role] = BaseRepository(Role)


@dataclass
class ActorContext:
    """Bundle carrying the acting user plus request metadata for auditing."""

    user: User
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None

    @property
    def user_id(self) -> str:
        return str(self.user.id)

    @property
    def email(self) -> str:
        return self.user.email

    # ---- Multi-location scoping -------------------------------------------

    @property
    def is_superuser(self) -> bool:
        """Super-admins are unscoped and see every branch."""
        return bool(self.user.is_superuser)

    @property
    def location_ids(self) -> List[str]:
        """Branch ids this user (a manager) is assigned to."""
        return [str(x) for x in (self.user.location_ids or [])]

    @property
    def is_scoped(self) -> bool:
        """A user is branch-scoped iff they are not a super-admin AND have at
        least one assigned branch. Super-admins and non-superusers with no
        assigned branch (head-office admins) are unscoped and see everything.
        """
        return not self.is_superuser and len(self.location_ids) > 0

    def scope_filter(self, field: str = "location_id") -> Dict[str, object]:
        """Mongo filter restricting a query to the actor's branches.

        Unscoped actors receive an empty filter (everything); branch-scoped
        managers get an ``$in`` over their assigned location ids.
        """
        if not self.is_scoped:
            return {}
        return {field: {"$in": self.location_ids}}

    def can_access_location(self, location_id: Optional[str]) -> bool:
        """Whether the actor may read/mutate a record in ``location_id``."""
        if not self.is_scoped:
            return True
        return location_id is not None and str(location_id) in self.location_ids


def _client_ip(request: Request) -> Optional[str]:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> User:
    """Resolve the authenticated user from the bearer access token."""
    if credentials is None or not credentials.credentials:
        raise UnauthorizedException("Authentication required")

    payload = decode_token(credentials.credentials, expected_type=ACCESS_TOKEN_TYPE)
    user = await _users.get(payload.get("sub", ""))
    if user is None:
        raise UnauthorizedException("User no longer exists")
    return user


async def get_current_active_user(
    user: User = Depends(get_current_user),
) -> User:
    """Ensure the authenticated user is active."""
    if not user.is_active:
        raise ForbiddenException("Your account is disabled")
    return user


async def get_actor(
    request: Request,
    user: User = Depends(get_current_active_user),
) -> ActorContext:
    """Provide an :class:`ActorContext` for auditing side-effectful routes."""
    return ActorContext(
        user=user,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
    )


async def _resolve_permissions(user: User) -> set[str]:
    """Return the effective permission set for a user."""
    if user.is_superuser:
        return {ALL}
    permissions: set[str] = set(user.extra_permissions)
    role = await _roles.find_one({"slug": user.role})
    if role:
        permissions.update(role.permissions)
    return permissions


def require_permission(resource: str, action: str):
    """Build a dependency enforcing the ``resource:action`` permission."""
    required = f"{resource}:{action}"

    async def _dependency(
        request: Request,
        user: User = Depends(get_current_active_user),
    ) -> ActorContext:
        permissions = await _resolve_permissions(user)
        allowed = (
            ALL in permissions
            or required in permissions
            or f"{resource}:{ALL}" in permissions
        )
        if not allowed:
            raise ForbiddenException(
                f"Missing required permission: {required}"
            )
        return ActorContext(
            user=user,
            ip_address=_client_ip(request),
            user_agent=request.headers.get("User-Agent"),
        )

    return _dependency


async def require_superuser(
    request: Request,
    user: User = Depends(get_current_active_user),
) -> ActorContext:
    """Dependency allowing only superusers."""
    if not user.is_superuser:
        raise ForbiddenException("Superuser privileges required")
    return ActorContext(
        user=user,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
    )
