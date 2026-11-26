import datetime as dt
import pytest
from httpx import AsyncClient

from app.models.assignment import Assignment
from app.models.customer import Customer
from app.models.employee import Employee
from app.models.enums import AssignmentStatus, EmployeeStatus, ShiftType
from app.models.location import Location


@pytest.mark.asyncio
async def test_replace_staff_and_consolidated_monthly_bill(
    client: AsyncClient, auth_headers: dict
):
    headers = auth_headers

    # 1. Setup Location, Customer, Staff 1 (Arnab), Staff 2 (Rupa)
    loc = Location(name="Kolkata Branch", code="KOL01", city="Kolkata", is_active=True)
    await loc.insert()
    loc_id = str(loc.id)

    cust = Customer(
        name="Sagnik Mondal",
        code="CUST-100",
        phone="9876543210",
        address="Salt Lake, Kolkata",
        location_id=loc_id,
        billing_rate=1200,
    )
    await cust.insert()
    cust_id = str(cust.id)

    emp1 = Employee(
        name="Arnab Senapati",
        code="SSN-001",
        phone="9000000001",
        location_id=loc_id,
        daily_rate=700,
        is_active=True,
        status=EmployeeStatus.AVAILABLE,
    )
    await emp1.insert()
    emp1_id = str(emp1.id)

    emp2 = Employee(
        name="Rupa Mondal",
        code="SSN-002",
        phone="9000000002",
        location_id=loc_id,
        daily_rate=750,
        is_active=True,
        status=EmployeeStatus.AVAILABLE,
    )
    await emp2.insert()
    emp2_id = str(emp2.id)

    # 2. Create Assignment 1 for emp1: 2026-10-01 to 2026-10-30 (30 days)
    assign_payload = {
        "customer_id": cust_id,
        "employee_id": emp1_id,
        "location_id": loc_id,
        "shift": "full_24h",
        "start_date": "2026-10-01",
        "end_date": "2026-10-30",
        "customer_daily_rate": 1200,
        "employee_daily_rate": 700,
    }
    res = await client.post("/api/v1/assignments", json=assign_payload, headers=headers)
    assert res.status_code == 201
    assign1_id = res.json()["data"]["id"]

    # Check emp1 is marked ASSIGNED
    emp1_refreshed = await Employee.get(emp1_id)
    assert emp1_refreshed.status == EmployeeStatus.ASSIGNED

    # 3. Replace emp1 with emp2 starting from 2026-10-11 (so emp1 worked 1st to 10th = 10 days)
    replace_payload = {
        "new_employee_id": emp2_id,
        "effective_date": "2026-10-11",
        "reason": "Family emergency",
    }
    replace_res = await client.post(
        f"/api/v1/assignments/{assign1_id}/replace-staff",
        json=replace_payload,
        headers=headers,
    )
    assert replace_res.status_code == 200, replace_res.text
    rep_data = replace_res.json()["data"]

    # Verify previous assignment ends on replacement effective date (2026-10-11)
    prev_a = rep_data["previous_assignment"]
    assert prev_a["end_date"] == "2026-10-11"
    assert prev_a["customer_rate"] == 1200 * 11  # 11 days (1st to 11th)

    # Verify new assignment starts 2026-10-11 and ends 2026-10-30 (20 days)
    new_a = rep_data["new_assignment"]
    assert new_a["employee_id"] == emp2_id
    assert new_a["employee_name"] == "Rupa Mondal"
    assert new_a["start_date"] == "2026-10-11"
    assert new_a["end_date"] == "2026-10-30"
    assert new_a["customer_rate"] == 1200 * 20  # 20 days

    # Check new emp2 is marked ASSIGNED
    emp2_refreshed = await Employee.get(emp2_id)
    assert emp2_refreshed.status == EmployeeStatus.ASSIGNED

    # 4. Check Monthly Bills for 2026-10
    bill_res = await client.get("/api/v1/billing/monthly?month=2026-10", headers=headers)
    assert bill_res.status_code == 200
    bill_data = bill_res.json()["data"]

    # There must be EXACTLY ONE bill for Customer "Sagnik Mondal", not two separate bills!
    bills = [b for b in bill_data["bills"] if b["customer_id"] == cust_id]
    assert len(bills) == 1

    cust_bill = bills[0]
    assert cust_bill["customer_name"] == "Sagnik Mondal"
    assert "Arnab Senapati" in cust_bill["employee_name"]
    assert "Rupa Mondal" in cust_bill["employee_name"]
    assert cust_bill["total_days"] == 31  # 11 + 20 = 31 days
    assert cust_bill["total_amount"] == 1200 * 31  # Rs 37,200

    # Line items check:
    assert len(cust_bill["line_items"]) == 2
    li1, li2 = cust_bill["line_items"][0], cust_bill["line_items"][1]
    assert li1["employee_name"] == "Arnab Senapati"
    assert li1["days"] == 11
    assert li1["amount"] == 13200

    assert li2["employee_name"] == "Rupa Mondal"
    assert li2["days"] == 20
    assert li2["amount"] == 24000

    # 5. Issue the monthly bill number
    issue_res = await client.post(
        "/api/v1/billing/monthly/issue",
        json={"month": "2026-10", "customer_id": cust_id},
        headers=headers,
    )
    assert issue_res.status_code == 200
    issued_no = issue_res.json()["data"]["bill_no"]
    assert issued_no == 1
    assert issue_res.json()["data"]["bill_no_formatted"] == "001"

    # Re-calling issue returns same bill_no (idempotent)
    issue_res_repeat = await client.post(
        "/api/v1/billing/monthly/issue",
        json={"month": "2026-10", "customer_id": cust_id},
        headers=headers,
    )
    assert issue_res_repeat.json()["data"]["bill_no"] == issued_no


