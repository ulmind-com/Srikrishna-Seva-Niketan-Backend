"""Staff-portal cookie auth: stateful refresh, rotation, revoke, logout.

Staff refresh tokens were previously stateless (un-revocable). These tests lock
in the new behaviour: refresh in a Secure/HttpOnly cookie, CSRF-guarded,
persisted server-side so sessions can be rotated and revoked.
"""

from app.config import settings

CSRF_H = settings.CSRF_HEADER_NAME


async def _seed_location_id():
    from app.models.location import Location

    loc = await Location.find_one({})
    return str(loc.id)


async def _register(client) -> dict:
    """Seed an active staff account, then log in via the API so the refresh +
    CSRF cookies land in the client jar. Avoids the registration OTP gate."""
    from app.core.security import hash_password
    from app.models.employee import Employee

    email = "staff.auth@test.com"
    if await Employee.find_one({"email": email}) is None:
        await Employee(
            code="EMP-AUTH1",
            name="Test Staff",
            email=email,
            hashed_password=hash_password("StaffPass@123"),
            auth_provider="password",
            location_id=await _seed_location_id(),
        ).insert()
    resp = await client.post(
        "/api/v1/staff-portal/login",
        json={"email": email, "password": "StaffPass@123"},
    )
    assert resp.status_code == 200, resp.text
    return resp


def _csrf(client) -> dict:
    return {CSRF_H: client.cookies.get(settings.CSRF_COOKIE_NAME)}


async def test_register_sets_cookie_no_body_refresh(client):
    resp = await _register(client)
    data = resp.json()["data"]
    assert data["access_token"]
    assert "refresh_token" not in data
    assert client.cookies.get(settings.STAFF_REFRESH_COOKIE_NAME)
    assert client.cookies.get(settings.CSRF_COOKIE_NAME)


async def test_staff_refresh_rotates(client):
    await _register(client)
    old = client.cookies.get(settings.STAFF_REFRESH_COOKIE_NAME)
    resp = await client.post("/api/v1/staff-portal/refresh", headers=_csrf(client))
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["access_token"]
    assert client.cookies.get(settings.STAFF_REFRESH_COOKIE_NAME) != old


async def test_staff_refresh_requires_csrf(client):
    await _register(client)
    resp = await client.post("/api/v1/staff-portal/refresh")
    assert resp.status_code == 403


async def test_staff_refresh_reuse_rejected(client):
    await _register(client)
    csrf_val = client.cookies.get(settings.CSRF_COOKIE_NAME)
    stale = client.cookies.get(settings.STAFF_REFRESH_COOKIE_NAME)

    first = await client.post(
        "/api/v1/staff-portal/refresh", headers={CSRF_H: csrf_val}
    )
    assert first.status_code == 200

    client.cookies.clear()
    replay = await client.post(
        "/api/v1/staff-portal/refresh",
        headers={CSRF_H: csrf_val},
        cookies={
            settings.STAFF_REFRESH_COOKIE_NAME: stale,
            settings.CSRF_COOKIE_NAME: csrf_val,
        },
    )
    assert replay.status_code == 401


async def test_staff_logout_blocks_refresh(client):
    await _register(client)
    csrf = _csrf(client)
    out = await client.post("/api/v1/staff-portal/logout", headers=csrf)
    assert out.status_code == 200
    resp = await client.post("/api/v1/staff-portal/refresh", headers=csrf)
    assert resp.status_code in (401, 403)


async def test_staff_revoked_session_requires_login(client):
    from app.models.token import RefreshToken

    await _register(client)
    await RefreshToken.find({"scope": "staff", "revoked": False}).update(
        {"$set": {"revoked": True}}
    )
    resp = await client.post("/api/v1/staff-portal/refresh", headers=_csrf(client))
    assert resp.status_code == 401
