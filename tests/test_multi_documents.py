import pytest
from httpx import AsyncClient

from app.models.customer import Customer
from app.models.employee import Employee
from app.models.location import Location


@pytest.mark.asyncio
async def test_employee_and_customer_multiple_documents(
    client: AsyncClient, auth_headers: dict
):
    loc = Location(name="Main Branch", code="MAIN01", city="Kolkata", is_active=True)
    await loc.insert()
    loc_id = str(loc.id)

    # 1. Create Staff with Aadhaar + Voter ID + PAN
    emp_payload = {
        "name": "Multi Doc Nurse",
        "phone": "9000011122",
        "location_id": loc_id,
        "documents": [
            {
                "doc_type": "Aadhaar",
                "doc_number": "1234 5678 9012",
                "file": {"url": "https://example.com/aadhaar.jpg"},
            },
            {
                "doc_type": "Voter ID",
                "doc_number": "ABC1234567",
                "file": {"url": "https://example.com/voter.jpg"},
            },
            {
                "doc_type": "PAN",
                "doc_number": "ABCDE1234F",
                "file": {"url": "https://example.com/pan.jpg"},
            },
        ],
    }
    res_emp = await client.post("/api/v1/employees", json=emp_payload, headers=auth_headers)
    assert res_emp.status_code == 201
    emp_data = res_emp.json()["data"]
    assert len(emp_data["documents"]) == 3
    assert emp_data["id_proof_type"] == "Aadhaar"
    assert emp_data["id_proof_number"] == "1234 5678 9012"
    assert emp_data["id_proof"]["url"] == "https://example.com/aadhaar.jpg"
    assert emp_data["voter_number"] == "ABC1234567"
    assert emp_data["voter_photo"]["url"] == "https://example.com/voter.jpg"
    assert emp_data["pan_number"] == "ABCDE1234F"
    assert emp_data["pan_photo"]["url"] == "https://example.com/pan.jpg"

    emp_in_db = await Employee.get(emp_data["id"])
    assert emp_in_db is not None
    assert len(emp_in_db.documents) == 3
    assert emp_in_db.join_date is not None

    # 1b. Create Staff with explicit join_date
    res_emp_jd = await client.post(
        "/api/v1/employees",
        json={
            "name": "Explicit Join Date Nurse",
            "phone": "9000011199",
            "location_id": loc_id,
            "join_date": "2026-11-10",
        },
        headers=auth_headers,
    )
    assert res_emp_jd.status_code == 201
    assert res_emp_jd.json()["data"]["join_date"] == "2026-11-10"

    # 2. Update Staff to add Driving License as 4th document
    res_upd = await client.put(
        f"/api/v1/employees/{emp_data['id']}",
        json={
            "documents": [
                *emp_payload["documents"],
                {"doc_type": "Driving License", "doc_number": "WB01-20220001"},
            ]
        },
        headers=auth_headers,
    )
    assert res_upd.status_code == 200
    assert len(res_upd.json()["data"]["documents"]) == 4

    # 3. Create Customer with Aadhaar + PAN (including PDF attachment)
    cust_payload = {
        "name": "Multi Doc Client",
        "phone": "9800011122",
        "location_id": loc_id,
        "documents": [
            {
                "doc_type": "Aadhaar",
                "doc_number": "9999 8888 7777",
                "file": {
                    "url": "https://example.com/aadhaar.pdf",
                    "format": "pdf",
                    "original_filename": "aadhaar.pdf",
                    "resource_type": "raw",
                },
            },
            {"doc_type": "PAN", "doc_number": "ZZZZZ9999Z", "file": {"url": "https://example.com/cpan.jpg"}},
        ],
    }
    res_cust = await client.post("/api/v1/customers", json=cust_payload, headers=auth_headers)
    assert res_cust.status_code == 201
    cust_data = res_cust.json()["data"]
    assert len(cust_data["documents"]) == 2
    assert cust_data["documents"][0]["file"]["format"] == "pdf"
    assert cust_data["documents"][0]["file"]["original_filename"] == "aadhaar.pdf"
    assert cust_data["id_proof_type"] == "Aadhaar"
    assert cust_data["id_proof_number"] == "9999 8888 7777"

    cust_in_db = await Customer.get(cust_data["id"])
    assert cust_in_db is not None
    assert len(cust_in_db.documents) == 2
