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
