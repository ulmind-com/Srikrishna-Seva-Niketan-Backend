"""Dashboard analytics service (counts, recent activity, charts data)."""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any, Dict, List, Optional

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

        scope: Dict[str, Any] = {"location_id": {"$in": location_ids}} if is_scoped else {}

        # Branch-name lookup (scoped) and the active-location list (super admin)
        # are wrapped so they can be fired alongside every other query below.
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

        # Every query here is independent, so fire them concurrently on the
        # connection pool instead of awaiting ~20 round-trips one by one. With a
        # distant DB this is the difference between ~15s and ~1s per load.
        (
            branch_names,
            total_staff,
            working_staff,
            available_staff,
            on_leave_staff,
            verified_staff,
            pending_staff,
            active_assignments,
            active_customers,
            all_assignments,
            paid_expenses,
            total_bookings,
            pending_bookings,
            approved_bookings,
            completed_bookings,
            total_services,
            total_equipment,
            total_rentals,
            total_applications,
            new_contacts,
            total_blogs,
            all_locs,
        ) = await asyncio.gather(
            _branch_names(),
            Employee.find(scope).count(),
            Employee.find({**scope, "status": "assigned"}).count(),
            Employee.find({**scope, "status": "available"}).count(),
            Employee.find({**scope, "status": "on_leave"}).count(),
            Employee.find({**scope, "verification_status": "verified"}).count(),
            Employee.find({**scope, "verification_status": "pending"}).count(),
            Assignment.find({**scope, "status": {"$in": ["scheduled", "active"]}}).count(),
            Customer.find({**scope, "is_active": True}).count(),
            # Revenue & expense — assignment-driven, matching the P&L report:
            # contract amounts in, staff payout + other engagement cost + the
            # standalone expense ledger out.
            Assignment.find({**scope, "status": {"$ne": "cancelled"}}).to_list(),
            Expense.find(scope).to_list(),
            Booking.find({}).count(),
            Booking.find({"status": BookingStatus.PENDING}).count(),
            Booking.find({"status": BookingStatus.APPROVED}).count(),
            Booking.find({"status": BookingStatus.COMPLETED}).count(),
            Service.find({}).count(),
            Equipment.find({}).count(),
            EquipmentRental.find({}).count(),
            JobApplication.find({}).count(),
            ContactMessage.find({"status": "new"}).count(),
            Blog.find({}).count(),
            _all_locs(),
        )

        total_revenue = sum(a.customer_rate or 0 for a in all_assignments)
        assignment_cost = sum(
            (a.employee_payout or 0) + (a.other_expense or 0)
            for a in all_assignments
        )
        total_expense = assignment_cost + sum(e.amount for e in paid_expenses)
        profit = total_revenue - total_expense
        payment_count = len(all_assignments)

        # Per-branch rollup for Super Admin — each branch's 3 queries run
        # concurrently, and all branches are rolled up in parallel too (no N+1
        # serial loop).
        branches_summary: List[Dict[str, Any]] = []
        if not is_scoped and all_locs:
            async def _branch_row(loc: Any) -> Dict[str, Any]:
                loc_id = str(loc.id)
                loc_scope = {"location_id": loc_id}
                b_staff, b_working, b_assignments = await asyncio.gather(
                    Employee.find(loc_scope).count(),
                    Employee.find({**loc_scope, "status": "assigned"}).count(),
                    Assignment.find(
                        {**loc_scope, "status": {"$ne": "cancelled"}}
                    ).to_list(),
                )
                return {
                    "id": loc_id,
                    "name": loc.name,
                    "code": loc.code,
                    "city": loc.city,
                    "total_staff": b_staff,
                    "working_staff": b_working,
                    "revenue": sum(a.customer_rate or 0 for a in b_assignments),
                }

            branches_summary = list(
                await asyncio.gather(*[_branch_row(loc) for loc in all_locs])
            )

        return {
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
