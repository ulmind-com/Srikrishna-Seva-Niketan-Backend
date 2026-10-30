"""Branch / office location endpoints (multi-location staff management)."""

from __future__ import annotations

import secrets
import string
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, Query

from app.api.helpers import item_response, paginated_response
from app.core.exceptions import NotFoundException
from app.core.pagination import PaginationParams, pagination_params
from app.core.responses import success_response
from app.dependencies.auth import ActorContext, require_permission
from app.models.location import Location
from app.schemas.location import LocationCreate, LocationResponse, LocationUpdate
from app.services.crud import CrudService

router = APIRouter(prefix="/locations", tags=["Locations"])

_locations = CrudService(
    Location, entity="location", search_fields=("name", "code", "city", "address"),
)

_CODE_ALPHABET = string.ascii_uppercase + string.digits


async def _unique_code(seed: str = "") -> str:
    """Generate a unique branch code like ``SSN-KOL-4F2A``."""
    base = "".join(c for c in seed.upper() if c.isalnum())[:3] or "BR"
    for _ in range(10):
        suffix = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(4))
        code = f"SSN-{base}-{suffix}"
        if await Location.find_one({"code": code}) is None:
            return code
    return f"SSN-{secrets.token_hex(4).upper()}"


def _scope(actor: ActorContext) -> dict:
    """Restrict a location query to the actor's own branches (by ``_id``)."""
    if not actor.is_scoped:
        return {}
    ids = [ObjectId(x) for x in actor.location_ids if ObjectId.is_valid(x)]
    return {"_id": {"$in": ids}}


@router.get("", summary="List locations")
async def list_locations(
    params: PaginationParams = Depends(pagination_params),
    is_active: Optional[bool] = Query(None),
    actor: ActorContext = Depends(require_permission("locations", "view")),
) -> dict:
    filters: dict = _scope(actor)
    if is_active is not None:
        filters["is_active"] = is_active
    items, total = await _locations.paginate(
        page=params.page, page_size=params.page_size, search=params.search,
        sort_by=params.sort_by or "order", sort_order=params.sort_direction,
        filters=filters or None,
    )
    return paginated_response(LocationResponse, items, total, params)


@router.get("/{location_id}", summary="Get location")
async def get_location(
    location_id: str,
    actor: ActorContext = Depends(require_permission("locations", "view")),
) -> dict:
    doc = await _locations.get_or_404(location_id)
    if not actor.can_access_location(str(doc.id)):
        raise NotFoundException("Location not found")
    return item_response(LocationResponse, doc)


def _invalidate_caches():
    try:
        from app.api.v1.staff_portal.router import _BRANCHES_CACHE, _BRANCH_MAP_CACHE
        import app.api.v1.staff_portal.router as sp
        sp._BRANCHES_CACHE = (0.0, [])
        sp._BRANCH_MAP_CACHE.clear()
    except Exception:
        pass


@router.post("", status_code=201, summary="Create location")
async def create_location(
    payload: LocationCreate,
    actor: ActorContext = Depends(require_permission("locations", "create")),
) -> dict:
    data = payload.model_dump(exclude_unset=True)
    if not data.get("code"):
        data["code"] = await _unique_code(data.get("city") or data.get("name", ""))
    doc = await _locations.create(data, actor)
    _invalidate_caches()
    return item_response(LocationResponse, doc, "Location created")


@router.put("/{location_id}", summary="Update location")
async def update_location(
    location_id: str,
    payload: LocationUpdate,
    actor: ActorContext = Depends(require_permission("locations", "update")),
) -> dict:
    existing = await _locations.get_or_404(location_id)
    if not actor.can_access_location(str(existing.id)):
        raise NotFoundException("Location not found")
    doc = await _locations.update(
        location_id, payload.model_dump(exclude_unset=True), actor
    )
    _invalidate_caches()
    return item_response(LocationResponse, doc, "Location updated")


@router.delete("/{location_id}", summary="Delete location")
async def delete_location(
    location_id: str,
    actor: ActorContext = Depends(require_permission("locations", "delete")),
) -> dict:
    await _locations.delete(location_id, actor)
    _invalidate_caches()
    return success_response(message="Location deleted")
