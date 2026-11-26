import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient

from app.models.location import Location


@pytest.mark.asyncio
async def test_employee_create_keeps_custom_join_date_and_sets_ist_verified_at(
    client: AsyncClient, auth_headers: dict
):
    loc = Location(name="Verify Date Branch", code="VDB01", city="Kolkata", is_active=True)
    await loc.insert()

    res = await client.post(
        "/api/v1/employees",
        json={
            "name": "Verified Staff Test",
            "phone": "9876511122",
            "location_id": str(loc.id),
            "category": "nurse",
            "join_date": "2026-11-10",
            "daily_rate": 700,
        },
        headers=auth_headers,
    )
    assert res.status_code == 201, res.text
    emp = res.json()["data"]

    # Date of Joining matches input date
    assert emp["join_date"] == "2026-11-10"
    assert emp["verification_status"] == "verified"

    # Verified On is set to entry/verification timestamp (today in IST), NOT join_date
    assert emp["verified_at"] is not None
    verified_dt = dt.datetime.fromisoformat(emp["verified_at"].replace("Z", "+00:00"))
    today_ist = dt.datetime.now(ZoneInfo("Asia/Kolkata")).date()
    assert verified_dt.astimezone(ZoneInfo("Asia/Kolkata")).date() == today_ist
