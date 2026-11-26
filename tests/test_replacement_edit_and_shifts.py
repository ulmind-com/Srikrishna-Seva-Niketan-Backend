import pytest
from httpx import AsyncClient

from app.models.customer import Customer
from app.models.employee import Employee
from app.models.enums import AssignmentStatus, EmployeeStatus
from app.models.location import Location


async def _seed_branch_and_people():
    loc = Location(name="Salt Lake Branch", code="SLK99", city="Kolkata", is_active=True)
    await loc.insert()
    loc_id = str(loc.id)

    cust = Customer(
        name="Test Client",
        code="CUST-901",
        phone="9876500001",
        address="Salt Lake Sector V",
        location_id=loc_id,
        billing_rate=1200,
    )
    await cust.insert()

    emp1 = Employee(
        name="Staff One",
        code="SSN-901",
        phone="9000090001",
        location_id=loc_id,
        daily_rate=700,
        is_active=True,
        status=EmployeeStatus.AVAILABLE,
    )
    await emp1.insert()

    emp2 = Employee(
        name="Staff Two",
        code="SSN-902",
        phone="9000090002",
        location_id=loc_id,
        daily_rate=750,
        is_active=True,
        status=EmployeeStatus.AVAILABLE,
    )
    await emp2.insert()

    return loc_id, str(cust.id), str(emp1.id), str(emp2.id)


@pytest.mark.asyncio
async def test_replacement_edit_syncs_handover_dates_times_and_amounts(
    client: AsyncClient, auth_headers: dict
):
    loc_id, cust_id, emp1_id, emp2_id = await _seed_branch_and_people()

    # 1. Create ongoing 24h assignment starting 2026-11-01 at 07:00 AM
    res = await client.post(
        "/api/v1/assignments",
        json={
            "customer_id": cust_id,
            "employee_id": emp1_id,
            "location_id": loc_id,
            "shift": "full_24h",
            "start_date": "2026-11-01",
            "start_time": "07:00",
            "end_time": "07:00",
            "customer_daily_rate": 1200,
            "employee_daily_rate": 700,
        },
        headers=auth_headers,
    )
    assert res.status_code == 201, res.text
    a1_id = res.json()["data"]["id"]

    # 2. Replace emp1 with emp2 effective 2026-11-11 at 07:00 AM (emp1 = 10 days = 12,000)
    rep_res = await client.post(
        f"/api/v1/assignments/{a1_id}/replace-staff",
        json={
            "new_employee_id": emp2_id,
            "effective_date": "2026-11-11",
            "shift": "full_24h",
            "start_time": "07:00",
            "end_time": "07:00",
            "end_date": None,
            "reason": "Handover check",
        },
        headers=auth_headers,
    )
    assert rep_res.status_code == 200, rep_res.text
    rep_data = rep_res.json()["data"]
    prev_a = rep_data["previous_assignment"]
    new_a = rep_data["new_assignment"]
    a2_id = new_a["id"]

    assert prev_a["status"] == AssignmentStatus.REPLACED.value
    assert prev_a["end_date"] == "2026-11-11"
    assert prev_a["end_time"] == "07:00"
    assert prev_a["days_worked"] == 10.0
    assert prev_a["customer_rate"] == 12000
    assert prev_a["employee_payout"] == 7000
    assert new_a["replacement_for_assignment_id"] == a1_id

    # 3. Edit replacement assignment a2: move start_date to 2026-11-12 07:00 and set end_date 2026-11-20 07:00
    upd_res = await client.put(
        f"/api/v1/assignments/{a2_id}",
        json={
            "start_date": "2026-11-12",
            "start_time": "07:00",
            "end_date": "2026-11-20",
            "end_time": "07:00",
        },
        headers=auth_headers,
    )
    assert upd_res.status_code == 200, upd_res.text
    a2_after = upd_res.json()["data"]
    assert a2_after["days_worked"] == 8.0
    assert a2_after["customer_rate"] == 1200 * 8
    assert a2_after["employee_payout"] == 750 * 8

    # Verify predecessor a1 automatically synced end_date -> 2026-11-12, days -> 11.0, rates updated
    a1_after = (await client.get(f"/api/v1/assignments/{a1_id}", headers=auth_headers)).json()["data"]
    assert a1_after["end_date"] == "2026-11-12"
    assert a1_after["end_time"] == "07:00"
    assert a1_after["days_worked"] == 11.0
    assert a1_after["total_days"] == 11.0
    assert a1_after["customer_rate"] == 1200 * 11
    assert a1_after["employee_payout"] == 700 * 11

    # 4. Verify editing predecessor end_date/end_time (2026-11-10 19:00) also syncs forward to successor a2!
    upd_prev = await client.put(
        f"/api/v1/assignments/{a1_id}",
        json={
            "end_date": "2026-11-10",
            "end_time": "19:00",
        },
        headers=auth_headers,
    )
    assert upd_prev.status_code == 200, upd_prev.text
    a1_final = upd_prev.json()["data"]
    assert a1_final["days_worked"] == 9.5
    assert a1_final["customer_rate"] == 1200 * 9.5
    assert a1_final["employee_payout"] == 700 * 9.5

    a2_final = (await client.get(f"/api/v1/assignments/{a2_id}", headers=auth_headers)).json()["data"]
    assert a2_final["start_date"] == "2026-11-10"
    assert a2_final["start_time"] == "19:00"
    assert a2_final["days_worked"] == 9.5
    assert a2_final["customer_rate"] == int(round(1200 * 9.5))
    assert a2_final["employee_payout"] == int(round(750 * 9.5))


