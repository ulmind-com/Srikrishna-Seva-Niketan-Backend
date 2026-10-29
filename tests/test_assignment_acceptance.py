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
