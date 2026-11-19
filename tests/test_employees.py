"""Tests for operational staff (employee) endpoints."""

import pytest
from app.models.location import Location


@pytest.mark.asyncio
async def test_create_employee_minimal(client, auth_headers):
    loc = await Location.find_one({})
    assert loc is not None
    payload = {
        "name": "Nurse Simple",
        "phone": "9876543201",
        "location_id": str(loc.id),
        "category": "nurse",
    }
    resp = await client.post("/api/v1/employees", json=payload, headers=auth_headers)
    assert resp.status_code == 201
    data = resp.json()["data"]
    assert data["name"] == "Nurse Simple"
    assert data["code"].startswith("EMP-")
    assert data["location_id"] == str(loc.id)
    assert data["verification_status"] == "verified"


@pytest.mark.asyncio
async def test_create_multiple_employees_without_email_no_duplicate_error(client, auth_headers):
    """Ensure multiple staff can be registered without email without unique index collision."""
    loc = await Location.find_one({})
    assert loc is not None

    for i in range(1, 4):
        payload = {
            "name": f"Nurse Batch {i}",
            "phone": f"987654321{i}",
            "location_id": str(loc.id),
            "category": "nurse",
        }
        resp = await client.post("/api/v1/employees", json=payload, headers=auth_headers)
        assert resp.status_code == 201, f"Failed on employee {i}: {resp.json()}"
        assert resp.json()["success"] is True


@pytest.mark.asyncio
async def test_create_employee_with_frontend_empty_fields(client, auth_headers):
    """Ensure empty strings from frontend form are converted and don't fail validation or server."""
    loc = await Location.find_one({})
    assert loc is not None

    fe_payload = {
        "name": "Staff Form Values",
        "phone": "9876500001",
        "alt_phone": "",
        "category": "aaya",
        "gender": "",
        "experience": "",
        "location_id": str(loc.id),
        "address": "",
        "city": "",
        "pincode": "",
        "id_proof_type": "",
        "id_proof_number": "",
        "id_proof": None,
        "photo": None,
        "signature": None,
        "status": "available",
        "daily_rate": None,
        "join_date": "",
        "rating": "5",
        "notes": "",
        "is_active": True,
        "email": "",
    }
    resp = await client.post("/api/v1/employees", json=fe_payload, headers=auth_headers)
    assert resp.status_code == 201
    data = resp.json()["data"]
    assert data["name"] == "Staff Form Values"
    assert data["gender"] is None
    assert data["join_date"] is None
    assert data["email"] is None
    assert data["rating"] == 5.0


@pytest.mark.asyncio
async def test_create_employee_duplicate_email_handled(client, auth_headers):
    loc = await Location.find_one({})
    assert loc is not None

    p1 = {
        "name": "Nurse With Email",
        "phone": "9876500002",
        "location_id": str(loc.id),
        "email": "unique.nurse@ssn.com",
    }
    r1 = await client.post("/api/v1/employees", json=p1, headers=auth_headers)
    assert r1.status_code == 201

    p2 = {
        "name": "Nurse Dup Email",
        "phone": "9876500003",
        "location_id": str(loc.id),
        "email": "unique.nurse@ssn.com",
    }
    r2 = await client.post("/api/v1/employees", json=p2, headers=auth_headers)
    assert r2.status_code == 409
    assert r2.json()["success"] is False
