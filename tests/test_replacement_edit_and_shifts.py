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
    assert issue_done.json()["data"]["bill_no"] == 1
    assert issue_done.json()["data"]["bill_no_formatted"] == "001"

    # Sequential general bills: 1st -> 001, 2nd -> 002, repeat on 1st -> 001
    gb1 = await client.post(
        "/api/v1/billing/monthly/issue",
        json={"customer_id": cust_id, "start_date": "2026-11-01", "end_date": "2026-11-30"},
        headers=auth_headers,
    )
    assert gb1.status_code == 200, gb1.text
    assert gb1.json()["data"]["bill_no"] == 1
    assert gb1.json()["data"]["bill_no_formatted"] == "001"

    # Seed second customer + ended assignment for sequential 002 test
    from app.models.customer import Customer as _Cust
    cust2 = _Cust(name="Second Client", code="CUST-902", phone="9876500099", location_id=loc_id, billing_rate=1000)
    await cust2.insert()
    r2 = await client.post(
        "/api/v1/assignments",
        json={
            "customer_id": str(cust2.id),
            "employee_id": emp1_id,
            "location_id": loc_id,
            "shift": "day",
            "start_date": "2026-11-20",
            "end_date": "2026-11-22",
            "start_time": "08:00",
            "end_time": "20:00",
            "customer_daily_rate": 1000,
        },
        headers=auth_headers,
    )
    assert r2.status_code == 201, r2.text
    gb2 = await client.post(
        "/api/v1/billing/monthly/issue",
        json={"customer_id": str(cust2.id), "start_date": "2026-11-01", "end_date": "2026-11-30"},
        headers=auth_headers,
    )
    assert gb2.status_code == 200, gb2.text
    assert gb2.json()["data"]["bill_no"] == 2
    assert gb2.json()["data"]["bill_no_formatted"] == "002"


@pytest.mark.asyncio
async def test_contract_amount_preserved_while_daily_bill_and_payout_calculate(
    client: AsyncClient, auth_headers: dict
):
    loc_id, cust_id, emp1_id, emp2_id = await _seed_branch_and_people()

    # 1. Create ongoing assignment with contract_amount=14000, daily customer=500, daily staff=300, daily expense=50
    res = await client.post(
        "/api/v1/assignments",
        json={
            "customer_id": cust_id,
            "employee_id": emp1_id,
            "location_id": loc_id,
            "shift": "full_24h",
            "start_date": "2026-11-01",
            "start_time": "07:00",
            "contract_amount": 14000,
            "customer_daily_rate": 500,
            "employee_daily_rate": 300,
            "daily_other_expense": 50,
        },
        headers=auth_headers,
    )
    assert res.status_code == 201, res.text
    a1 = res.json()["data"]
    assert a1["contract_amount"] == 14000
    assert a1["daily_other_expense"] == 50
    assert a1["customer_rate"] is None
    assert a1["employee_payout"] is None
    assert a1["other_expense"] is None

    # 2. Replace on Nov 15 07:00 -> 14 days => Bill=7000, Pay=4200, Exp=700 (50x14), Profit=2100, Contract STILL 14000!
    rep = await client.post(
        f"/api/v1/assignments/{a1['id']}/replace-staff",
        json={
            "new_employee_id": emp2_id,
            "effective_date": "2026-11-15",
            "shift": "full_24h",
            "start_time": "07:00",
            "reason": "Handover",
        },
        headers=auth_headers,
    )
    assert rep.status_code == 200, rep.text
    prev_a = rep.json()["data"]["previous_assignment"]
    new_a = rep.json()["data"]["new_assignment"]

    assert prev_a["days_worked"] == 14.0
    assert prev_a["contract_amount"] == 14000
    assert prev_a["customer_rate"] == 7000
    assert prev_a["employee_payout"] == 4200
    assert prev_a["daily_other_expense"] == 50
    assert prev_a["other_expense"] == 700
    assert new_a["contract_amount"] == 14000
    assert new_a["daily_other_expense"] == 50

    # 3. Edit predecessor daily rate/end_date -> contract_amount remains 14000, Exp=750 (50x15)!
    upd = await client.put(
        f"/api/v1/assignments/{a1['id']}",
        json={"end_date": "2026-11-16", "end_time": "07:00"},
        headers=auth_headers,
    )
    assert upd.status_code == 200, upd.text
    a1_upd = upd.json()["data"]
    assert a1_upd["days_worked"] == 15.0
    assert a1_upd["contract_amount"] == 14000
    assert a1_upd["customer_rate"] == 7500
    assert a1_upd["employee_payout"] == 4500
    assert a1_upd["other_expense"] == 750


