"""Tests for branch / location management (create, update, delete, seeding resilience)."""

import pytest
from httpx import AsyncClient

from app.models.location import Location
from app.models.employee import Employee
from app.models.customer import Customer
from app.database.seed import seed_locations


@pytest.mark.asyncio
async def test_location_crud_and_seed_resilience(
    client: AsyncClient,
    auth_headers: dict,
):
    # 1. Create a new branch
    create_payload = {
        "name": "Burdwan Branch",
        "city": "Burdwan",
        "address": "Grand Trunk Rd, Burdwan",
        "phone": "+91 9876543210",
        "order": 10,
        "is_active": True,
    }
    res = await client.post("/api/v1/locations", json=create_payload, headers=auth_headers)
    assert res.status_code == 201, res.text
    created = res.json()["data"]
    loc_id = created["id"]
    assert created["name"] == "Burdwan Branch"
    assert created["code"].startswith("SSN-")

    # 2. Get the branch
    res = await client.get(f"/api/v1/locations/{loc_id}", headers=auth_headers)
    assert res.status_code == 200
    assert res.json()["data"]["name"] == "Burdwan Branch"

    # 3. Update the branch
    res = await client.put(
        f"/api/v1/locations/{loc_id}",
        json={"name": "Burdwan Main Branch", "pincode": "713101"},
        headers=auth_headers,
    )
    assert res.status_code == 200
    assert res.json()["data"]["name"] == "Burdwan Main Branch"
    assert res.json()["data"]["pincode"] == "713101"

    # 4. Attach an employee and customer to this branch to test cascade on delete
    emp = await Employee(
        code="EMP-BURDWAN-1",
        name="Test Burdwan Staff",
        category="nurse",
        location_id=loc_id,
        phone="9999988888",
    ).insert()

    cust = await Customer(
        code="CUST-BURDWAN-1",
        name="Test Burdwan Customer",
        location_id=loc_id,
        phone="8888877777",
    ).insert()

    # 5. Delete the branch
    res = await client.delete(f"/api/v1/locations/{loc_id}", headers=auth_headers)
    assert res.status_code == 200
    assert res.json()["success"] is True

    # 6. Verify branch is gone from GET
    res = await client.get(f"/api/v1/locations/{loc_id}", headers=auth_headers)
    assert res.status_code == 404

    # 7. Verify seed_locations() does NOT resurrect deleted branches
    await seed_locations()
    deleted_check = await Location.get(loc_id)
    assert deleted_check is None

    # 8. Clean up test emp and cust
    await emp.delete()
    await cust.delete()
