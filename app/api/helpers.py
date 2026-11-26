"""Router-level helpers for building consistent responses."""

from __future__ import annotations

from typing import Any, List, Sequence, Type

from pydantic import BaseModel, ValidationError

from app.core.logging import get_logger
from app.core.pagination import PaginationParams
from app.core.responses import build_pagination_meta, success_response

logger = get_logger(__name__)


def serialize_list(schema: Type[BaseModel], items: Sequence[Any]) -> List[dict]:
    """Convert a list of documents to JSON-ready dicts via a response schema.

    A single malformed document must not take down the whole list endpoint, so
    items that fail validation are skipped (and logged) instead of raising —
    the endpoint still returns every valid record rather than a 500.
    """
    out: List[dict] = []
    for item in items:
        try:
            out.append(schema.model_validate(item).model_dump(mode="json"))
        except ValidationError as exc:
            logger.warning(
                "Skipping document that failed %s validation: %s",
                schema.__name__,
                exc,
                extra={"doc_id": str(getattr(item, "id", None))},
            )
    return out


def paginated_response(
    schema: Type[BaseModel],
    items: Sequence[Any],
    total: int,
    params: PaginationParams,
    message: str = "Fetched successfully",
) -> dict:
    """Build the standard envelope for a paginated list endpoint."""
    meta = build_pagination_meta(total, params.page, params.page_size)
    return success_response(
        data={
            "items": serialize_list(schema, items),
            "pagination": meta.model_dump(),
        },
        message=message,
    )


def item_response(
    schema: Type[BaseModel],
    item: Any,
    message: str = "Fetched successfully",
) -> dict:
    """Build the standard envelope for a single-item endpoint."""
    return success_response(
        data=schema.model_validate(item).model_dump(mode="json"), message=message
    )


def sync_identity_documents(data: dict, *, sync_kyc: bool = False) -> dict:
    """Normalise ``documents`` array and mirror primary/Voter/PAN fields for backwards compat."""
    if "documents" not in data or data["documents"] is None:
        return data

    cleaned = []
    for raw in data["documents"]:
        if not isinstance(raw, dict):
            continue
        doc_type = (raw.get("doc_type") or "").strip()
        doc_num = (raw.get("doc_number") or "").strip() or None
        file_val = raw.get("file") or None
        if doc_type and (doc_num or file_val):
            cleaned.append({"doc_type": doc_type, "doc_number": doc_num, "file": file_val})

    data["documents"] = cleaned
    if cleaned:
        first = cleaned[0]
        data.setdefault("id_proof_type", first["doc_type"])
        data["id_proof_type"] = first["doc_type"]
        data["id_proof_number"] = first["doc_number"]
        data["id_proof"] = first["file"]
    else:
        data["id_proof_type"] = None
        data["id_proof_number"] = None
        data["id_proof"] = None

    if sync_kyc:
        voter = next((d for d in cleaned if "voter" in d["doc_type"].lower()), None)
        pan = next((d for d in cleaned if "pan" in d["doc_type"].lower().split()), None)
        data["voter_number"] = voter["doc_number"] if voter else None
        data["voter_photo"] = voter["file"] if voter else None
        data["pan_number"] = pan["doc_number"] if pan else None
        data["pan_photo"] = pan["file"] if pan else None

    return data
