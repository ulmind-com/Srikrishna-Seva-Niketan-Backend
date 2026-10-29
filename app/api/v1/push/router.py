"""Web Push API endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from app.core.responses import success_response
from app.services.webpush_service import webpush_service

router = APIRouter(prefix="/push", tags=["Push Notifications"])


@router.get("/vapid-public-key", summary="Get VAPID public key for Web Push")
async def get_vapid_public_key() -> dict:
    """Return the server's VAPID public key needed by browser PushManager."""
    return success_response(
        data={"public_key": webpush_service.public_key},
        message="VAPID public key retrieved",
    )

