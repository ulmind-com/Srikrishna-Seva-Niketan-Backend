"""Government-ID vision matching: name + DOB + ID number must all match."""


def _extracted(**over):
    base = {
        "name": "Arnab Senapati",
        "dob": "18/10/2002",
        "id_number": "7892 5879 5235",
        "document_type": "Aadhaar",
        "is_government_id": True,
        "readable": True,
    }
    base.update(over)
    return base


def test_match_all_ok():
    from app.services.id_vision_service import match_id

    r = match_id(_extracted(), name="Arnab Senapati", id_number="7892 5879 5235",
                 dob="2002-10-18", id_type="Aadhaar")
    assert r["ok"] is True
    assert r["name_matched"] and r["dob_matched"] and r["id_number_matched"]
    assert r["fraud_risk"] == "low"


def test_match_id_number_mismatch():
    from app.services.id_vision_service import match_id

    r = match_id(_extracted(id_number="1111 2222 3333"), name="Arnab Senapati",
                 id_number="7892 5879 5235", dob="2002-10-18", id_type="Aadhaar")
    assert r["id_number_matched"] is False
    assert r["ok"] is False and r["fraud_risk"] == "high"


def test_match_dob_mismatch():
    from app.services.id_vision_service import match_id

    r = match_id(_extracted(dob="01/01/1990"), name="Arnab Senapati",
                 id_number="7892 5879 5235", dob="2002-10-18", id_type="Aadhaar")
    assert r["dob_matched"] is False
    assert r["ok"] is False


def test_match_name_mismatch():
    from app.services.id_vision_service import match_id

    r = match_id(_extracted(name="Someone Else"), name="Arnab Senapati",
                 id_number="7892 5879 5235", dob="2002-10-18", id_type="Aadhaar")
    assert r["name_matched"] is False
    assert r["ok"] is False


def test_match_not_government_id():
    from app.services.id_vision_service import match_id

    r = match_id(_extracted(is_government_id=False), name="Arnab Senapati",
                 id_number="7892 5879 5235", dob="2002-10-18", id_type="Aadhaar")
    assert r["ok"] is False and r["fraud_risk"] == "high"


# ---- endpoint (vision mocked, no network) ----

async def _staff_token(client):
    from app.core.security import create_access_token
    from app.dependencies.staff_auth import STAFF_SCOPE
    from app.models.employee import Employee
    from app.models.location import Location

    loc = await Location.find_one({})
    emp = Employee(code="EMP-VIS1", name="Arnab Senapati", email="vis@test.com",
                   location_id=str(loc.id))
    await emp.insert()
    access, _, _ = create_access_token(str(emp.id), {"scope": STAFF_SCOPE, "email": emp.email})
    return {"Authorization": f"Bearer {access}"}


async def test_verify_id_endpoint_ok(client, monkeypatch):
    from app.services import id_vision_service as mod

    async def _fake_extract(self, b, m):
        return _extracted()

    monkeypatch.setattr(mod.IdVisionService, "extract", _fake_extract)
    headers = await _staff_token(client)
    resp = await client.post("/api/v1/staff-portal/me/verify-id", headers=headers,
                             json={"image_url": "https://x/id.png", "id_type": "Aadhaar",
                                   "id_number": "7892 5879 5235", "dob": "2002-10-18"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["ok"] is True


async def test_verify_id_endpoint_mismatch(client, monkeypatch):
    from app.services import id_vision_service as mod

    async def _fake_extract(self, b, m):
        return _extracted(id_number="1111 2222 3333")

    monkeypatch.setattr(mod.IdVisionService, "extract", _fake_extract)
    headers = await _staff_token(client)
    resp = await client.post("/api/v1/staff-portal/me/verify-id", headers=headers,
                             json={"image_url": "https://x/id.png", "id_type": "Aadhaar",
                                   "id_number": "7892 5879 5235", "dob": "2002-10-18"})
    assert resp.status_code == 200
    assert resp.json()["data"]["ok"] is False


async def test_verify_id_endpoint_vision_down(client, monkeypatch):
    from app.services import id_vision_service as mod

    async def _fake_extract(self, b, m):
        return None

    monkeypatch.setattr(mod.IdVisionService, "extract", _fake_extract)
    headers = await _staff_token(client)
    resp = await client.post("/api/v1/staff-portal/me/verify-id", headers=headers,
                             json={"image_url": "https://x/id.png", "id_type": "Aadhaar",
                                   "id_number": "7892 5879 5235", "dob": "2002-10-18"})
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["ok"] is False and body["readable"] is False