@pytest.mark.asyncio
async def test_attendance_payslips_reports_match_assignment_24h_days_and_payout(
    client: AsyncClient, auth_headers: dict
):
    import datetime as dt
    from app.models.attendance import AttendanceRecord
    from app.models.enums import AttendanceStatus, ShiftType
    from app.services import report_service

    loc_id, cust_id, emp1_id, emp2_id = await _seed_branch_and_people()

    # 1. Assign emp1 24h from Nov 1 07:00 AM; replaced Nov 15 07:00 AM -> 14 days (Rs 300/day = 4200)
    res = await client.post(
        "/api/v1/assignments",
        json={
            "customer_id": cust_id,
            "employee_id": emp1_id,
            "location_id": loc_id,
            "shift": "full_24h",
            "start_date": "2026-11-01",
            "start_time": "07:00",
            "contract_amount": 14000,
            "customer_daily_rate": 500,
            "employee_daily_rate": 300,
            "daily_other_expense": 50,
        },
        headers=auth_headers,
    )
    assert res.status_code == 201, res.text
    a1_id = res.json()["data"]["id"]

    # Simulate an old stale Nov 15 attendance row with wrong pay_rate=500 having existed before replacement
    await AttendanceRecord(
        employee_id=emp1_id,
        employee_name="Staff One",
        location_id=loc_id,
        date=dt.date(2026, 11, 15),
        status=AttendanceStatus.PRESENT,
        shift=ShiftType.FULL_24H,
        start_time="07:00",
        end_time="07:00",
        assignment_id=a1_id,
        customer_name="Test Client",
        pay_rate=500,
    ).insert()

    rep = await client.post(
        f"/api/v1/assignments/{a1_id}/replace-staff",
        json={
            "new_employee_id": emp2_id,
            "effective_date": "2026-11-15",
            "shift": "full_24h",
            "start_time": "07:00",
            "reason": "Handover",
        },
        headers=auth_headers,
    )
    assert rep.status_code == 200, rep.text

    # 2. Attendance / Payslip statement MUST show 14 days = Rs 4,200 (NOT 15 days = 4,500)
    slip_res = await client.get(
        f"/api/v1/payroll/payslip?employee_id={emp1_id}&start_date=2026-11-01&end_date=2026-11-30",
        headers=auth_headers,
    )
    assert slip_res.status_code == 200, slip_res.text
    slip = slip_res.json()["data"]
    assert slip["summary"]["present_days"] == 14
    assert slip["summary"]["payable_days"] == 14.0
    assert slip["summary"]["total_shifts"] == 14
    assert slip["summary"]["gross_pay"] == 4200
    worked_dates = [d["date"] for d in slip["days"] if d["status"] == "present"]
    assert worked_dates[0] == "2026-11-01"
    assert worked_dates[-1] == "2026-11-14"
    assert "2026-11-15" not in worked_dates

    # 3. Monthly Payroll Run matches 14 days = Rs 4,200
    run_res = await client.get("/api/v1/payroll/payslips?month=2026-11", headers=auth_headers)
    assert run_res.status_code == 200, run_res.text
    emp1_row = next(r for r in run_res.json()["data"]["rows"] if r["employee_id"] == emp1_id)
    assert emp1_row["payable_days"] == 14.0
    assert emp1_row["gross_pay"] == 4200

    # 4. General Bill matches 14 days = Rs 7,000
    bill_res = await client.get(
        f"/api/v1/billing/monthly?start_date=2026-11-01&end_date=2026-11-30&customer_id={cust_id}",
        headers=auth_headers,
    )
    assert bill_res.status_code == 200, bill_res.text
    bill = bill_res.json()["data"]["bills"][0]
    assert bill["total_days"] == 14.0
    assert bill["total_amount"] == 7000

    # 5. Report P&L and Financial report match Assignment exact totals (7000 / 4200 / 700 / profit=2100)
    pnl = await report_service.profit_loss({}, dt.date(2026, 11, 1), dt.date(2026, 11, 30))
    assert pnl["revenue"] == 7000
    assert pnl["staff_payout"] == 4200
    assert pnl["assignment_expense"] == 700
    assert pnl["profit"] == 2100

    fin = await report_service.duty_report({}, start=dt.date(2026, 11, 1), end=dt.date(2026, 11, 30))
    assert fin["summary"]["total_billed"] == 7000
    assert fin["summary"]["total_staff_payout"] == 4200
    assert fin["summary"]["total_expenses"] == 700
    assert fin["summary"]["total_profit"] == 2100


