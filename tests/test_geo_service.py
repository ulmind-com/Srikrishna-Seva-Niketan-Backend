"""Ola geo service — parsing + failure safety. httpx is faked, no network."""

import pytest


class _FakeResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("err", request=None, response=None)


class _FakeClient:
    """Stands in for httpx.AsyncClient as an async context manager."""

    def __init__(self, get_resp=None, post_resp=None, raises=None):
        self._get_resp = get_resp
        self._post_resp = post_resp
        self._raises = raises

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, *a, **k):
        if self._raises:
            raise self._raises
        return self._get_resp

    async def post(self, *a, **k):
        if self._raises:
            raise self._raises
        return self._post_resp


# Google-compatible Ola response shapes (Ola Maps mirrors the Google API).
_GEOCODE_BODY = {
    "geocodingResults": [
        {"geometry": {"location": {"lat": 22.5726, "lng": 88.3639}}}
    ]
}
# Google's canonical polyline example → 3 points, first is (38.5, -120.2).
_DIRECTIONS_BODY = {
    "routes": [
        {
            "legs": [{"distance": 12345, "duration": 1680}],
            "overview_polyline": {"points": "_p~iF~ps|U_ulLnnqC_mqNvxq`@"},
        }
    ]
}


async def test_geocode_parses_latlng(monkeypatch):
    from app.services import geo_service as gs

    monkeypatch.setattr(gs, "_client", lambda: _FakeClient(get_resp=_FakeResp(_GEOCODE_BODY)))
    result = await gs.geo_service.geocode("Salt Lake, Kolkata")
    assert result == pytest.approx((22.5726, 88.3639))


async def test_geocode_none_on_error(monkeypatch):
    import httpx

    from app.services import geo_service as gs

    monkeypatch.setattr(gs, "_client", lambda: _FakeClient(raises=httpx.ConnectError("down")))
    assert await gs.geo_service.geocode("x") is None


async def test_directions_parses(monkeypatch):
    from app.services import geo_service as gs

    monkeypatch.setattr(gs, "_client", lambda: _FakeClient(post_resp=_FakeResp(_DIRECTIONS_BODY)))
    result = await gs.geo_service.directions((22.57, 88.36), (22.60, 88.40), route_preference="fastest")
    assert result is not None
    assert result["distance_m"] == 12345
    assert result["duration_s"] == 1680
    assert result["route_preference"] == "fastest"
    assert "readable_distance" in result
    assert "readable_duration" in result
    geom = result["geometry"]
    assert isinstance(geom, list) and len(geom) == 3
    # geometry is [lng, lat]; first decoded point is lat 38.5, lng -120.2
    assert geom[0] == pytest.approx([-120.2, 38.5], abs=1e-3)


async def test_geocode_detailed(monkeypatch):
    from app.services import geo_service as gs

    detailed_body = {
        "geocodingResults": [
            {
                "formatted_address": "Salt Lake Sector 5, Kolkata, India",
                "geometry": {
                    "location": {"lat": 22.5726, "lng": 88.3639},
                    "location_type": "APPROXIMATE",
                },
            }
        ]
    }
    monkeypatch.setattr(gs, "_client", lambda: _FakeClient(get_resp=_FakeResp(detailed_body)))
    detail = await gs.geo_service.geocode_detailed("Salt Lake")
    assert detail is not None
    assert detail["lat"] == pytest.approx(22.5726)
    assert detail["lng"] == pytest.approx(88.3639)
    assert detail["is_approximate"] is True
    assert detail["formatted_address"] == "Salt Lake Sector 5, Kolkata, India"


async def test_directions_none_on_error(monkeypatch):
    from app.services import geo_service as gs

    monkeypatch.setattr(gs, "_client", lambda: _FakeClient(post_resp=_FakeResp({}, status_code=500)))
    assert await gs.geo_service.directions((1.0, 1.0), (2.0, 2.0)) is None


async def test_geocode_endpoint_requires_auth(client):
    resp = await client.get("/api/v1/geo/geocode?address=Kolkata")
    assert resp.status_code == 401


async def test_geocode_endpoint_success(client, auth_headers, monkeypatch):
    from app.services import geo_service as gs

    detailed_body = {
        "geocodingResults": [
            {
                "formatted_address": "Salt Lake Sector 5, Kolkata, India",
                "geometry": {
                    "location": {"lat": 22.5726, "lng": 88.3639},
                    "location_type": "ROOFTOP",
                },
            }
        ]
    }
    monkeypatch.setattr(gs, "_client", lambda: _FakeClient(get_resp=_FakeResp(detailed_body)))
    resp = await client.get("/api/v1/geo/geocode?address=Salt+Lake", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["found"] is True
    assert data["lat"] == pytest.approx(22.5726)
    assert data["lng"] == pytest.approx(88.3639)
    assert data["is_approximate"] is False