@pytest.mark.asyncio
async def test_batch_replace_multiple_staff(client: AsyncClient, auth_headers: dict):
    headers = auth_headers
    loc = Location(name="Batch Branch", code="BB01", city="Kolkata", is_active=True)
    await loc.insert()
    loc_id = str(loc.id)

    cust = Customer(name="Multi Staff Patient", code="CUST-MULTI", phone="9876543210", location_id=loc_id, billing_rate=2000)
    await cust.insert()
    cust_id = str(cust.id)

    # 4 staff members
    staff_ids = []
    for i in range(1, 5):
        emp = Employee(name=f"Nurse {i}", code=f"N-{i}", location_id=loc_id, is_active=True, status=EmployeeStatus.AVAILABLE)
        await emp.insert()
        staff_ids.append(str(emp.id))

    # Assign 2 nurses (Nurse 1 Day, Nurse 2 Night)
    asg1_res = await client.post(
        "/api/v1/assignments",
        json={
            "customer_id": cust_id, "employee_id": staff_ids[0], "location_id": loc_id,
            "shift": "day", "start_date": "2026-11-01", "end_date": "2026-11-30",
        },
        headers=headers,
    )
    asg2_res = await client.post(
        "/api/v1/assignments",
        json={
            "customer_id": cust_id, "employee_id": staff_ids[1], "location_id": loc_id,
            "shift": "night", "start_date": "2026-11-01", "end_date": "2026-11-30",
        },
        headers=headers,
    )
    a1_id, a2_id = asg1_res.json()["data"]["id"], asg2_res.json()["data"]["id"]

    # Both Nurse 1 and Nurse 2 have emergencies: replace BOTH simultaneously with Nurse 3 and Nurse 4!
    batch_res = await client.post(
        "/api/v1/assignments/batch-replace",
        json={
            "replacements": [
                {"assignment_id": a1_id, "new_employee_id": staff_ids[2], "effective_date": "2026-11-16", "reason": "Emergency 1"},
                {"assignment_id": a2_id, "new_employee_id": staff_ids[3], "effective_date": "2026-11-16", "reason": "Emergency 2"},
            ]
        },
        headers=headers,
    )
    assert batch_res.status_code == 200, batch_res.text
    batch_data = batch_res.json()["data"]
    assert batch_data["count"] == 2
    assert len(batch_data["items"]) == 2

    # Test multi_staff_assign: assign 2 new nurses at once to the same customer
    multi_res = await client.post(
        "/api/v1/assignments/multi-assign",
        json={
            "customer_id": cust_id,
            "start_date": "2026-12-01",
            "duration_days": 15,
            "staff_list": [
                {"employee_id": staff_ids[0], "shift": "day"},
                {"employee_id": staff_ids[1], "shift": "night"},
            ],
        },
        headers=headers,
    )
    assert multi_res.status_code == 200, multi_res.text
    multi_data = multi_res.json()["data"]
    assert multi_data["count"] == 2
    assert len(multi_data["items"]) == 2