@pytest.mark.asyncio
async def test_ist_breaks_and_schedule_edits_sync_attendance_payslip_and_bill(
    client: AsyncClient, auth_headers: dict
):
    from app.services import report_service
    import datetime as dt

    loc_id, cust_id, emp1_id, _emp2_id = await _seed_branch_and_people()

    # 1. Create evening-to-evening 24h duty: Nov 1 19:00 (7 PM) -> Nov 15 19:00 (7 PM) = 14.0 days
    res = await client.post(
        "/api/v1/assignments",
        json={
            "customer_id": cust_id,
            "employee_id": emp1_id,
            "location_id": loc_id,
            "shift": "full_24h",
            "start_date": "2026-11-01",
            "end_date": "2026-11-15",
            "start_time": "19:00",
            "end_time": "19:00",
            "customer_daily_rate": 500,
            "employee_daily_rate": 300,
        },
        headers=auth_headers,
    )
    assert res.status_code == 201, res.text
    a1 = res.json()["data"]
    assert a1["days_worked"] == 14.0
    assert a1["customer_rate"] == 7000
    assert a1["employee_payout"] == 4200

    # 2. Apply a 48h pause: Nov 5 19:00 -> Nov 7 19:00 (2.0 days OFF: Nov 5 & Nov 6; resumes Nov 7 19:00)
    brk = await client.post(
        f"/api/v1/assignments/{a1['id']}/breaks",
        json={
            "start_date": "2026-11-05",
            "end_date": "2026-11-07",
            "start_time": "19:00",
            "end_time": "19:00",
            "reason": "Hospitalization",
        },
        headers=auth_headers,
    )
    assert brk.status_code == 200, brk.text
    a1_brk = brk.json()["data"]
    assert a1_brk["days_worked"] == 12.0
    assert a1_brk["customer_rate"] == 6000
    assert a1_brk["employee_payout"] == 3600

    # Payslip immediately reflects 12 present days (3600) and ONLY Nov 5 & Nov 6 off (NOT Nov 7!)
    slip_brk = (
        await client.get(
            f"/api/v1/payroll/payslip?employee_id={emp1_id}&month=2026-11",
            headers=auth_headers,
        )
    ).json()["data"]
    assert slip_brk["summary"]["present_days"] == 12
    assert slip_brk["summary"]["payable_days"] == 12.0
    assert slip_brk["summary"]["gross_pay"] == 3600
    assert slip_brk["off_dates"] == ["2026-11-05", "2026-11-06"]

    # 3. Remove the break -> Attendance & Payslip immediately restore Nov 5 & Nov 6 back to Present (14 days / 4200)
    break_id = a1_brk["duty_breaks"][0]["id"]
    rm_brk = await client.delete(
        f"/api/v1/assignments/{a1['id']}/breaks/{break_id}",
        headers=auth_headers,
    )
    assert rm_brk.status_code == 200, rm_brk.text
    assert rm_brk.json()["data"]["days_worked"] == 14.0

    # 4. Edit time to Nov 1 19:00 -> Nov 16 07:00 (14.5 days -> 14 full days + half-day on Nov 15)
    upd = await client.put(
        f"/api/v1/assignments/{a1['id']}",
        json={"end_date": "2026-11-16", "end_time": "07:00"},
        headers=auth_headers,
    )
    assert upd.status_code == 200, upd.text
    assert upd.json()["data"]["days_worked"] == 14.5
    assert upd.json()["data"]["employee_payout"] == 4350

    slip_half = (
        await client.get(
            f"/api/v1/payroll/payslip?employee_id={emp1_id}&month=2026-11",
            headers=auth_headers,
        )
    ).json()["data"]
    assert slip_half["summary"]["present_days"] == 14
    assert slip_half["summary"]["half_days"] == 1
    assert slip_half["summary"]["payable_days"] == 14.5
    assert slip_half["summary"]["gross_pay"] == 4350
    nov15_row = next(d for d in slip_half["days"] if d["date"] == "2026-11-15")
    assert nov15_row["status"] == "half_day"
    assert nov15_row["start_time"] == "19:00"
    assert nov15_row["end_time"] == "07:00"

    pnl = await report_service.profit_loss({}, dt.date(2026, 11, 1), dt.date(2026, 11, 30))
    assert pnl["revenue"] == 7250
    assert pnl["staff_payout"] == 4350


