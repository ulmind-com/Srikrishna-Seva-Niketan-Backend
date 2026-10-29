"""Staff-portal assignment endpoints: list/accept/decline/route/location.

Verifies the money-free contract, ownership, decline-frees-nurse, and that a
maps outage degrades gracefully instead of 500ing.
"""

import datetime as dt

import pytest_asyncio

_MONEY_KEYS = {"customer_rate", "employee_payout", "other_expense", "notes",
               "billing_rate", "daily_rate"}


async def _seed_location_id():
    from app.models.location import Location

    loc = await Location.find_one({})
    return str(loc.id)


async def _register_staff(client, email, sfx="A"):
    """Create a verified staff Employee and mint a staff-scoped token.

    Bypasses the OTP/email flow of the register endpoint — these tests
    exercise assignment endpoints, not signup.
    """
    from app.core.security import create_access_token
    from app.dependencies.staff_auth import STAFF_SCOPE
    from app.models.employee import Employee
    from app.models.enums import StaffVerificationStatus

    emp = Employee(
        code=f"EMP-NUR{sfx}", name="Nurse A", email=email,
        location_id=await _seed_location_id(),
        verification_status=StaffVerificationStatus.VERIFIED,
    )
    await emp.insert()
    access, _, _ = create_access_token(str(emp.id), {"scope": STAFF_SCOPE, "email": email})
    return access, str(emp.id)


async def _mk_assignment(employee_id, location_id, **extra):
    from app.models.assignment import Assignment
    from app.models.customer import Customer
    from app.models.enums import AcceptanceStatus

    cust = Customer(code=f"CUST-{extra.pop('csfx','Z1')}", name="Client",
                    phone="9001002003", location_id=location_id,
                    lat=22.60, lng=88.40, address="Dest Rd")
    await cust.insert()
    a = Assignment(
        customer_id=str(cust.id), customer_name=cust.name,
        employee_id=employee_id, employee_name="Nurse A",
        location_id=location_id, start_date=dt.date(2026, 11, 1),
        acceptance_status=extra.pop("acceptance", AcceptanceStatus.PENDING),
        dest_lat=22.60, dest_lng=88.40, customer_rate=5000, employee_payout=800,
        **extra,
    )
    await a.insert()
    return a, cust


@pytest_asyncio.fixture
async def staff_ctx(client):
    token, staff_id = await _register_staff(client, "nursea@test.com")
    loc = await _seed_location_id()
    return {"headers": {"Authorization": f"Bearer {token}"},
            "staff_id": staff_id, "location_id": loc}


async def test_list_assignments_no_money(client, staff_ctx):
    await _mk_assignment(staff_ctx["staff_id"], staff_ctx["location_id"], csfx="L1")
    resp = await client.get("/api/v1/staff-portal/me/assignments", headers=staff_ctx["headers"])
    assert resp.status_code == 200, resp.text
    items = resp.json()["data"]["items"]
    assert len(items) == 1
    for item in items:
        assert _MONEY_KEYS.isdisjoint(item.keys())
    assert items[0]["customer_phone"] == "9001002003"


async def test_list_pending_filter(client, staff_ctx):
    from app.models.enums import AcceptanceStatus

    await _mk_assignment(staff_ctx["staff_id"], staff_ctx["location_id"], csfx="P1")
    await _mk_assignment(staff_ctx["staff_id"], staff_ctx["location_id"], csfx="P2",
                         acceptance=AcceptanceStatus.ACCEPTED)
    resp = await client.get("/api/v1/staff-portal/me/assignments?pending=true",
                            headers=staff_ctx["headers"])
    items = resp.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["acceptance_status"] == "pending"


