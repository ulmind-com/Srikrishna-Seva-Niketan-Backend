"""End-to-end integration test for live nurse-to-customer route tracking workflow."""

import pytest
from app.models.assignment import Assignment
from app.models.customer import Customer
from app.models.employee import Employee
from app.models.enums import AcceptanceStatus, AssignmentStatus, EmployeeStatus
from app.models.location import Location


@pytest.fixture
async def full_setup(db):
    loc = Location(name="Kolkata North", code="CCU-N", city="Kolkata", is_active=True)
    await loc.insert()
    loc_id = str(loc.id)

    emp = Employee(
        name="Nurse Riya Das",
        code="EMP-1001",
        email="riya.nurse@example.com",
        phone="9876543210",
        location_id=loc_id,
        is_active=True,
        status=EmployeeStatus.AVAILABLE,
    )
    await emp.insert()

    cust = Customer(
        name="Mr. Amit Mukherjee",
        code="CUST-2001",
        phone="9123456789",
        location_id=loc_id,
        address="Salt Lake Sector 5, Kolkata",
        city="Kolkata",
        pincode="700091",
        lat=22.5800,
        lng=88.4200,
        is_active=True,
    )
    await cust.insert()

    return {
        "location": loc,
        "employee": emp,
        "customer": cust,
    }


async def test_full_nurse_route_workflow(client, auth_headers, full_setup, monkeypatch):
    from app.dependencies.staff_auth import STAFF_SCOPE
    from app.core.security import create_access_token
    from app.services.geo_service import geo_service

    emp = full_setup["employee"]
    cust = full_setup["customer"]

    # 1. Admin creates assignment with confirmed customer destination pin
    create_payload = {
        "customer_id": str(cust.id),
        "employee_id": str(emp.id),
        "place_type": "home",
        "address": cust.address,
        "dest_lat": cust.lat,
        "dest_lng": cust.lng,
        "shift": "day",
        "start_date": "2026-11-01",
        "duration_days": 7,
        "status": "active",
    }
    res_assign = await client.post("/api/v1/assignments", headers=auth_headers, json=create_payload)
    assert res_assign.status_code == 201, res_assign.text
    assignment_data = res_assign.json()["data"]
    assignment_id = assignment_data["id"]
    assert assignment_data["dest_lat"] == 22.5800
    assert assignment_data["dest_lng"] == 88.4200
    assert assignment_data["acceptance_status"] == "pending"

    # 2. Staff nurse auth token
    staff_token, _, _ = create_access_token(str(emp.id), {"scope": STAFF_SCOPE, "email": emp.email})
    staff_headers = {"Authorization": f"Bearer {staff_token}"}

    # 3. Nurse accepts assignment
    res_accept = await client.post(
        f"/api/v1/staff-portal/me/assignments/{assignment_id}/accept",
        headers=staff_headers,
    )
    assert res_accept.status_code == 200
    assert res_accept.json()["data"]["acceptance_status"] == "accepted"

    # 4. Nurse live GPS ping during active duty
    nurse_lat, nurse_lng = 22.5726, 88.3639
    res_ping = await client.post(
        "/api/v1/staff-portal/me/location",
        headers=staff_headers,
        json={"lat": nurse_lat, "lng": nurse_lng},
    )
    assert res_ping.status_code == 200
    updated_emp = await Employee.get(emp.id)
    assert updated_emp.last_lat == nurse_lat
    assert updated_emp.last_lng == nurse_lng
    assert updated_emp.located_at is not None

    # 5. Nurse requests road-following route (mocked Ola response with real road polyline)
    async def _mock_directions(origin, dest, route_preference="shortest", mode="driving"):
        return {
            "distance_m": 7907,
            "duration_s": 2083,
            "readable_distance": "7.9 km",
            "readable_duration": "34 mins",
            "geometry": [[88.36402, 22.57285], [88.36386, 22.57293], [88.42003, 22.57998]],
            "route_preference": route_preference,
        }

    monkeypatch.setattr(geo_service, "directions", _mock_directions)

    # Shortest route
    res_route_shortest = await client.get(
        f"/api/v1/staff-portal/me/assignments/{assignment_id}/route?lat={nurse_lat}&lng={nurse_lng}&preference=shortest",
        headers=staff_headers,
    )
    assert res_route_shortest.status_code == 200
    route_shortest = res_route_shortest.json()["data"]
    assert route_shortest["distance_m"] == 7907
    assert route_shortest["duration_s"] == 2083
    assert route_shortest["readable_distance"] == "7.9 km"
    assert len(route_shortest["geometry"]) == 3
    assert route_shortest["preference"] == "shortest"

    # Fastest route
    res_route_fastest = await client.get(
        f"/api/v1/staff-portal/me/assignments/{assignment_id}/route?lat={nurse_lat}&lng={nurse_lng}&preference=fastest",
        headers=staff_headers,
    )
    assert res_route_fastest.status_code == 200
    assert res_route_fastest.json()["data"]["preference"] == "fastest"
