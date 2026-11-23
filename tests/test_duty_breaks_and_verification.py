"""Tests for Duty Breaks (pause/gap) and Unverified Staff filtering."""

from __future__ import annotations

import datetime as dt
import pytest
from httpx import AsyncClient

from app.models.assignment import Assignment
from app.models.customer import Customer
from app.models.employee import Employee
from app.models.enums import AssignmentStatus, AttendanceStatus, ShiftType, StaffVerificationStatus
from app.schemas.assignment import calculate_duty_days


def test_calculate_duty_days_with_breaks():
    start = dt.date(2026, 11, 1)
    end = dt.date(2026, 11, 15)
    # 15 calendar days without breaks
    gross = calculate_duty_days(start, end, "07:00", "19:00", ShiftType.FULL_24H)
    assert gross == 14.5

    # Add a 2-day break from Nov 5 07:00 to Nov 7 07:00 (2.0 days)
    breaks = [
        {
            "start_date": dt.date(2026, 11, 5),
            "end_date": dt.date(2026, 11, 7),
            "start_time": "07:00",
            "end_time": "07:00",
            "reason": "Customer out of town",
        }
    ]
    net = calculate_duty_days(start, end, "07:00", "19:00", ShiftType.FULL_24H, duty_breaks=breaks)
    assert net == 12.5


async def test_unverified_staff_cannot_be_assigned(client: AsyncClient, auth_headers: dict):
    loc_id = "6aff4b30988f6f916333bb63"
    emp = Employee(
        code="EMP-TEST-UNVERIFIED",
        name="Unverified Staff",
        location_id=loc_id,
        verification_status=StaffVerificationStatus.PENDING,
    )
    await emp.insert()

    cust = Customer(
        code="CUST-TEST-1",
        name="Customer 1",
        phone="9876543210",
        location_id=loc_id,
        service="elder_care",
    )
    await cust.insert()

    # Try creating assignment with unverified staff
    res = await client.post(
        "/api/v1/assignments",
        headers=auth_headers,
        json={
            "customer_id": str(cust.id),
            "employee_id": str(emp.id),
            "start_date": "2026-11-01",
            "end_date": "2026-11-10",
            "shift": "full_24h",
            "customer_daily_rate": 500,
            "employee_daily_rate": 200,
        },
    )
    assert res.status_code == 400
    assert "not verified" in res.json()["message"].lower() or "verification" in res.json()["message"].lower()

    # Clean up
    await emp.delete()
    await cust.delete()
