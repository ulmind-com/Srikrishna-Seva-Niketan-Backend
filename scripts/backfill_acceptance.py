"""One-off backfill: mark pre-existing assignments as accepted.

The acceptance lifecycle is new, so old assignments have no ``acceptance_status``
(or default to ``pending``). Without this, every historical active shift would
ring in the staff portal. Run once, after deploying the feature, before nurses
open the portal:

    python scripts/backfill_acceptance.py
"""

from __future__ import annotations

import asyncio

from app.models.assignment import Assignment
from app.models.enums import AcceptanceStatus


async def backfill() -> int:
    """Set accepted on every assignment still pending or missing the field."""
    result = await Assignment.find(
        {
            "$or": [
                {"acceptance_status": AcceptanceStatus.PENDING.value},
                {"acceptance_status": {"$exists": False}},
            ]
        }
    ).update({"$set": {"acceptance_status": AcceptanceStatus.ACCEPTED.value}})
    return getattr(result, "modified_count", 0)


async def _main() -> None:
    from app.database.connection import connect_to_mongo

    await connect_to_mongo()
    count = await backfill()
    print(f"Backfilled {count} assignment(s) to accepted.")


if __name__ == "__main__":
    asyncio.run(_main())
