import pytest
import datetime as dt
from httpx import AsyncClient

from app.models.assignment import Assignment
from app.models.customer import Customer
from app.models.employee import Employee
from app.models.location import Location
from app.models.enums import AssignmentStatus, EmployeeStatus, PaymentStatus, ShiftType
from app.models.finance import Payment


@pytest.mark.asyncio
async def test_admin_complete_assignment_postpaid_cash(client: AsyncClient, auth_headers: dict):
    headers = auth_headers

    # 1. Create Location
    loc = Location(name="Arambagh Branch", code="AB01", city="Arambagh", is_active=True)
    await loc.insert()
    loc_id = str(loc.id)

    # 2. Create Customer
    cust = Customer(
        name="Dilip Ghosh",
        code="CUST-DG1",
        phone="9876543210",
        address="Arambagh",
        location_id=loc_id,
    )
    await cust.insert()

    # 3. Create Employee
    emp = Employee(
        name="Manoj Barman",
        code="EMP-MB1",
        phone="9876543211",
        category="nurse",
        location_id=loc_id,
        status=EmployeeStatus.ASSIGNED,
        daily_rate=700,
    )
    await emp.insert()

    # 4. Create Postpaid Assignment
    today = dt.date.today()
    assign = Assignment(
        customer_id=str(cust.id),
        customer_name=cust.name,
        employee_id=str(emp.id),
        employee_name=emp.name,
        location_id=loc_id,
        start_date=today - dt.timedelta(days=4),
        end_date=today,
        shift=ShiftType.DAY,
        start_time="08:00",
        end_time="20:00",
        customer_daily_rate=1200,
        employee_daily_rate=700,
        customer_rate=1200 * 5,  # 5 days
        employee_payout=700 * 5,
        payment_type="postpaid",
        payment_status="pending",
        status=AssignmentStatus.ACTIVE,
    )
    await assign.insert()

    # 5. Call admin complete endpoint
    complete_payload = {
        "cash_collected": True,
        "cash_amount": 6000,
        "note": "Nurse uses keypad phone, called to confirm duty ended and cash collected.",
        "received_by": "Staff Manoj Barman (Confirmed by Admin)",
    }
    res = await client.post(
        f"/api/v1/assignments/{assign.id}/complete",
        json=complete_payload,
        headers=headers,
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]

    # Verify assignment status
    assert data["status"] == "completed"
    assert data["payment_status"] == "paid"
    assert data["work_completed_at"] is not None
    assert data["payment_received_at"] is not None
    assert "Manoj Barman" in data["payment_received_by"]

    # Verify employee is freed (AVAILABLE)
    refreshed_emp = await Employee.get(emp.id)
    assert refreshed_emp.status == EmployeeStatus.AVAILABLE

    # Verify Payment record inserted
    payments = await Payment.find({"assignment_id": str(assign.id)}).to_list()
    assert len(payments) == 1
    p = payments[0]
    assert p.amount == 6000
    assert p.status == PaymentStatus.PAID
    assert p.customer_id == str(cust.id)
