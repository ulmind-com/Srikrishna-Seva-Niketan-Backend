"""Shared helpers that apply multi-location scoping to operational routers.

Every operational record (employee, customer, assignment, expense, payment)
carries a ``location_id``. Branch-scoped managers may only read/create/mutate
records in their assigned branches; super-admins (and unscoped head-office
users) are unrestricted. These helpers centralise that rule so each router
stays thin and consistent.
"""

from __future__ import annotations

from typing import Optional

from app.core.exceptions import BadRequestException, ForbiddenException, NotFoundException
from app.dependencies.auth import ActorContext


def resolve_create_location(actor: ActorContext, location_id: Optional[str]) -> str:
    """Validate and normalise the branch for a record being created.

    - Scoped managers: must target one of their branches; when omitted it
      defaults to their first branch.
    - Unscoped users (super-admin / head-office): must supply a branch.
    """
    if actor.is_scoped:
        if location_id:
            if not actor.can_access_location(location_id):
                raise ForbiddenException("You cannot create records in that branch")
            return location_id
        if actor.location_ids:
            return actor.location_ids[0]
        raise ForbiddenException("No branch is assigned to your account")
    if not location_id:
        raise BadRequestException("location_id is required")
    return location_id


def ensure_can_access(actor: ActorContext, location_id: Optional[str]) -> None:
    """Raise 404 if the actor may not touch a record in ``location_id``.

    A 404 (rather than 403) is used so out-of-scope managers cannot even
    confirm that a record exists in another branch.
    """
    if not actor.can_access_location(location_id):
        raise NotFoundException("Record not found")