@pytest.mark.asyncio
async def test_all_shift_types_duty_days_edit_and_replacement_totals(
    client: AsyncClient, auth_headers: dict
):
    loc_id, cust_id, emp1_id, emp2_id = await _seed_branch_and_people()

    # Day shift: Nov 1 -> Nov 10 (08:00 -> 20:00) = 10 days (not 19!)
    r_day = await client.post(
        "/api/v1/assignments",
        json={
            "customer_id": cust_id,
            "employee_id": emp1_id,
            "location_id": loc_id,
            "shift": "day",
            "start_date": "2026-11-01",
            "end_date": "2026-11-10",
            "start_time": "08:00",
            "end_time": "20:00",
            "customer_daily_rate": 1000,
            "employee_daily_rate": 600,
        },
        headers=auth_headers,
    )
    assert r_day.status_code == 201, r_day.text
    day_data = r_day.json()["data"]
    assert day_data["days_worked"] == 10.0
    assert day_data["customer_rate"] == 10000
    assert day_data["employee_payout"] == 6000

    # Edit Day shift end_date -> Nov 15 = 15 days
    r_day_edit = await client.put(
        f"/api/v1/assignments/{day_data['id']}",
        json={"end_date": "2026-11-15"},
        headers=auth_headers,
    )
    assert r_day_edit.status_code == 200, r_day_edit.text
    assert r_day_edit.json()["data"]["days_worked"] == 15.0
    assert r_day_edit.json()["data"]["customer_rate"] == 15000
    assert r_day_edit.json()["data"]["employee_payout"] == 9000

    # Complete / cancel Day assignment so emp1 is free for Night shift test
    await client.put(
        f"/api/v1/assignments/{day_data['id']}",
        json={"status": "completed"},
        headers=auth_headers,
    )

    # Night shift: Nov 1 -> Nov 11 (20:00 -> 08:00) = 10 nights
    r_night = await client.post(
        "/api/v1/assignments",
        json={
            "customer_id": cust_id,
            "employee_id": emp1_id,
            "location_id": loc_id,
            "shift": "night",
            "start_date": "2026-11-01",
            "end_date": "2026-11-11",
            "start_time": "20:00",
            "end_time": "08:00",
            "customer_daily_rate": 1500,
            "employee_daily_rate": 800,
        },
        headers=auth_headers,
    )
    assert r_night.status_code == 201, r_night.text
    night_data = r_night.json()["data"]
    assert night_data["days_worked"] == 10.0
    assert night_data["customer_rate"] == 15000
    assert night_data["employee_payout"] == 8000

    # Replace Night shift effective 2026-11-06 20:00 -> emp1 = 5 nights = 7500
    rep_night = await client.post(
        f"/api/v1/assignments/{night_data['id']}/replace-staff",
        json={
            "new_employee_id": emp2_id,
            "effective_date": "2026-11-06",
            "end_date": "2026-11-11",
            "shift": "night",
            "start_time": "20:00",
            "end_time": "08:00",
        },
        headers=auth_headers,
    )
    assert rep_night.status_code == 200, rep_night.text
    assert rep_night.json()["data"]["previous_assignment"]["days_worked"] == 5.0
    assert rep_night.json()["data"]["previous_assignment"]["customer_rate"] == 7500
    assert rep_night.json()["data"]["new_assignment"]["days_worked"] == 5.0
    assert rep_night.json()["data"]["new_assignment"]["customer_rate"] == 7500

    # Edit replacement Night start_date -> 2026-11-07 20:00 -> emp1 syncs to 6 nights (9000), emp2 = 4 nights (6000)
    rep_night_id = rep_night.json()["data"]["new_assignment"]["id"]
    upd_night = await client.put(
        f"/api/v1/assignments/{rep_night_id}",
        json={"start_date": "2026-11-07", "start_time": "20:00"},
        headers=auth_headers,
    )
    assert upd_night.status_code == 200, upd_night.text
    assert upd_night.json()["data"]["days_worked"] == 4.0
    assert upd_night.json()["data"]["customer_rate"] == 6000

    orig_night_after = (await client.get(f"/api/v1/assignments/{night_data['id']}", headers=auth_headers)).json()["data"]
    assert orig_night_after["end_date"] == "2026-11-07"
    assert orig_night_after["days_worked"] == 6.0
    assert orig_night_after["customer_rate"] == 9000

    await client.put(f"/api/v1/assignments/{rep_night_id}", json={"status": "completed"}, headers=auth_headers)

    # Morning shift: Nov 1 -> Nov 5 (07:00 -> 15:00) = 5 days
    r_morn = await client.post(
        "/api/v1/assignments",
        json={
            "customer_id": cust_id,
            "employee_id": emp1_id,
            "location_id": loc_id,
            "shift": "morning",
            "start_date": "2026-11-01",
            "end_date": "2026-11-05",
            "start_time": "07:00",
            "end_time": "15:00",
            "customer_daily_rate": 800,
            "employee_daily_rate": 400,
        },
        headers=auth_headers,
    )
    assert r_morn.status_code == 201, r_morn.text
    assert r_morn.json()["data"]["days_worked"] == 5.0
    assert r_morn.json()["data"]["customer_rate"] == 4000
    await client.put(f"/api/v1/assignments/{r_morn.json()['data']['id']}", json={"status": "completed"}, headers=auth_headers)

    # Custom shift: single day 09:00 -> 17:00 = 1.0 day (not 0.5!), and 2-window overnight+day Nov 7->Nov 8 = 2.0 days
    r_cust_1d = await client.post(
        "/api/v1/assignments",
        json={
            "customer_id": cust_id,
            "employee_id": emp1_id,
            "location_id": loc_id,
            "shift": "custom",
            "start_date": "2026-11-12",
            "end_date": "2026-11-12",
            "shift_windows": [{"start": "09:00", "end": "17:00"}],
            "customer_daily_rate": 900,
            "employee_daily_rate": 500,
        },
        headers=auth_headers,
    )
    assert r_cust_1d.status_code == 201, r_cust_1d.text
    assert r_cust_1d.json()["data"]["days_worked"] == 1.0
    assert r_cust_1d.json()["data"]["customer_rate"] == 900

    # Edit custom shift into 2-window overnight + day over Nov 7 -> Nov 8 (2 days = 6000)
    r_cust_2w = await client.put(
        f"/api/v1/assignments/{r_cust_1d.json()['data']['id']}",
        json={
            "start_date": "2026-11-07",
            "end_date": "2026-11-08",
            "customer_daily_rate": 3000,
            "shift_windows": [
                {"start": "22:42", "end": "10:00"},
                {"start": "06:00", "end": "20:00"},
            ],
        },
        headers=auth_headers,
    )
    assert r_cust_2w.status_code == 200, r_cust_2w.text
    assert r_cust_2w.json()["data"]["days_worked"] == 2.0
    assert r_cust_2w.json()["data"]["customer_rate"] == 6000


