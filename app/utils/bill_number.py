"""Sequential invoice / bill numbering starting from 001 (1, 2, 3...)."""

from __future__ import annotations

from typing import Optional

from pymongo import ReturnDocument


def format_bill_no(bill_no: Optional[int]) -> Optional[str]:
    """Format numeric bill number into zero-padded 3-digit string ('001', '002', ...)."""
    if bill_no is None:
        return None
    return f"{int(bill_no):03d}"


async def next_sequential_bill_no(db, counter_kind: str) -> int:
    """Allocate sequential 1-based bill number ('monthly' or 'assignment').

    Cleanly resets any legacy >= 1000 hardcoded bill numbers once per live DB
    (identified by legacy counter presence) so the new numbering starts at 001.
    """
    legacy_id = "monthly_bill_no" if counter_kind == "monthly" else "assignment_bill_no"
    v2_id = "monthly_bill_seq_v2" if counter_kind == "monthly" else "assignment_bill_seq_v2"

    v2_doc = await db["counters"].find_one({"_id": v2_id})
    if v2_doc is None:
        legacy_doc = await db["counters"].find_one({"_id": legacy_id})
        if legacy_doc is not None:
            if counter_kind == "monthly":
                await db["monthly_bills"].delete_many({"bill_no": {"$gte": 1000}})
            else:
                await db["assignments"].update_many(
                    {"bill_no": {"$gte": 1000}},
                    {"$set": {"bill_no": None}},
                )

    res = await db["counters"].find_one_and_update(
        {"_id": v2_id},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return int(res["seq"])
