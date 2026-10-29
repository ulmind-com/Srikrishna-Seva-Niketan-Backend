"""Unit tests for Web Push notifications."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest_asyncio

from app.core.security import create_access_token
from app.dependencies.staff_auth import STAFF_SCOPE
from app.models.employee import Employee
from app.models.enums import StaffVerificationStatus
from app.models.location import Location
from app.models.push_subscription import PushSubscription
from app.services.webpush_service import webpush_service


async def _register_staff(email: str = "nurse.push@test.com"):
    loc = await Location.find_one({})
    loc_id = str(loc.id) if loc else "loc-1"
    emp = Employee(
        code="EMP-PUSH-1",
        name="Nurse Push",
        email=email,
        location_id=loc_id,
        verification_status=StaffVerificationStatus.VERIFIED,
    )
    await emp.insert()
    token, _, _ = create_access_token(str(emp.id), {"scope": STAFF_SCOPE, "email": email})
    return token, str(emp.id)


@pytest_asyncio.fixture
def mock_webpush():
    """Mock the synchronous pywebpush.webpush call to prevent real network calls."""
    with patch("app.services.webpush_service.webpush") as mock:
        yield mock


async def test_vapid_public_key_endpoint(client):
    """VAPID public key must be exposed so browsers can subscribe."""
    res = await client.get("/api/v1/push/vapid-public-key")
    assert res.status_code == 200
    data = res.json()["data"]
    assert "public_key" in data
    assert len(data["public_key"]) > 20


async def test_register_and_unregister_push_subscription(client, db):
    """Staff can register and unregister device push subscription."""
    token, emp_id = await _register_staff()
    headers = {"Authorization": f"Bearer {token}"}

    payload = {
        "endpoint": "https://fcm.googleapis.com/fcm/send/test-token-123",
        "keys": {
            "p256dh": "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QT9t0448G3DghH",
            "auth": "tBHItJI5svbpez7KI4CCXg",
        },
        "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
    }

    # 1. Register subscription
    res = await client.post("/api/v1/staff-portal/me/push-subscription", json=payload, headers=headers)
    assert res.status_code == 200
    assert res.json()["data"]["is_active"] is True

    sub = await PushSubscription.find_one(PushSubscription.endpoint == payload["endpoint"])
    assert sub is not None
    assert sub.employee_id == emp_id
    assert sub.is_active is True

    # 2. Unregister subscription
    unres = await client.request(
        "DELETE",
        "/api/v1/staff-portal/me/push-subscription",
        json={"endpoint": payload["endpoint"]},
        headers=headers,
    )
    assert unres.status_code == 200
    assert unres.json()["data"]["unregistered"] is True

    sub_after = await PushSubscription.find_one(PushSubscription.endpoint == payload["endpoint"])
    assert sub_after.is_active is False


async def test_nurse_push_test_alert(client, db, mock_webpush):
    """Nurse can trigger a test push alert to verify their device works."""
    token, emp_id = await _register_staff("nurse.alert@test.com")
    headers = {"Authorization": f"Bearer {token}"}

    # Register subscription first
    await webpush_service.register_subscription(
        endpoint="https://push.example.com/send/nurse-1",
        p256dh="key123",
        auth="auth123",
        employee_id=emp_id,
    )

    res = await client.post("/api/v1/staff-portal/me/push-test", headers=headers)
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["sent"] == 1
    assert mock_webpush.called


async def test_admin_push_dispatch_all_and_employee(client, auth_headers, db, mock_webpush):
    """Admin can dispatch duty push alerts to all nurses or a specific nurse."""
    token, emp_id = await _register_staff("nurse.duty@test.com")

    # Register subscription for the nurse
    await webpush_service.register_subscription(
        endpoint="https://push.example.com/send/nurse-duty-1",
        p256dh="key-duty",
        auth="auth-duty",
        employee_id=emp_id,
    )

    # 1. Check push status
    status_res = await client.get("/api/v1/notifications/push-status", headers=auth_headers)
    assert status_res.status_code == 200
    assert status_res.json()["data"]["active_staff_devices"] >= 1

    # 2. Dispatch to specific nurse
    emp_res = await client.post(
        "/api/v1/notifications/push-dispatch",
        json={
            "target": "employee",
            "employee_id": emp_id,
            "title": "🚨 Urgent Patient Call",
            "message": "Patient requires immediate attention in Salt Lake Sector 1.",
            "urgency": "high",
        },
        headers=auth_headers,
    )
    assert emp_res.status_code == 200
    assert emp_res.json()["data"]["sent"] == 1

    # 3. Dispatch to all nurses
    all_res = await client.post(
        "/api/v1/notifications/push-dispatch",
        json={
            "target": "all_nurses",
            "title": "📢 Shift Roster Published",
            "message": "Next week's rosters are now available in your portal.",
            "urgency": "normal",
        },
        headers=auth_headers,
    )
    assert all_res.status_code == 200
    assert all_res.json()["data"]["sent"] >= 1