@pytest.mark.asyncio
async def test_replace_staff_with_shift_timing_and_dates(client: AsyncClient, auth_headers: dict):
    headers = auth_headers
    loc = Location(name="Shift Timing Branch", code="STB01", city="Kolkata", is_active=True)
    await loc.insert()
    loc_id = str(loc.id)

    cust = Customer(name="Time Test Customer", code="CUST-TT01", phone="9988776655", location_id=loc_id, is_active=True)
    await cust.insert()
    cust_id = str(cust.id)

    emp_orig = Employee(name="Nurse Original", phone="9000000001", code="NO01", category="nurse", location_id=loc_id, is_active=True)
    emp_new = Employee(name="Nurse Replacement", phone="9000000002", code="NR02", category="nurse", location_id=loc_id, is_active=True)
    await emp_orig.insert()
    await emp_new.insert()

    # Create initial assignment: Day shift
    orig_asg = Assignment(
        customer_id=cust_id,
        customer_name=cust.name,
        employee_id=str(emp_orig.id),
        employee_name=emp_orig.name,
        location_id=loc_id,
        shift=ShiftType.DAY,
        start_time="08:00",
        end_time="20:00",
        start_date=dt.date(2026, 11, 1),
        end_date=dt.date(2026, 11, 30),
        customer_daily_rate=1500,
        employee_daily_rate=800,
        status=AssignmentStatus.ACTIVE,
    )
    await orig_asg.insert()

    # Replace with custom shift and new timing
    rep_res = await client.post(
        f"/api/v1/assignments/{orig_asg.id}/replace-staff",
        json={
            "new_employee_id": str(emp_new.id),
            "effective_date": "2026-11-15",
            "end_date": "2026-11-28",
            "shift": "night",
            "start_time": "20:00",
            "end_time": "08:00",
            "reason": "Shift swapped due to family event",
        },
        headers=headers,
    )
    assert rep_res.status_code == 200, rep_res.text
    rep_data = rep_res.json()["data"]

    new_asg = rep_data["new_assignment"]
    assert new_asg["employee_name"] == "Nurse Replacement"
    assert new_asg["shift"] == "night"
    assert new_asg["start_time"] == "20:00"
    assert new_asg["end_time"] == "08:00"
    assert new_asg["start_date"] == "2026-11-15"
    assert new_asg["end_date"] == "2026-11-28"
    assert new_asg["is_replacement"] is True
    assert new_asg["replacement_for_employee_name"] == "Nurse Original"

    # Replace with custom shift windows (e.g. morning 6am-6pm + evening 8pm-2am)
    emp_window = Employee(name="Nurse MultiWindow", phone="9000000003", code="NMW03", category="nurse", location_id=loc_id, is_active=True)
    await emp_window.insert()

    rep_res_windows = await client.post(
        f"/api/v1/assignments/{new_asg['id']}/replace-staff",
        json={
            "new_employee_id": str(emp_window.id),
            "effective_date": "2026-11-20",
            "end_date": "2026-11-30",
            "shift": "custom",
            "shift_windows": [
                {"start": "06:00", "end": "18:00"},
                {"start": "20:00", "end": "02:00"},
            ],
            "reason": "Custom multi-window duty hours",
        },
        headers=headers,
    )
    assert rep_res_windows.status_code == 200, rep_res_windows.text
    new_asg_win = rep_res_windows.json()["data"]["new_assignment"]
    assert new_asg_win["shift"] == "custom"
    assert new_asg_win["start_time"] == "06:00"
    assert new_asg_win["end_time"] == "02:00"
    assert len(new_asg_win["shift_windows"]) == 2
    assert new_asg_win["shift_windows"][0]["start"] == "06:00"
    assert new_asg_win["shift_windows"][1]["end"] == "02:00"




