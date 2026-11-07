"""Monthly customer bill — one per (nurse + customer) per calendar month.

A bill number is allocated the first time a monthly bill is issued for a given
nurse/customer/month and kept thereafter, so re-printing shows the same number.
The amount itself is computed live from the assignments in that month; only the
allocated number (and a small snapshot) is persisted here.
"""

from __future__ import annotations

from typing import Optional

import pymongo
from beanie import Indexed

from app.models.base import TimestampedDocument


class MonthlyBill(TimestampedDocument):
    # Stable composite key: "<employee_id>:<customer_id>:<YYYY-MM>".
    key: Indexed(str, unique=True)  # type: ignore[valid-type]
    employee_id: str
    customer_id: str
    location_id: Optional[str] = None
    month: str  # "YYYY-MM"
    bill_no: int
    # Snapshot of what was billed when the number was issued (for audit).
    total_days: Optional[int] = None
    total_amount: Optional[int] = None

    class Settings:
        name = "monthly_bills"
        indexes = [
            [("month", pymongo.ASCENDING)],
            [("employee_id", pymongo.ASCENDING)],
            [("customer_id", pymongo.ASCENDING)],
        ]
