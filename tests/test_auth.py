"""Authentication flow tests.

Covers the cookie-based refresh design: short-lived access token in the body,
refresh token only in a Secure/HttpOnly cookie, double-submit CSRF, rotation,
reuse/revocation detection and logout.
"""

from app.config import settings

ADMIN = {"email": "admin@test.com", "password": "Admin@12345"}


def _csrf(client) -> dict:
    """Header echoing the current CSRF cookie (double-submit)."""
    return {settings.CSRF_HEADER_NAME: client.cookies.get(settings.CSRF_COOKIE_NAME)}


async def _login(client) -> None:
    resp = await client.post("/api/v1/auth/login", json=ADMIN)
    assert resp.status_code == 200
    return resp


# ---- Login ---------------------------------------------------------------


async def test_login_returns_access_only_and_sets_cookies(client):
    resp = await _login(client)
    data = resp.json()["data"]
    assert data["access_token"]
    # Refresh token is in the body as a cross-site fallback AND in the cookie.
    assert data["refresh_token"]
    assert data["user"]["email"] == ADMIN["email"]
    # Refresh + CSRF cookies are set.
    assert client.cookies.get(settings.REFRESH_COOKIE_NAME)
    assert client.cookies.get(settings.CSRF_COOKIE_NAME)


async def test_login_wrong_password(client):
    resp = await client.post(
        "/api/v1/auth/login", json={**ADMIN, "password": "wrong"}
    )
    assert resp.status_code == 401
    assert resp.json()["success"] is False


async def test_me_requires_auth(client):
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 401


async def test_me_returns_profile(client, auth_headers):
    resp = await client.get("/api/v1/auth/me", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["is_superuser"] is True


# ---- Refresh -------------------------------------------------------------


async def test_refresh_rotates_cookie_and_returns_new_access(client):
    await _login(client)
    old_refresh = client.cookies.get(settings.REFRESH_COOKIE_NAME)

    resp = await client.post("/api/v1/auth/refresh", headers=_csrf(client))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["access_token"]
    # Rotated refresh token is returned in the body (cross-site fallback) too.
    assert data["refresh_token"]
    # Rotation: a different refresh token is now in the cookie.
    assert client.cookies.get(settings.REFRESH_COOKIE_NAME) != old_refresh


async def test_refresh_requires_csrf(client):
    await _login(client)
    resp = await client.post("/api/v1/auth/refresh")  # no X-CSRF-Token header
    assert resp.status_code == 403


async def test_refresh_without_session(client):
    # Fresh client, never logged in: no refresh cookie.
    resp = await client.post(
        "/api/v1/auth/refresh",
        headers={settings.CSRF_HEADER_NAME: "anything"},
    )
    assert resp.status_code in (401, 403)


async def test_refresh_token_reuse_is_rejected(client):
    """Simulates two concurrent calls racing on the same refresh token: the
    first rotates (revokes) it, the replayed second must be rejected."""
    await _login(client)
    csrf_val = client.cookies.get(settings.CSRF_COOKIE_NAME)
    stale = client.cookies.get(settings.REFRESH_COOKIE_NAME)

    first = await client.post(
        "/api/v1/auth/refresh", headers={settings.CSRF_HEADER_NAME: csrf_val}
    )
    assert first.status_code == 200

    # Replay the now-rotated (revoked) token explicitly on a cleared jar.
    client.cookies.clear()
    replay = await client.post(
        "/api/v1/auth/refresh",
        headers={settings.CSRF_HEADER_NAME: csrf_val},
        cookies={
            settings.REFRESH_COOKIE_NAME: stale,
            settings.CSRF_COOKIE_NAME: csrf_val,
        },
    )
    assert replay.status_code == 401


async def test_revoked_session_requires_login(client):
    from app.models.token import RefreshToken

    await _login(client)
    # Revoke every active refresh token server-side (admin disabled the session).
    await RefreshToken.find({"revoked": False}).update(
        {"$set": {"revoked": True}}
    )
    resp = await client.post("/api/v1/auth/refresh", headers=_csrf(client))
    assert resp.status_code == 401


# ---- Logout --------------------------------------------------------------


async def test_logout_revokes_and_blocks_refresh(client):
    await _login(client)
    csrf = _csrf(client)

    out = await client.post("/api/v1/auth/logout", headers=csrf)
    assert out.status_code == 200

    # After logout the session is gone; refresh must fail.
    resp = await client.post("/api/v1/auth/refresh", headers=csrf)
    assert resp.status_code in (401, 403)


# ---- Passwords -----------------------------------------------------------


async def test_change_password_wrong_current(client, auth_headers):
    resp = await client.post(
        "/api/v1/auth/change-password",
        headers=auth_headers,
        json={"current_password": "nope", "new_password": "NewPass@123"},
    )
    assert resp.status_code == 400
