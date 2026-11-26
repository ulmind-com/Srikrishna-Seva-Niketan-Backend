"""End-to-end tests for Payments & Receipts (Client partial/full, Staff salary payouts,
Transaction history), Accounting Rule (no double counting in P&L), and Staff Leave Management."""

from __future__ import annotations

import datetime as dt
import pytest
from httpx import AsyncClient

from app.models.customer import Customer
from app.models.employee import Employee
from app.models.enums import EmployeeStatus, PaymentStatus
from app.models.location import Location


async def _seed_data():
    loc = Location(name="Arambagh Branch", code="ARB01", city="Arambagh", is_active=True)
    await loc.insert()
    loc_id = str(loc.id)

    c1 = Customer(name="Client A", code="C-A", phone="9000011111", location_id=loc_id, billing_rate=1000)
    c2 = Customer(name="Client B", code="C-B", phone="9000022222", location_id=loc_id, billing_rate=1000)
    c3 = Customer(name="Client C", code="C-C", phone="9000033333", location_id=loc_id, billing_rate=1000)
    await c1.insert()
    await c2.insert()
    await c3.insert()

    e1 = Employee(name="Staff A", code="S-A", phone="9111111111", location_id=loc_id, daily_rate=500, status=EmployeeStatus.AVAILABLE)
    e2 = Employee(name="Staff B", code="S-B", phone="9222222222", location_id=loc_id, daily_rate=500, status=EmployeeStatus.AVAILABLE)
    await e1.insert()
    await e2.insert()

    return loc_id, c1, c2, c3, e1, e2