@pytest.mark.asyncio
async def test_general_bill_handover_dates_times_and_days(
    client: AsyncClient, auth_headers: dict
):
    from app.models.assignment import Assignment

    loc_id, cust_id, emp1_id, emp2_id = await _seed_branch_and_people()

    # 1. Assign emp1 ongoing from Nov 1 07:00 AM
    r1 = await client.post(
        "/api/v1/assignments",
        json={
            "customer_id": cust_id,
            "employee_id": emp1_id,
            "location_id": loc_id,
            "shift": "full_24h",
            "start_date": "2026-11-01",
            "start_time": "07:00",
            "end_time": None,
            "customer_daily_rate": 1000,
            "employee_daily_rate": 700,
        },
        headers=auth_headers,
    )
    assert r1.status_code == 201, r1.text
    a1_id = r1.json()["data"]["id"]

    # 2. Replace emp1 with emp2 effective Nov 15 07:00 AM (emp2 ongoing)
    rep = await client.post(
        f"/api/v1/assignments/{a1_id}/replace-staff",
        json={
            "new_employee_id": emp2_id,
            "effective_date": "2026-11-15",
            "end_date": None,
            "shift": "full_24h",
            "start_time": "07:00",
            "end_time": None,
            "reason": "Scheduled rotation",
        },
        headers=auth_headers,
    )
    assert rep.status_code == 200, rep.text

    # Even if a1 had a stale days_worked in DB (e.g. 15 from old bug), General Bill must calculate 14!
    a1_doc = await Assignment.get(a1_id)
    a1_doc.days_worked = 15.0
    a1_doc.customer_rate = 15000
    await a1_doc.save()

    # 3. General Bill for 1st to 15th: Nov 1 07:00 AM -> Nov 15 07:00 AM MUST be 14 days, NOT 15!
    bill_1_15 = await client.get(
        f"/api/v1/billing/monthly?start_date=2026-11-01&end_date=2026-11-15&customer_id={cust_id}",
        headers=auth_headers,
    )
    assert bill_1_15.status_code == 200, bill_1_15.text
    bills_15 = bill_1_15.json()["data"]["bills"]
    assert len(bills_15) == 1
    assert bills_15[0]["total_days"] == 14.0
    assert bills_15[0]["total_amount"] == 14000
    assert len(bills_15[0]["line_items"]) == 1
    li1 = bills_15[0]["line_items"][0]
    assert li1["from"] == "2026-11-01"
    assert li1["to"] == "2026-11-15"
    assert li1["from_time"] == "07:00"
    assert li1["to_time"] == "07:00"
    assert li1["days"] == 14.0

    # 4. General Bill for Nov 1..30 must NOT bill ongoing emp2 yet (no end_date / not completed)!
    bill_full_ongoing = await client.get(
        f"/api/v1/billing/monthly?start_date=2026-11-01&end_date=2026-11-30&customer_id={cust_id}",
        headers=auth_headers,
    )
    assert bill_full_ongoing.status_code == 200, bill_full_ongoing.text
    b_ongoing = bill_full_ongoing.json()["data"]["bills"][0]
    assert b_ongoing["total_days"] == 14.0
    assert b_ongoing["total_amount"] == 14000
    assert len(b_ongoing["line_items"]) == 1

    # Issuing an individual bill for ongoing emp2 before work ends must also fail cleanly (400)
    a2_id = rep.json()["data"]["new_assignment"]["id"]
    issue_ongoing = await client.post(f"/api/v1/assignments/{a2_id}/issue-bill", headers=auth_headers)
    assert issue_ongoing.status_code == 400

    # 5. Once emp2 gets an end_date (Nov 30 07:00 AM), BOTH Staff 1 (14d) & Staff 2 (15d) are billed!
    upd2 = await client.put(
        f"/api/v1/assignments/{a2_id}",
        json={"end_date": "2026-11-30", "end_time": "07:00"},
        headers=auth_headers,
    )
    assert upd2.status_code == 200, upd2.text

    bill_full = await client.get(
        f"/api/v1/billing/monthly?start_date=2026-11-01&end_date=2026-11-30&customer_id={cust_id}",
        headers=auth_headers,
    )
    assert bill_full.status_code == 200, bill_full.text
    b_full = bill_full.json()["data"]["bills"][0]
    assert b_full["total_days"] == 29.0
    assert b_full["total_amount"] == 29000
    assert [li["days"] for li in b_full["line_items"]] == [14.0, 15.0]
    assert b_full["line_items"][1]["from_time"] == "07:00"
    assert b_full["line_items"][1]["to_time"] == "07:00"

    issue_done = await client.post(f"/api/v1/assignments/{a2_id}/issue-bill", headers=auth_headers)
    assert issue_done.status_code == 200, issue_done.text
    assert issue_done.json()["data"]["bill_no"] >= 1000

