import pytest
from httpx import AsyncClient

from app.models.customer import Customer
from app.models.employee import Employee
from app.models.enums import EmployeeStatus
from app.models.location import Location


@pytest.mark.asyncio
async def test_assignment_purpose_persists_across_home_other_and_replacement(
    client: AsyncClient, auth_headers: dict
):
    loc = Location(name="Purpose Branch", code="PPB01", city="Kolkata", is_active=True)
    await loc.insert()
    loc_id = str(loc.id)

    cust = Customer(
        name="Care Client",
        code="CUST-P01",
        phone="9876543200",
        location_id=loc_id,
        billing_rate=1000,
    )
    await cust.insert()

    emp1 = Employee(
        name="Nurse A",
        code="NA-01",
        phone="9000010001",
        location_id=loc_id,
        is_active=True,
        status=EmployeeStatus.AVAILABLE,
    )
    emp2 = Employee(
        name="Nurse B",
        code="NB-02",
        phone="9000010002",
        location_id=loc_id,
        is_active=True,
        status=EmployeeStatus.AVAILABLE,
    )
    await emp1.insert()
    await emp2.insert()

    # 1. Home posting with purpose
    res = await client.post(
        "/api/v1/assignments",
        json={
            "customer_id": str(cust.id),
            "employee_id": str(emp1.id),
            "place_type": "home",
            "purpose": "Post-surgery bedside care",
            "start_date": "2026-11-26",
            "end_date": "2026-11-30",
        },
        headers=auth_headers,
    )
    assert res.status_code == 201, res.text
    a1 = res.json()["data"]
    assert a1["place_type"] == "home"
    assert a1["purpose"] == "Post-surgery bedside care"

    # 2. Edit to "other" with custom place_name and updated purpose
    upd = await client.put(
        f"/api/v1/assignments/{a1['id']}",
        json={
            "place_type": "other",
            "place_name": "Recovery Rehab Center",
            "purpose": "Physiotherapy & mobility support",
        },
        headers=auth_headers,
    )
    assert upd.status_code == 200, upd.text
    a1_upd = upd.json()["data"]
    assert a1_upd["place_type"] == "other"
    assert a1_upd["place_name"] == "Recovery Rehab Center"
    assert a1_upd["purpose"] == "Physiotherapy & mobility support"

    # 3. Replacement preserves place_name & purpose
    rep = await client.post(
        f"/api/v1/assignments/{a1['id']}/replace-staff",
        json={
            "new_employee_id": str(emp2.id),
            "effective_date": "2026-11-28",
            "reason": "Schedule handover",
        },
        headers=auth_headers,
    )
    assert rep.status_code == 200, rep.text
    a2 = rep.json()["data"]["new_assignment"]
    assert a2["place_type"] == "other"
    assert a2["place_name"] == "Recovery Rehab Center"
    assert a2["purpose"] == "Physiotherapy & mobility support"