@pytest.mark.asyncio
async def test_client_partial_payments_receipts_staff_payouts_and_reports(
    client: AsyncClient, auth_headers: dict
):
    loc_id, c1, c2, c3, e1, e2 = await _seed_data()

    # Client A: 10 days = Rs 10,000 invoice, staff = 5,000
    a1 = (
        await client.post(
            "/api/v1/assignments",
            json={
                "customer_id": str(c1.id),
                "employee_id": str(e1.id),
                "location_id": loc_id,
                "shift": "day",
                "start_date": "2026-10-01",
                "end_date": "2026-10-10",
                "start_time": "08:00",
                "end_time": "20:00",
                "customer_daily_rate": 1000,
                "employee_daily_rate": 500,
            },
            headers=auth_headers,
        )
    ).json()["data"]
    assert a1["customer_rate"] == 10000

    # Client B: 10 days = Rs 10,000 invoice (Staff B = 5,000)
    a2 = (
        await client.post(
            "/api/v1/assignments",
            json={
                "customer_id": str(c2.id),
                "employee_id": str(e2.id),
                "location_id": loc_id,
                "shift": "day",
                "start_date": "2026-10-01",
                "end_date": "2026-10-10",
                "start_time": "08:00",
                "end_time": "20:00",
                "customer_daily_rate": 1000,
                "employee_daily_rate": 500,
            },
            headers=auth_headers,
        )
    ).json()["data"]

    # Client C: 5 days @ 1,100 = Rs 5,500 invoice (Total across 3 clients = Rs 25,500!)
    # Finish e1 first by setting status completed on a1
    await client.put(
        f"/api/v1/assignments/{a1['id']}",
        json={"status": "completed"},
        headers=auth_headers,
    )
    a3 = (
        await client.post(
            "/api/v1/assignments",
            json={
                "customer_id": str(c3.id),
                "employee_id": str(e1.id),
                "location_id": loc_id,
                "shift": "day",
                "start_date": "2026-10-11",
                "end_date": "2026-10-15",
                "start_time": "08:00",
                "end_time": "20:00",
                "customer_daily_rate": 1100,
                "employee_daily_rate": 500,
            },
            headers=auth_headers,
        )
    ).json()["data"]
    assert a3["customer_rate"] == 5500

    # 1. Partial payment from Client A: Rs 5,000 via UPI -> Receipt RCP-001, Assignment status -> partial
    p1 = await client.post(
        "/api/v1/payments",
        json={
            "customer_id": str(c1.id),
            "assignment_id": a1["id"],
            "amount": 5000,
            "date": "2026-10-12",
            "mode": "upi",
            "transaction_id": "UPI-1001",
            "note": "First installment",
        },
        headers=auth_headers,
    )
    assert p1.status_code == 201, p1.text
    assert p1.json()["data"]["receipt_no"] == "RCP-001"
    a1_after = (await client.get(f"/api/v1/assignments/{a1['id']}", headers=auth_headers)).json()["data"]
    assert a1_after["payment_status"] == PaymentStatus.PARTIAL.value

    # Full payment from Client B (Rs 10,000) & Partial from Client C (Rs 5,000) => Total Received = Rs 20,000
    p2 = await client.post(
        "/api/v1/payments",
        json={"customer_id": str(c2.id), "assignment_id": a2["id"], "amount": 10000, "date": "2026-10-13", "mode": "cash"},
        headers=auth_headers,
    )
    assert p2.status_code == 201
    assert p2.json()["data"]["receipt_no"] == "RCP-002"

    p3 = await client.post(
        "/api/v1/payments",
        json={"customer_id": str(c3.id), "assignment_id": a3["id"], "amount": 5000, "date": "2026-10-16", "mode": "bank_transfer", "transaction_id": "UTR-99"},
        headers=auth_headers,
    )
    assert p3.status_code == 201
    assert p3.json()["data"]["receipt_no"] == "RCP-003"

    # Client Ledger matches exact totals: Billed = 25,500 | Received = 20,000 | Outstanding = 5,500 | Rate = 78.43%
    cl = (
        await client.get(
            "/api/v1/payments/client-ledger?start=2026-10-01&end=2026-10-31",
            headers=auth_headers,
        )
    ).json()["data"]
    assert cl["summary"]["total_billed"] == 25500
    assert cl["summary"]["total_received"] == 20000
    assert cl["summary"]["total_outstanding"] == 5500
    assert cl["summary"]["collection_rate_pct"] == 78.43
    assert cl["summary"]["paid_clients"] == 1
    assert cl["summary"]["partial_clients"] == 2

    # 2. Staff Payments (Partial salary payout): Staff A earned 7,500 (15d * 500), paid 4,000 -> 3,500 remaining
    sp1 = await client.post(
        "/api/v1/payments/staff",
        json={
            "employee_id": str(e1.id),
            "month": "2026-10",
            "amount": 4000,
            "date": "2026-10-20",
            "mode": "bank_transfer",
            "transaction_id": "SAL-01",
            "note": "Advance October payout",
        },
        headers=auth_headers,
    )
    assert sp1.status_code == 201, sp1.text
    assert sp1.json()["data"]["voucher_no"] == "PAY-001"

    sl = (
        await client.get(
            "/api/v1/payments/staff-ledger?month=2026-10",
            headers=auth_headers,
        )
    ).json()["data"]
    s_a = next(r for r in sl["staff"] if r["employee_id"] == str(e1.id))
    assert s_a["salary_due"] == 7500
    assert s_a["paid_amount"] == 4000
    assert s_a["remaining_amount"] == 3500
    assert s_a["status"] == "partial"

    # 3. Payment History combines RCP-001..003 incoming & PAY-001 outgoing
    hist = (
        await client.get(
            "/api/v1/payments/history?start=2026-10-01&end=2026-10-31",
            headers=auth_headers,
        )
    ).json()["data"]
    assert hist["summary"]["total_incoming"] == 20000
    assert hist["summary"]["total_staff_paid"] == 4000
    assert hist["summary"]["net_cash_flow"] == 16000

    # 4. Reports (Profit & Loss + Customer Payments) do NOT double-count cash received on top of revenue!
    pnl = (
        await client.get(
            "/api/v1/reports/profit-loss?start=2026-10-01&end=2026-10-31",
            headers=auth_headers,
        )
    ).json()["data"]
    assert pnl["revenue"] == 25500  # Billed revenue (NOT 25,500 + 20,000!)
    assert pnl["cash_received"] == 20000
    assert pnl["outstanding_receivable"] == 5500
    assert pnl["collection_rate_pct"] == 78.43
    assert pnl["staff_payout"] == 12500  # 7,500 + 5,000 earned
    assert pnl["staff_salary_paid"] == 4000
    assert pnl["staff_salary_remaining"] == 8500
    assert pnl["profit"] == 13000


