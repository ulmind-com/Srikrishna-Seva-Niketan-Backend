import datetime as dt
import pytest
from httpx import AsyncClient

from app.models.assignment import Assignment
from app.models.attendance import AttendanceRecord
from app.models.billing import MonthlyBill
from app.models.booking import Booking
from app.models.customer import Customer
from app.models.employee import Employee
from app.models.enums import AttendanceStatus, BookingStatus, EmployeeStatus, NotificationType, ShiftType
from app.models.finance import Expense, Payment
from app.models.location import Location
from app.models.notification import Notification


@pytest.mark.asyncio
async def test_delete_branch_cascades_all_branch_data(
    client: AsyncClient, auth_headers: dict
):
    # Branch A (to delete) & Branch B (must remain untouched)
    loc_a = Location(name="Arambagh Branch", code="ARB01", city="Arambagh", is_active=True)
    await loc_a.insert()
    loc_a_id = str(loc_a.id)

    loc_b = Location(name="Kolkata Branch", code="KOL02", city="Kolkata", is_active=True)
    await loc_b.insert()
    loc_b_id = str(loc_b.id)

    emp_a = Employee(
        name="Staff A", code="EMP-A1", phone="9000000011",
        location_id=loc_a_id, daily_rate=500, is_active=True, status=EmployeeStatus.ASSIGNED,
    )
    await emp_a.insert()
    emp_a_id = str(emp_a.id)

    emp_b = Employee(
        name="Staff B", code="EMP-B1", phone="9000000012",
        location_id=loc_b_id, daily_rate=600, is_active=True, status=EmployeeStatus.AVAILABLE,
    )
    await emp_b.insert()
    emp_b_id = str(emp_b.id)

    cust_a = Customer(
        name="Customer A", code="CUST-A1", phone="9800000011",
        address="Arambagh", location_id=loc_a_id, billing_rate=1000,
    )
    await cust_a.insert()
    cust_a_id = str(cust_a.id)

    cust_b = Customer(
        name="Customer B", code="CUST-B1", phone="9800000012",
        address="Salt Lake", location_id=loc_b_id, billing_rate=1200,
    )
    await cust_b.insert()
    cust_b_id = str(cust_b.id)

    asgn_a = Assignment(
        customer_id=cust_a_id, customer_name=cust_a.name,
        employee_id=emp_a_id, employee_name=emp_a.name,
        location_id=loc_a_id, shift=ShiftType.FULL_24H,
        start_date=dt.date(2026, 11, 1), end_date=dt.date(2026, 11, 5),
        customer_daily_rate=1000, employee_daily_rate=500,
        customer_rate=5000, employee_payout=2500,
    )
    await asgn_a.insert()
    asgn_a_id = str(asgn_a.id)

    asgn_b = Assignment(
        customer_id=cust_b_id, customer_name=cust_b.name,
        employee_id=emp_b_id, employee_name=emp_b.name,
        location_id=loc_b_id, shift=ShiftType.FULL_24H,
        start_date=dt.date(2026, 11, 1), end_date=dt.date(2026, 11, 5),
        customer_daily_rate=1200, employee_daily_rate=600,
        customer_rate=6000, employee_payout=3000,
    )
    await asgn_b.insert()
    asgn_b_id = str(asgn_b.id)

    await AttendanceRecord(
        employee_id=emp_a_id, employee_name=emp_a.name, location_id=loc_a_id,
        date=dt.date(2026, 11, 1), status=AttendanceStatus.PRESENT,
        assignment_id=asgn_a_id, customer_name=cust_a.name, pay_rate=500,
    ).insert()
    await AttendanceRecord(
        employee_id=emp_b_id, employee_name=emp_b.name, location_id=loc_b_id,
        date=dt.date(2026, 11, 1), status=AttendanceStatus.PRESENT,
        assignment_id=asgn_b_id, customer_name=cust_b.name, pay_rate=600,
    ).insert()

    await MonthlyBill(
        key=f"{cust_a_id}:2026-11", customer_id=cust_a_id,
        employee_id=emp_a_id, location_id=loc_a_id,
        month="2026-11", bill_no=1001, total_days=5, total_amount=5000,
    ).insert()
    await MonthlyBill(
        key=f"{cust_b_id}:2026-11", customer_id=cust_b_id,
        employee_id=emp_b_id, location_id=loc_b_id,
        month="2026-11", bill_no=1002, total_days=5, total_amount=6000,
    ).insert()

    await Expense(category="rent", amount=2000, location_id=loc_a_id).insert()
    await Expense(category="rent", amount=3000, location_id=loc_b_id).insert()

    await Payment(
        customer_id=cust_a_id, customer_name=cust_a.name,
        assignment_id=asgn_a_id, amount=5000, location_id=loc_a_id,
    ).insert()
    await Payment(
        customer_id=cust_b_id, customer_name=cust_b.name,
        assignment_id=asgn_b_id, amount=6000, location_id=loc_b_id,
    ).insert()

    await Booking(
        reference="BK-A", patient_name="Patient A", contact_name="Cust A",
        contact_phone="9800000011", service_name="Home Nursing",
        preferred_date=dt.date(2026, 11, 1), address="Arambagh",
        status=BookingStatus.ASSIGNED, customer_id=cust_a_id,
        assigned_staff_id=emp_a_id, location_id=loc_a_id,
    ).insert()
    await Notification(
        employee_id=emp_a_id, type=NotificationType.ASSIGNMENT,
        title="New duty", reference_id=asgn_a_id,
    ).insert()

    # Delete Branch A
    res = await client.delete(f"/api/v1/locations/{loc_a_id}", headers=auth_headers)
    assert res.status_code == 200

    # Everything under Branch A must be deleted
    assert await Location.get(loc_a_id) is None
    assert await Employee.find({"location_id": loc_a_id}).count() == 0
    assert await Customer.find({"location_id": loc_a_id}).count() == 0
    assert await Assignment.find({"location_id": loc_a_id}).count() == 0
    assert await AttendanceRecord.find({"employee_id": emp_a_id}).count() == 0
    assert await MonthlyBill.find({"customer_id": cust_a_id}).count() == 0
    assert await Expense.find({"location_id": loc_a_id}).count() == 0
    assert await Payment.find({"customer_id": cust_a_id}).count() == 0
    assert await Booking.find({"location_id": loc_a_id}).count() == 0
    assert await Notification.find({"employee_id": emp_a_id}).count() == 0

    # Branch B data must stay intact
    assert await Location.get(loc_b_id) is not None
    assert await Employee.find({"location_id": loc_b_id}).count() == 1
    assert await Customer.find({"location_id": loc_b_id}).count() == 1
    assert await Assignment.find({"location_id": loc_b_id}).count() == 1
    assert await AttendanceRecord.find({"employee_id": emp_b_id}).count() == 1
    assert await MonthlyBill.find({"customer_id": cust_b_id}).count() == 1
    assert await Expense.find({"location_id": loc_b_id}).count() == 1
    assert await Payment.find({"customer_id": cust_b_id}).count() == 1


