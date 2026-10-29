"""Government-ID verification via a vision model (Google Gemini).

Browser OCR (Tesseract) can't reliably read Indian government IDs, so the image
is read server-side by a multimodal model that returns the name, date of birth
and ID number as JSON. ``match_id`` then compares those against what the nurse
entered and decides whether all three match.

The Gemini key lives only in ``settings.GEMINI_API_KEY`` and never reaches the
browser. Every network call is failure-safe: on error ``extract`` returns None.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 30.0
_API = "https://generativelanguage.googleapis.com/v1beta/models"

_PROMPT = (
    "You are verifying an Indian government identity document. Read the image "
    "and return ONLY JSON with these keys: "
    '{"name": string|null, "dob": "DD/MM/YYYY"|null, "id_number": string|null, '
    '"document_type": string|null, "is_government_id": boolean, "readable": boolean}. '
    "name is the full printed name of the holder. dob is the date of birth. "
    "id_number is the main ID / Aadhaar / PAN / Voter number exactly as printed. "
    "is_government_id is true only if this is a genuine-looking government ID "
    "(not a template, sample, blank form or random photo). readable is false if "
    "the image is too blurry or cropped to read. Do not invent values; use null "
    "when a field is not visible."
)

_SALUTATIONS = {"mr", "mrs", "ms", "miss", "dr", "shri", "smt", "sri", "md"}


def _digits(s: Optional[str]) -> str:
    return re.sub(r"\D", "", s or "")


def _name_tokens(name: str) -> list[str]:
    toks = re.sub(r"[^a-z\s]", " ", (name or "").lower()).split()
    return [t for t in toks if t and t not in _SALUTATIONS]


def _dob_parts(s: Optional[str]) -> Optional[tuple[int, int, int]]:
    """Normalise a date string to (day, month, year). Handles ISO + D/M/Y."""
    nums = re.findall(r"\d+", s or "")
    if len(nums) < 3:
        return None
    a, b, c = (int(nums[0]), int(nums[1]), int(nums[2]))
    # ISO YYYY-MM-DD when the first chunk is a 4-digit year.
    if a > 31:
        return (c, b, a)
    return (a, b, c)


def match_id(extracted: dict, *, name: str, id_number: str, dob: str, id_type: str) -> dict:
    """Compare the vision extraction against the entered values.

    ``ok`` requires: readable, a government ID, and name + DOB + ID all match.
    """
    warnings: list[str] = []
    readable = bool(extracted.get("readable", True))
    is_gov = bool(extracted.get("is_government_id", False))

    # ID number: all entered digits present in the extracted number.
    entered_digits = _digits(id_number)
    extracted_digits = _digits(extracted.get("id_number"))
    id_number_matched = bool(entered_digits) and entered_digits == extracted_digits
    if not id_number_matched and entered_digits and entered_digits in extracted_digits:
        id_number_matched = True

    # Name: first and last entered tokens both appear in the extracted name.
    entered_name = _name_tokens(name)
    doc_name = set(_name_tokens(extracted.get("name") or ""))
    if not entered_name:
        name_matched = False
    elif len(entered_name) == 1:
        name_matched = entered_name[0] in doc_name
    else:
        name_matched = entered_name[0] in doc_name and entered_name[-1] in doc_name

    # DOB: day/month/year all equal.
    ed, xd = _dob_parts(dob), _dob_parts(extracted.get("dob"))
    dob_matched = ed is not None and xd is not None and ed == xd

    if not readable:
        warnings.append("Document image is unreadable — upload a clearer original scan.")
    if not is_gov:
        warnings.append("Image does not look like a genuine government ID.")
    if not id_number_matched:
        warnings.append(f"The entered {id_type} number does not match the document.")
    if not name_matched:
        warnings.append("The name on the document does not match your registered name.")
    if not dob_matched:
        warnings.append("The date of birth does not match the document.")

    ok = readable and is_gov and id_number_matched and name_matched and dob_matched
    confidence = 25 * sum([id_number_matched, name_matched, dob_matched, is_gov])

    return {
        "ok": ok,
        "readable": readable,
        "is_government_id": is_gov,
        "name_matched": name_matched,
        "dob_matched": dob_matched,
        "id_number_matched": id_number_matched,
        "fraud_risk": "low" if ok else "high",
        "confidence": confidence,
        "extracted": {
            "name": extracted.get("name"),
            "dob": extracted.get("dob"),
            "id_number": extracted.get("id_number"),
            "document_type": extracted.get("document_type"),
        },
        "warnings": warnings,
    }


class IdVisionService:
    """Reads a government-ID image with Gemini and returns the fields as JSON."""

    async def extract(self, image_bytes: bytes, mime_type: str) -> Optional[dict]:
        if not settings.GEMINI_API_KEY or not image_bytes:
            return None
        url = f"{_API}/{settings.GEMINI_MODEL}:generateContent?key={settings.GEMINI_API_KEY}"
        body = {
            "contents": [
                {
                    "parts": [
                        {"inline_data": {"mime_type": mime_type,
                                          "data": base64.b64encode(image_bytes).decode()}},
                        {"text": _PROMPT},
                    ]
                }
            ],
            "generationConfig": {"response_mime_type": "application/json", "temperature": 0},
        }
        # The flash model can briefly return 503 under load; retry a couple times.
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                    resp = await client.post(url, json=body)
                    if resp.status_code == 503 and attempt < 2:
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text)
            except Exception:  # noqa: BLE001 — vision failure must never 500 the endpoint
                if attempt < 2:
                    await asyncio.sleep(1.0)
                    continue
                logger.warning("Gemini ID extraction failed", exc_info=True)
                return None
        return None


id_vision_service = IdVisionService()
