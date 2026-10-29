"""Booking public-create and admin-workflow tests."""

import pytest


@pytest.fixture
def booking_payload():
    return {
        "patient_name": "Test Patient",
        "contact_phone": "9998887776",
        "service_name": "Home Nursing",
        "preferred_date": "2026-09-01",
        "address": "42 Test Street",
    }


async def test_public_create_booking(client, booking_payload):
    resp = await client.post("/api/v1/bookings", json=booking_payload)
    assert resp.status_code == 201
    data = resp.json()["data"]
    assert data["reference"].startswith("NHB-")
    assert data["status"] == "pending"


async def test_list_bookings_requires_auth(client):
    resp = await client.get("/api/v1/bookings")
    assert resp.status_code == 401


async def test_approve_and_reject_flow(client, auth_headers, booking_payload):
    created = await client.post("/api/v1/bookings", json=booking_payload)
    booking_id = created.json()["data"]["id"]

    approved = await client.post(
        f"/api/v1/bookings/{booking_id}/approve", headers=auth_headers
    )
    assert approved.json()["data"]["status"] == "approved"

    # Approved is not terminal, cancelling is allowed.
    cancelled = await client.post(
        f"/api/v1/bookings/{booking_id}/cancel",
        headers=auth_headers,
        json={"reason": "duplicate"},
    )
    assert cancelled.json()["data"]["status"] == "cancelled"

    # Cancelled is terminal — approving again should fail.
    again = await client.post(
        f"/api/v1/bookings/{booking_id}/approve", headers=auth_headers
    )
    assert again.status_code == 400


async def test_booking_validation_error(client):
    resp = await client.post("/api/v1/bookings", json={"patient_name": "x"})
    assert resp.status_code == 422
    assert resp.json()["success"] is False
    assert isinstance(resp.json()["errors"], list)


async def test_convert_booking_to_customer(client, auth_headers, booking_payload):
    created = await client.post("/api/v1/bookings", json=booking_payload)
    booking_id = created.json()["data"]["id"]

    res = await client.post(
        f"/api/v1/bookings/{booking_id}/convert-to-customer",
        headers=auth_headers,
    )
    assert res.status_code == 200
    data = res.json()["data"]
    assert "customer" in data
    assert data["customer"]["name"] == booking_payload["patient_name"]
    assert data["customer"]["phone"] == booking_payload["contact_phone"]
    assert data["customer"]["code"].startswith("CUST-")
    assert data["booking"]["customer_id"] == data["customer"]["id"]
    assert data["booking"]["status"] == "approved"


async def test_booking_city_resolves_location(client, auth_headers):
    payload = {
        "patient_name": "Kolkata Patient",
        "contact_phone": "9998887771",
        "service_name": "Physiotherapy",
        "preferred_date": "2026-09-02",
        "city": "Kolkata",
        "address": "Park Street, Kolkata",
    }
    created = await client.post("/api/v1/bookings", json=payload)
    assert created.status_code == 201
    booking_data = created.json()["data"]
    assert booking_data["location_id"] is not None

    list_res = await client.get(
        f"/api/v1/bookings?location_id={booking_data['location_id']}",
        headers=auth_headers,
    )
    assert list_res.status_code == 200
    assert any(b["id"] == booking_data["id"] for b in list_res.json()["data"]["items"])