@pytest.mark.asyncio
async def test_delete_employee_cascades_payslips_attendance_and_duties(
    client: AsyncClient, auth_headers: dict
):
    loc = Location(name="Test Branch", code="TST01", city="Kolkata", is_active=True)
    await loc.insert()
    loc_id = str(loc.id)

    emp = Employee(
        name="Nurse One", code="EMP-N1", phone="9000000099",
        location_id=loc_id, daily_rate=500, is_active=True, status=EmployeeStatus.ASSIGNED,
    )
    await emp.insert()
    emp_id = str(emp.id)

    cust = Customer(
        name="Client One", code="CUST-C1", phone="9800000099",
        address="Kolkata", location_id=loc_id, billing_rate=1000,
    )
    await cust.insert()
    cust_id = str(cust.id)

    asgn = Assignment(
        customer_id=cust_id, customer_name=cust.name,
        employee_id=emp_id, employee_name=emp.name,
        location_id=loc_id, shift=ShiftType.DAY,
        start_date=dt.date(2026, 11, 2), end_date=dt.date(2026, 11, 4),
    )
    await asgn.insert()
    asgn_id = str(asgn.id)

    await AttendanceRecord(
        employee_id=emp_id, employee_name=emp.name, location_id=loc_id,
        date=dt.date(2026, 11, 2), status=AttendanceStatus.PRESENT,
        assignment_id=asgn_id, customer_name=cust.name, pay_rate=500,
    ).insert()
    await Notification(
        employee_id=emp_id, type=NotificationType.ASSIGNMENT,
        title="Assigned", reference_id=asgn_id,
    ).insert()

    res = await client.delete(f"/api/v1/employees/{emp_id}", headers=auth_headers)
    assert res.status_code == 200

    assert await Employee.get(emp_id) is None
    assert await Assignment.find({"employee_id": emp_id}).count() == 0
    assert await AttendanceRecord.find({"employee_id": emp_id}).count() == 0
    assert await Notification.find({"employee_id": emp_id}).count() == 0
    # Customer and branch stay intact
    assert await Customer.get(cust_id) is not None
    assert await Location.get(loc_id) is not None
