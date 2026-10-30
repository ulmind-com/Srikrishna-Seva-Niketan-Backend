"""Dashboard analytics service (counts, recent activity, charts data)."""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from typing import Any, Dict, List, Optional, Tuple

from bson import ObjectId
from app.models.assignment import Assignment
from app.models.blog import Blog
from app.models.booking import Booking
from app.models.career import JobApplication
from app.models.contact import ContactMessage
from app.models.customer import Customer
from app.models.employee import Employee
from app.models.enums import BookingStatus
from app.models.equipment import Equipment, EquipmentRental
from app.models.finance import Expense
from app.models.location import Location
from app.models.service import Service

# In-memory short-lived cache so dashboard polling and concurrent widget calls
# return in <1ms without hitting the database repeatedly.
_STATS_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_STATS_CACHE_TTL = 8.0  # seconds


class DashboardService:
    """Aggregate cross-collection statistics for the admin dashboard."""

    async def stats(self, actor: Optional[Any] = None) -> Dict[str, Any]:
        """Return top-line counts used by dashboard stat cards.

        Automatically location-scoped: branch managers receive metrics for their
        assigned branches (staff total, staff on duty/working, and revenue), while
        super admins see global organization totals plus a per-branch rollup.
        """
        is_scoped = bool(actor and getattr(actor, "is_scoped", False))
        location_ids = list(getattr(actor, "location_ids", [])) if is_scoped else []

        cache_key = f"{is_scoped}:{sorted(location_ids)}"
        now = time.time()
        if cache_key in _STATS_CACHE:
            cached_time, cached_data = _STATS_CACHE[cache_key]
            if now - cached_time < _STATS_CACHE_TTL:
                return cached_data

        scope: Dict[str, Any] = {"location_id": {"$in": location_ids}} if is_scoped else {}

        async def _branch_names() -> List[str]:
            if not is_scoped:
                return []
            valid_ids = [ObjectId(x) for x in location_ids if ObjectId.is_valid(x)]
            locations = await Location.find({"_id": {"$in": valid_ids}}).to_list()
            return [loc.name for loc in locations]

        async def _all_locs() -> List[Any]:
            if is_scoped:
                return []
            return await Location.find({"is_active": True}).sort("order").to_list()

        # Ultra-fast MongoDB aggregation pipelines: summarize collections server-side
        # in a single query rather than running dozens of round-trips.
        emp_pipeline = [
            {"$match": scope},
            {
                "$group": {
                    "_id": None,
                    "total": {"$sum": 1},
                    "working": {"$sum": {"$cond": [{"$eq": ["$status", "assigned"]}, 1, 0]}},
                    "available": {"$sum": {"$cond": [{"$eq": ["$status", "available"]}, 1, 0]}},
                    "on_leave": {"$sum": {"$cond": [{"$eq": ["$status", "on_leave"]}, 1, 0]}},
                    "verified": {"$sum": {"$cond": [{"$eq": ["$verification_status", "verified"]}, 1, 0]}},
                    "pending": {"$sum": {"$cond": [{"$eq": ["$verification_status", "pending"]}, 1, 0]}},
                }
            },
        ]

        booking_pipeline = [
            {
                "$group": {
                    "_id": None,
                    "total": {"$sum": 1},
                    "pending": {"$sum": {"$cond": [{"$eq": ["$status", "pending"]}, 1, 0]}},
                    "approved": {"$sum": {"$cond": [{"$eq": ["$status", "approved"]}, 1, 0]}},
                    "completed": {"$sum": {"$cond": [{"$eq": ["$status", "completed"]}, 1, 0]}},
                }
            }
        ]

        assign_pipeline = [
            {"$match": {**scope, "status": {"$ne": "cancelled"}}},
            {
                "$group": {
                    "_id": None,
                    "revenue": {"$sum": "$customer_rate"},
                    "payout": {"$sum": "$employee_payout"},
                    "other_expense": {"$sum": "$other_expense"},
                    "count": {"$sum": 1},
                }
            },
        ]

        exp_pipeline = [
            {"$match": scope},
            {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
        ]

        branch_emp_pipeline = [
            {
                "$group": {
                    "_id": "$location_id",
                    "total": {"$sum": 1},
                    "working": {"$sum": {"$cond": [{"$eq": ["$status", "assigned"]}, 1, 0]}},
                }
            }
        ]

        branch_assign_pipeline = [
            {"$match": {"status": {"$ne": "cancelled"}}},
            {"$group": {"_id": "$location_id", "revenue": {"$sum": "$customer_rate"}}},
        ]

        (
            branch_names,
            all_locs,
            emp_agg,
            booking_agg,
            assign_agg,
            exp_agg,
            branch_emp_agg,
            branch_assign_agg,
            active_assignments,
            active_customers,
            total_services,
            total_equipment,
            total_rentals,
            total_applications,
            new_contacts,
            total_blogs,
        ) = await asyncio.gather(
            _branch_names(),
            _all_locs(),
            Employee.aggregate(emp_pipeline).to_list(),
            Booking.aggregate(booking_pipeline).to_list(),
            Assignment.aggregate(assign_pipeline).to_list(),
            Expense.aggregate(exp_pipeline).to_list(),
            Employee.aggregate(branch_emp_pipeline).to_list() if not is_scoped else asyncio.sleep(0, result=[]),
            Assignment.aggregate(branch_assign_pipeline).to_list() if not is_scoped else asyncio.sleep(0, result=[]),
            Assignment.find({**scope, "status": {"$in": ["scheduled", "active"]}}).count(),
            Customer.find({**scope, "is_active": True}).count(),
            Service.find({}).count(),
            Equipment.find({}).count(),
            EquipmentRental.find({}).count(),
            JobApplication.find({}).count(),
            ContactMessage.find({"status": "new"}).count(),
            Blog.find({}).count(),
        )

        emp_data = emp_agg[0] if emp_agg else {}
        total_staff = emp_data.get("total", 0)
        working_staff = emp_data.get("working", 0)
        available_staff = emp_data.get("available", 0)
        on_leave_staff = emp_data.get("on_leave", 0)
        verified_staff = emp_data.get("verified", 0)
        pending_staff = emp_data.get("pending", 0)

        book_data = booking_agg[0] if booking_agg else {}
        total_bookings = book_data.get("total", 0)
        pending_bookings = book_data.get("pending", 0)
        approved_bookings = book_data.get("approved", 0)
        completed_bookings = book_data.get("completed", 0)

        assign_data = assign_agg[0] if assign_agg else {}
        total_revenue = assign_data.get("revenue", 0)
        assignment_cost = assign_data.get("payout", 0) + assign_data.get("other_expense", 0)
        payment_count = assign_data.get("count", 0)

        total_expense = assignment_cost + (exp_agg[0]["total"] if exp_agg else 0)
        profit = total_revenue - total_expense

        # Map branch summaries in O(1) from aggregation dictionaries
        branches_summary: List[Dict[str, Any]] = []
        if not is_scoped and all_locs:
            b_emp_map = {str(r["_id"]): r for r in branch_emp_agg if r.get("_id")}
            b_rev_map = {str(r["_id"]): r.get("revenue", 0) for r in branch_assign_agg if r.get("_id")}
            for loc in all_locs:
                loc_id = str(loc.id)
                emp_info = b_emp_map.get(loc_id, {})
                branches_summary.append({
                    "id": loc_id,
                    "name": loc.name,
                    "code": loc.code,
                    "city": loc.city,
                    "total_staff": emp_info.get("total", 0),
                    "working_staff": emp_info.get("working", 0),
                    "revenue": b_rev_map.get(loc_id, 0),
                })

        result_data = {
            "is_scoped": is_scoped,
            "branch_names": branch_names,
            "branch_ids": location_ids,
            "staff": {
                "total": total_staff,
                "working": working_staff,
                "available": available_staff,
                "on_leave": on_leave_staff,
                "verified": verified_staff,
                "pending": pending_staff,
            },
            "revenue": {
                "currency": "INR",
                "total": total_revenue,
                "expense": total_expense,
                "profit": profit,
                "payment_count": payment_count,
            },
            "assignments": {
                "active": active_assignments,
            },
            "customers": {
                "active": active_customers,
            },
            "bookings": {
                "total": total_bookings,
                "pending": pending_bookings,
                "approved": approved_bookings,
                "completed": completed_bookings,
            },
            "services": total_services,
            "equipment": total_equipment,
            "rentals": total_rentals,
            "applications": total_applications,
            "new_contacts": new_contacts,
            "blogs": total_blogs,
            "branches_summary": branches_summary,
        }
        _STATS_CACHE[cache_key] = (now, result_data)
        return result_data

    async def booking_status_breakdown(self) -> Dict[str, int]:
        """Return a count per booking status (for a pie/donut chart)."""
        pipeline = [{"$group": {"_id": "$status", "count": {"$sum": 1}}}]
        result = await Booking.aggregate(pipeline).to_list()
        breakdown = {status.value: 0 for status in BookingStatus}
        for row in result:
            breakdown[row["_id"]] = row["count"]
        return breakdown

    async def bookings_trend(self, days: int = 30) -> List[Dict[str, Any]]:
        """Return a daily booking count for the last ``days`` days."""
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
        pipeline = [
            {"$match": {"created_at": {"$gte": since}}},
            {
                "$group": {
                    "_id": {
                        "$dateToString": {"format": "%Y-%m-%d", "date": "$created_at"}
                    },
                    "count": {"$sum": 1},
                }
            },
            {"$sort": {"_id": 1}},
        ]
        result = await Booking.aggregate(pipeline).to_list()
        return [{"date": row["_id"], "count": row["count"]} for row in result]

    async def recent_bookings(self, limit: int = 5) -> List[Booking]:
        return await Booking.find({}).sort([("created_at", -1)]).limit(limit).to_list()

    async def recent_contacts(self, limit: int = 5) -> List[ContactMessage]:
        return (
            await ContactMessage.find({}).sort([("created_at", -1)]).limit(limit).to_list()
        )

    async def recent_applications(self, limit: int = 5) -> List[JobApplication]:
        return (
            await JobApplication.find({}).sort([("created_at", -1)]).limit(limit).to_list()
        )


dashboard_service = DashboardService()