@pytest.mark.asyncio
async def test_leave_from_assignment_deducts_and_syncs_attendance_and_payslip(
    client: AsyncClient, auth_headers: dict
):
    loc_id, c1, _c2, _c3, e1, _e2 = await _seed_data()

    asg = (
        await client.post(
            "/api/v1/assignments",
            json={
                "customer_id": str(c1.id),
                "employee_id": str(e1.id),
                "location_id": loc_id,
                "shift": "day",
                "start_date": "2026-10-01",
                "end_date": "2026-10-10",
                "start_time": "08:00",
                "end_time": "20:00",
                "customer_daily_rate": 1000,
                "employee_daily_rate": 500,
            },
            headers=auth_headers,
        )
    ).json()["data"]
    assert asg["days_worked"] == 10.0

    # Record 2-day unpaid leave from Assignment (Oct 4..5)
    lv = await client.post(
        "/api/v1/payroll/leaves",
        json={
            "employee_id": str(e1.id),
            "assignment_id": asg["id"],
            "leave_date": "2026-10-04",
            "end_date": "2026-10-05",
            "leave_type": "unpaid",
            "deduct_pay": True,
            "approval_status": "approved",
            "reason": "Family function",
        },
        headers=auth_headers,
    )
    assert lv.status_code == 201, lv.text

    # Assignment bill & payout automatically drop to 8 days (Rs 8,000 / Rs 4,000)
    asg_after = (await client.get(f"/api/v1/assignments/{asg['id']}", headers=auth_headers)).json()["data"]
    assert asg_after["days_worked"] == 8.0
    assert asg_after["customer_rate"] == 8000
    assert asg_after["employee_payout"] == 4000

    # Payslip reflects 8 present days + 2 leave/off days = Rs 4,000
    slip = (
        await client.get(
            f"/api/v1/payroll/payslip?employee_id={e1.id}&month=2026-10",
            headers=auth_headers,
        )
    ).json()["data"]
    assert slip["summary"]["present_days"] == 8
    assert slip["summary"]["off_days"] == 2
    assert slip["summary"]["gross_pay"] == 4000

    # Custom bank-statement date range: Oct 1 .. Oct 3 (before leave on Oct 4-5)
    run_custom = (
        await client.get(
            "/api/v1/payroll/payslips?start_date=2026-10-01&end_date=2026-10-03",
            headers=auth_headers,
        )
    ).json()["data"]
    assert run_custom["period"]["start"] == "2026-10-01"
    assert run_custom["period"]["end"] == "2026-10-03"
    assert run_custom["period"]["days_in_month"] == 3
    row = next(r for r in run_custom["rows"] if r["employee_id"] == str(e1.id))
    assert row["present_days"] == 3
    assert row["gross_pay"] == 1500

    slip_custom = (
        await client.get(
            f"/api/v1/payroll/payslip?employee_id={e1.id}&start_date=2026-10-01&end_date=2026-10-03",
            headers=auth_headers,
        )
    ).json()["data"]
    assert slip_custom["period"]["start"] == "2026-10-01"
    assert slip_custom["period"]["end"] == "2026-10-03"
    assert slip_custom["period"]["days_in_month"] == 3
    assert slip_custom["summary"]["present_days"] == 3
    assert slip_custom["summary"]["gross_pay"] == 1500
    assert len(slip_custom["days"]) == 3
