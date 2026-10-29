"""Staff onboarding submit: the OCR fraud audit is advisory, not a hard gate.

A flagged document (the browser OCR often misreads genuine Aadhaar photos) must
still be submittable — the super-admin reviews the uploaded document manually.
"""

import datetime as dt


async def _seed_location_id():
    from app.models.location import Location

    loc = await Location.find_one({})
    return str(loc.id)


async def _complete_draft_staff(fraud: bool):
    from app.core.security import create_access_token
    from app.dependencies.staff_auth import STAFF_SCOPE
    from app.models.base import ImageAsset
    from app.models.employee import Employee
    from app.models.enums import StaffVerificationStatus

    emp = Employee(
        code="EMP-ONB1",
        name="Arnab Senapati",
        email="onb@test.com",
        location_id=await _seed_location_id(),
        verification_status=StaffVerificationStatus.DRAFT,
        phone="9990001111",
        address="42 Test Rd",
        experience="3 years",
        work_history="home care",
        id_proof_type="Aadhaar",
        id_proof_number="7892 5879 5235",
        photo=ImageAsset(url="https://x/p.png"),
        signature=ImageAsset(url="https://x/s.png"),
        id_proof=ImageAsset(url="https://x/id.png"),
        document_verification=(
            {"fraudRisk": "high", "nameMatched": False, "idNumberMatched": False}
            if fraud
            else {"fraudRisk": "low", "nameMatched": True, "idNumberMatched": True}
        ),
    )
    await emp.insert()
    access, _, _ = create_access_token(str(emp.id), {"scope": STAFF_SCOPE, "email": emp.email})
    return access, str(emp.id)


async def test_submit_succeeds_despite_fraud_flag(client):
    from app.models.employee import Employee
    from app.models.enums import StaffVerificationStatus

    token, staff_id = await _complete_draft_staff(fraud=True)
    resp = await client.post(
        "/api/v1/staff-portal/me/submit",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    emp = await Employee.get(staff_id)
    assert emp.verification_status == StaffVerificationStatus.PENDING
