"""Geocoding & Maps proxy router — secure backend Ola Maps integration."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.core.exceptions import BadRequestException
from app.core.responses import success_response
from app.dependencies.auth import get_current_active_user
from app.models.user import User
from app.services.geo_service import geo_service

router = APIRouter(prefix="/geo", tags=["Geocoding & Maps"])


@router.get("/geocode", summary="Geocode address to confirmed coordinates")
async def geocode_address(
    address: str = Query(..., min_length=2, max_length=500, description="Address text to geocode"),
    user: User = Depends(get_current_active_user),
) -> dict:
    """Convert an address to precise latitude/longitude using Ola Maps Places API.

    Keeps the secret API key on the backend and returns confidence metadata
    so the admin UI can prompt the user to confirm the pin if the result is approximate.
    """
    clean_addr = address.strip()
    if not clean_addr:
        raise BadRequestException("Address cannot be empty")

    detail = await geo_service.geocode_detailed(clean_addr)
    if detail is None:
        return success_response(
            data={
                "found": False,
                "lat": None,
                "lng": None,
                "formatted_address": None,
                "location_type": None,
                "is_approximate": True,
                "results_count": 0,
            },
            message="No geocoding results found for this address. Please drop pin manually.",
        )

    return success_response(
        data={
            "found": True,
            **detail,
        },
        message="Address geocoded successfully",
    )
