"""Server-side maps via Ola Maps (Krutrim): geocoding + directions.

The Ola API key lives only here (``settings.OLA_MAPS_API_KEY``) and never
reaches any frontend. Every call is failure-safe: on any network, HTTP or
parse error it logs a warning and returns ``None`` / empty, so a maps outage
can never 500 an endpoint or blank a page.

Ola Maps mirrors the Google Maps API shapes (geocodingResults, routes with an
encoded ``overview_polyline``), so the parsers below follow that convention.
Confirm the exact endpoints against Ola's API Documentation before launch.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 6.0
_BASE = "https://api.olamaps.io"
_GEOCODE_URL = f"{_BASE}/places/v1/geocode"
_DIRECTIONS_URL = f"{_BASE}/routing/v1/directions"


def _client() -> httpx.AsyncClient:
    """Return a fresh async client. Seam that tests monkeypatch."""
    return httpx.AsyncClient(timeout=HTTP_TIMEOUT)


def _decode_polyline(encoded: str) -> List[List[float]]:
    """Decode a Google/Ola encoded polyline to ``[[lng, lat], ...]`` pairs."""
    points: List[List[float]] = []
    index = lat = lng = 0
    length = len(encoded)
    while index < length:
        for is_lng in (False, True):
            shift = result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if (result & 1) else (result >> 1)
            if is_lng:
                lng += delta
            else:
                lat += delta
        points.append([lng / 1e5, lat / 1e5])
    return points


class GeoService:
    """Thin wrapper over Ola Maps geocoding + directions."""

    @property
    def _key(self) -> str:
        return settings.OLA_MAPS_API_KEY

    async def geocode(self, address: str) -> Optional[Tuple[float, float]]:
        """Address text → ``(lat, lng)``. ``None`` on miss or failure."""
        if not address or not self._key:
            return None
        try:
            async with _client() as client:
                resp = await client.get(
                    _GEOCODE_URL, params={"address": address, "api_key": self._key}
                )
                resp.raise_for_status()
                data = resp.json()
            loc = data["geocodingResults"][0]["geometry"]["location"]
            return (float(loc["lat"]), float(loc["lng"]))
        except Exception:  # noqa: BLE001 — geo failure must never propagate
            logger.warning("Ola geocode failed", exc_info=True)
            return None

    async def directions(
        self, origin: Tuple[float, float], dest: Tuple[float, float]
    ) -> Optional[dict]:
        """Shortest route origin→dest.

        Returns ``{"distance_m": int, "duration_s": int, "geometry": [[lng,lat],...]}``
        or ``None`` on failure.
        """
        if not self._key:
            return None
        try:
            params = {
                "origin": f"{origin[0]},{origin[1]}",
                "destination": f"{dest[0]},{dest[1]}",
                "api_key": self._key,
            }
            async with _client() as client:
                resp = await client.post(_DIRECTIONS_URL, params=params)
                resp.raise_for_status()
                data = resp.json()
            route = data["routes"][0]
            leg = route["legs"][0]
            distance_m = int(_scalar(leg.get("distance")))
            duration_s = int(_scalar(leg.get("duration")))
            poly = route.get("overview_polyline")
            points = poly["points"] if isinstance(poly, dict) else poly
            geometry = _decode_polyline(points) if points else []
            return {
                "distance_m": distance_m,
                "duration_s": duration_s,
                "geometry": geometry,
            }
        except Exception:  # noqa: BLE001 — geo failure must never propagate
            logger.warning("Ola directions failed", exc_info=True)
            return None


def _scalar(value) -> float:
    """Accept either a plain number or a ``{"value": n}`` wrapper."""
    if isinstance(value, dict):
        return float(value.get("value", 0))
    return float(value or 0)


geo_service = GeoService()
