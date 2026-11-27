"""E2E tests for auto-completing expired assignments & blocking duplicate/overlapping duties."""

import datetime as dt
import pytest
from httpx import AsyncClient

from app.models.assignment import Assignment
from app.models.customer import Customer
from app.models.employee import Employee
from app.models.enums import (
    AssignmentPlace,
    AssignmentStatus,
    EmployeeStatus,
    ShiftType,
    StaffVerificationStatus,
)
from app.models.location import Location
from app.utils.ist_time import today_ist


@pytest.mark.asyncio
async def test_expired_active_assignment_auto_completes_and_releases_staff(
    client: AsyncClient, auth_headers: dict
):
    loc = Location(name="Salt Lake Branch", code="SL01", city="Kolkata", is_active=True)
    await loc.insert()
    loc_id = str(loc.id)

    cust = Customer(
        name="Samiran M",
        code="CUST-EXP-1",
        phone="9800000111",
        location_id=loc_id,
    )
    await cust.insert()

    emp = Employee(
        name="Sagnik M",
        code="EMP-EXP-1",
        phone="9700000111",
        category="nurse",
        daily_rate=350,
        verification_status=StaffVerificationStatus.VERIFIED,
        status=EmployeeStatus.ASSIGNED,
        is_active=True,
        location_id=loc_id,
    )
    await emp.insert()

    today = today_ist()
    past_start = today - dt.timedelta(days=7)
    past_end = today - dt.timedelta(days=2)

    asg = Assignment(
        customer_id=str(cust.id),
        customer_name=cust.name,
        employee_id=str(emp.id),
        employee_name=emp.name,
        location_id=loc_id,
        place_type=AssignmentPlace.HOME,
        shift=ShiftType.DAY,
        start_time="08:00",
        end_time="20:00",
        start_date=past_start,
        end_date=past_end,
        status=AssignmentStatus.ACTIVE,
        customer_daily_rate=500,
        employee_daily_rate=350,
    )
    await asg.insert()

    # Listing assignments or employees auto-completes expired assignments & frees staff
    res_asg = await client.get("/api/v1/assignments", headers=auth_headers)
    assert res_asg.status_code == 200
    items = res_asg.json()["data"]["items"]
    row = next(x for x in items if x["id"] == str(asg.id))
    assert row["status"] == "completed"

    res_emp = await client.get("/api/v1/employees", headers=auth_headers)
    assert res_emp.status_code == 200
    emp_row = next(x for x in res_emp.json()["data"]["items"] if x["id"] == str(emp.id))
    assert emp_row["status"] == "available"


@pytest.mark.asyncio
async def test_prevent_duplicate_overlapping_assignment_even_against_replaced_or_completed(
    client: AsyncClient, auth_headers: dict
):
    loc = Location(name="Salt Lake Branch 2", code="SL02", city="Kolkata", is_active=True)
    await loc.insert()
    loc_id = str(loc.id)

    cust = Customer(
        name="Samiran Ghosh",
        code="CUST-DUP-1",
        phone="9800000222",
        location_id=loc_id,
    )
    await cust.insert()

    emp = Employee(
        name="Arnab Se",
        code="EMP-DUP-1",
        phone="9700000222",
        category="nurse",
        daily_rate=300,
        verification_status=StaffVerificationStatus.VERIFIED,
        status=EmployeeStatus.AVAILABLE,
        is_active=True,
        location_id=loc_id,
    )
    await emp.insert()

    # Existing REPLACED 24h assignment: 01 Nov 07:00 AM -> 15 Nov 07:00 AM
    existing = Assignment(
        customer_id=str(cust.id),
        customer_name=cust.name,
        employee_id=str(emp.id),
        employee_name=emp.name,
        location_id=loc_id,
        place_type=AssignmentPlace.HOME,
        shift=ShiftType.FULL_24H,
        start_time="07:00",
        end_time="07:00",
        start_date=dt.date(2026, 11, 1),
        end_date=dt.date(2026, 11, 15),
        status=AssignmentStatus.REPLACED,
        customer_daily_rate=500,
        employee_daily_rate=300,
    )
    await existing.insert()

    # Attempt to create an overlapping Night/Day/Ongoing duty on 10 Nov for the SAME customer -> MUST FAIL
    dup = await client.post(
        "/api/v1/assignments",
        json={
            "customer_id": str(cust.id),
            "employee_id": str(emp.id),
            "shift": "night",
            "start_time": "20:00",
            "end_time": "08:00",
            "start_date": "2026-11-10",
            "customer_daily_rate": 500,
            "employee_daily_rate": 300,
        },
        headers=auth_headers,
    )
    assert dup.status_code == 400
    assert "Samiran Ghosh" in dup.text
    assert "Arnab Se" in dup.text

    # Exact handover start at 15 Nov 07:00 AM (after 15 Nov 07:00 AM end) -> ALLOWED
    ok_handover = await client.post(
        "/api/v1/assignments",
        json={
            "customer_id": str(cust.id),
            "employee_id": str(emp.id),
            "shift": "full_24h",
            "start_time": "07:00",
            "end_time": "07:00",
            "start_date": "2026-11-15",
            "end_date": "2026-11-20",
            "customer_daily_rate": 500,
            "employee_daily_rate": 300,
        },
        headers=auth_headers,
    )
    assert ok_handover.status_code == 201, ok_handover.text