async def test_accept_sets_accepted_and_notifies_branch(client, staff_ctx):
    from app.models.notification import Notification

    a, _ = await _mk_assignment(staff_ctx["staff_id"], staff_ctx["location_id"], csfx="AC")
    resp = await client.post(
        f"/api/v1/staff-portal/me/assignments/{a.id}/accept", headers=staff_ctx["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["acceptance_status"] == "accepted"
    # Branch (admin broadcast) notification exists for this assignment.
    notes = await Notification.find(
        {"employee_id": None, "reference_id": str(a.id)}).to_list()
    assert any("accept" in (n.title + n.message).lower() for n in notes)


async def test_decline_frees_nurse_and_notifies(client, staff_ctx):
    from app.models.employee import Employee
    from app.models.enums import EmployeeStatus
    from app.models.notification import Notification

    a, _ = await _mk_assignment(staff_ctx["staff_id"], staff_ctx["location_id"], csfx="DC")
    emp = await Employee.get(staff_ctx["staff_id"])
    emp.status = EmployeeStatus.ASSIGNED
    await emp.save()

    resp = await client.post(
        f"/api/v1/staff-portal/me/assignments/{a.id}/decline",
        headers=staff_ctx["headers"], json={"reason": "sick"})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["acceptance_status"] == "declined"
    emp = await Employee.get(staff_ctx["staff_id"])
    assert emp.status == EmployeeStatus.AVAILABLE
    from app.models.assignment import Assignment
    assert await Assignment.get(a.id) is not None  # not deleted
    notes = await Notification.find(
        {"employee_id": None, "reference_id": str(a.id)}).to_list()
    assert any("declin" in (n.title + n.message).lower() for n in notes)


async def test_cross_staff_forbidden(client, staff_ctx):
    a, _ = await _mk_assignment(staff_ctx["staff_id"], staff_ctx["location_id"], csfx="X1")
    token2, _ = await _register_staff(client, "nurseb@test.com", sfx="B")
    resp = await client.post(
        f"/api/v1/staff-portal/me/assignments/{a.id}/accept",
        headers={"Authorization": f"Bearer {token2}"})
    assert resp.status_code == 404


async def test_route_ok(client, staff_ctx, monkeypatch):
    from app.services.geo_service import geo_service

    async def _fake_dir(origin, dest, *args, **kwargs):
        pref = kwargs.get("route_preference", "shortest")
        return {
            "distance_m": 4200,
            "duration_s": 600,
            "readable_distance": "4.2 km",
            "readable_duration": "10 mins",
            "geometry": [[88.36, 22.57], [88.40, 22.60]],
            "route_preference": pref,
        }

    monkeypatch.setattr(geo_service, "directions", _fake_dir)
    a, _ = await _mk_assignment(staff_ctx["staff_id"], staff_ctx["location_id"], csfx="R1")
    resp = await client.get(
        f"/api/v1/staff-portal/me/assignments/{a.id}/route?lat=22.57&lng=88.36&preference=fastest",
        headers=staff_ctx["headers"])
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["distance_m"] == 4200 and data["geometry"] is not None
    assert data["preference"] == "fastest"


async def test_route_survives_geo_failure(client, staff_ctx, monkeypatch):
    from app.services.geo_service import geo_service

    async def _fake_dir(origin, dest, *args, **kwargs):
        return None

    monkeypatch.setattr(geo_service, "directions", _fake_dir)
    a, _ = await _mk_assignment(staff_ctx["staff_id"], staff_ctx["location_id"], csfx="R2")
    resp = await client.get(
        f"/api/v1/staff-portal/me/assignments/{a.id}/route?lat=22.57&lng=88.36",
        headers=staff_ctx["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["geometry"] is None


async def test_location_ping_persists(client, staff_ctx):
    from app.models.employee import Employee

    resp = await client.post(
        "/api/v1/staff-portal/me/location", headers=staff_ctx["headers"],
        json={"lat": 22.58, "lng": 88.37})
    assert resp.status_code == 200, resp.text
    emp = await Employee.get(staff_ctx["staff_id"])
    assert emp.last_lat == 22.58 and emp.last_lng == 88.37
