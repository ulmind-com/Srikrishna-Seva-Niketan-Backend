"""Assignment acceptance lifecycle: model defaults, geocode wiring, backfill."""

import datetime as dt


async def test_assignment_defaults_pending(db):
    from app.models.assignment import Assignment
    from app.models.enums import AcceptanceStatus

    a = Assignment(
        customer_id="c",
        customer_name="C",
        employee_id="e",
        employee_name="E",
        location_id="l",
        start_date=dt.date(2026, 1, 1),
    )
    assert a.acceptance_status == AcceptanceStatus.PENDING
    assert a.dest_lat is None and a.dest_lng is None


_MONEY_KEYS = {
    "customer_rate",
    "employee_payout",
    "other_expense",
    "notes",
    "billing_rate",
    "daily_rate",
}


def test_staff_schema_has_no_money():
    from app.schemas.assignment import StaffAssignmentResponse

    assert _MONEY_KEYS.isdisjoint(StaffAssignmentResponse.model_fields.keys())


def test_admin_response_has_acceptance():
    from app.schemas.assignment import AssignmentResponse

    assert "acceptance_status" in AssignmentResponse.model_fields


# ---- create-time geocode + destination snapshot -------------------------


async def _seed_location_id() -> str:
    from app.models.location import Location

    loc = await Location.find_one({})
    return str(loc.id)


async def _mk_customer(location_id, **extra):
    from app.models.customer import Customer

    c = Customer(code=f"CUST-{extra.pop('sfx','A1')}", name="Client", phone="9990001111",
                 location_id=location_id, **extra)
    await c.insert()
    return c


async def _mk_employee(location_id, sfx="A1"):
    from app.models.employee import Employee

    e = Employee(code=f"EMP-{sfx}", name="Nurse", location_id=location_id)
    await e.insert()
    return e


async def test_create_assignment_uses_customer_pin(client, auth_headers, monkeypatch):
    from app.services.geo_service import geo_service

    called = {"n": 0}

    async def _fake_geocode(addr):
        called["n"] += 1
        return (0.0, 0.0)

    monkeypatch.setattr(geo_service, "geocode", _fake_geocode)

    loc = await _seed_location_id()
    cust = await _mk_customer(loc, sfx="PIN", lat=22.5, lng=88.3, address="somewhere")
    emp = await _mk_employee(loc, sfx="PIN")

    resp = await client.post(
        "/api/v1/assignments",
        headers=auth_headers,
        json={"customer_id": str(cust.id), "employee_id": str(emp.id),
              "start_date": "2026-11-01"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["dest_lat"] == 22.5 and data["dest_lng"] == 88.3
    assert called["n"] == 0  # pin present → no geocode


async def test_create_assignment_geocodes_when_no_pin(client, auth_headers, monkeypatch):
    from app.services.geo_service import geo_service

    async def _fake_geocode(addr):
        return (22.511, 88.311)

    monkeypatch.setattr(geo_service, "geocode", _fake_geocode)

    loc = await _seed_location_id()
    cust = await _mk_customer(loc, sfx="NOPIN", address="42 Test Rd")
    emp = await _mk_employee(loc, sfx="NOPIN")

    resp = await client.post(
        "/api/v1/assignments",
        headers=auth_headers,
        json={"customer_id": str(cust.id), "employee_id": str(emp.id),
              "start_date": "2026-11-01"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["dest_lat"] == 22.511


async def test_create_customer_geocodes_address(client, auth_headers, monkeypatch):
    from app.models.customer import Customer
    from app.services.geo_service import geo_service

    async def _fake_geocode(addr):
        return (22.577, 88.366)

    monkeypatch.setattr(geo_service, "geocode", _fake_geocode)

    loc = await _seed_location_id()
    resp = await client.post(
        "/api/v1/customers",
        headers=auth_headers,
        json={"name": "Pin Client", "phone": "9876500000",
              "address": "12 Lake Rd", "location_id": loc},
    )
    assert resp.status_code == 201, resp.text
    cust = await Customer.get(resp.json()["data"]["id"])
    assert cust.lat == 22.577 and cust.lng == 88.366


async def test_backfill_marks_existing_accepted(db):
    import sys
    from pathlib import Path

    from app.models.assignment import Assignment
    from app.models.enums import AcceptanceStatus

    loc = await _seed_location_id()
    for i in (1, 2):
        a = Assignment(customer_id=f"c{i}", customer_name="C", employee_id=f"e{i}",
                       employee_name="E", location_id=loc, start_date=dt.date(2026, 1, 1))
        await a.insert()

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from backfill_acceptance import backfill

    count = await backfill()
    assert count >= 2
    for a in await Assignment.find({}).to_list():
        assert a.acceptance_status == AcceptanceStatus.ACCEPTED