@pytest.mark.asyncio
async def test_complex_half_day_breaks_and_replacement_chain_sync(
    client: AsyncClient, auth_headers: dict
):
    """End-to-end 12.5d (with middle break) + 13.5d replacement across ALL modules."""
    import datetime as dt
    from app.services import report_service

    loc_id, cust_id, emp1_id, emp2_id = await _seed_branch_and_people()

    # 1. Nurse 1 starts Oct 1 07:00 AM (Rs 1000 cust, Rs 600 staff, Rs 50 exp)
    r1 = await client.post(
        "/api/v1/assignments",
        json={
            "customer_id": cust_id,
            "employee_id": emp1_id,
            "location_id": loc_id,
            "shift": "full_24h",
            "start_date": "2026-10-01",
            "start_time": "07:00",
            "customer_daily_rate": 1000,
            "employee_daily_rate": 600,
            "daily_other_expense": 50,
        },
        headers=auth_headers,
    )
    assert r1.status_code == 201, r1.text
    a1_id = r1.json()["data"]["id"]

    # 2. 2-day middle break: Oct 5 07:00 -> Oct 7 07:00
    brk = await client.post(
        f"/api/v1/assignments/{a1_id}/breaks",
        json={
            "start_date": "2026-10-05",
            "end_date": "2026-10-07",
            "start_time": "07:00",
            "end_time": "07:00",
            "reason": "Hospital gap",
        },
        headers=auth_headers,
    )
    assert brk.status_code == 200, brk.text

    # 3. Replace Nurse 1 with Nurse 2 on Oct 15 at 19:00 (07:00 PM) -> Nurse 1 worked 14.5 - 2.0 = 12.5 days!
    rep = await client.post(
        f"/api/v1/assignments/{a1_id}/replace-staff",
        json={
            "new_employee_id": emp2_id,
            "effective_date": "2026-10-15",
            "shift": "full_24h",
            "start_time": "19:00",
            "end_date": "2026-10-29",
            "end_time": "07:00",
            "reason": "Evening handover",
        },
        headers=auth_headers,
    )
    assert rep.status_code == 200, rep.text
    prev_a = rep.json()["data"]["previous_assignment"]
    new_a = rep.json()["data"]["new_assignment"]

    assert prev_a["days_worked"] == 12.5
    assert prev_a["customer_rate"] == 12500
    assert prev_a["employee_payout"] == 7500
    assert prev_a["other_expense"] == 625

    # Nurse 2: Oct 15 19:00 -> Oct 29 07:00 = 13.5 days
    assert new_a["days_worked"] == 13.5
    assert new_a["customer_rate"] == 13500
    assert new_a["employee_payout"] == int(round(750 * 13.5))  # 10125

    # 4. Payslips for BOTH nurses match exact half-days + breaks!
    slip1 = (
        await client.get(
            f"/api/v1/payroll/payslip?employee_id={emp1_id}&month=2026-10",
            headers=auth_headers,
        )
    ).json()["data"]
    assert slip1["summary"]["present_days"] == 12
    assert slip1["summary"]["half_days"] == 1
    assert slip1["summary"]["off_days"] == 2
    assert slip1["off_dates"] == ["2026-10-05", "2026-10-06"]
    assert slip1["summary"]["payable_days"] == 12.5
    assert slip1["summary"]["gross_pay"] == 7500

    slip2 = (
        await client.get(
            f"/api/v1/payroll/payslip?employee_id={emp2_id}&month=2026-10",
            headers=auth_headers,
        )
    ).json()["data"]
    assert slip2["summary"]["present_days"] == 13
    assert slip2["summary"]["half_days"] == 1
    assert slip2["summary"]["payable_days"] == 13.5
    assert slip2["summary"]["gross_pay"] == 10125

    # 5. General Bill consolidates 12.5d + 13.5d = 26.0d (Rs 26,000)
    bill_res = await client.get(
        f"/api/v1/billing/monthly?start_date=2026-10-01&end_date=2026-10-31&customer_id={cust_id}",
        headers=auth_headers,
    )
    assert bill_res.status_code == 200, bill_res.text
    bill = bill_res.json()["data"]["bills"][0]
    assert bill["total_days"] == 26.0
    assert bill["total_amount"] == 26000
    assert [li["days"] for li in bill["line_items"]] == [12.5, 13.5]

    # 6. Reports match Assignment & Bill totals!
    pnl = await report_service.profit_loss({}, dt.date(2026, 10, 1), dt.date(2026, 10, 31))
    assert pnl["revenue"] == 26000
    assert pnl["staff_payout"] == 7500 + 10125
    assert pnl["assignment_expense"] == 625 + int(round(50 * 13.5))  # 1300
    fin = await report_service.duty_report({}, start=dt.date(2026, 10, 1), end=dt.date(2026, 10, 31))
    assert fin["summary"]["total_billed"] == 26000
    assert fin["summary"]["total_staff_payout"] == 17625
    assert fin["summary"]["total_expenses"] == 1300

