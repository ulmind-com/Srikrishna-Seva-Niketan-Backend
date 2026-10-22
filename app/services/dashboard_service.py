"""Dashboard analytics service (counts, recent activity, charts data)."""

from __future__ import annotations

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
from app.models.finance import Expense, Payment
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

        # Branch information for scoped session
        branch_names: List[str] = []
        if is_scoped:
            valid_ids = [ObjectId(x) for x in location_ids if ObjectId.is_valid(x)]
            locations = await Location.find({"_id": {"$in": valid_ids}}).to_list()
            branch_names = [loc.name for loc in locations]

        # Staff metrics (koto stuff ache, koto stuff kaj korche)
        total_staff = await Employee.find(scope).count()
        working_staff = await Employee.find({**scope, "status": "assigned"}).count()
        available_staff = await Employee.find({**scope, "status": "available"}).count()
        on_leave_staff = await Employee.find({**scope, "status": "on_leave"}).count()
        verified_staff = await Employee.find({**scope, "verification_status": "verified"}).count()
        pending_staff = await Employee.find({**scope, "verification_status": "pending"}).count()

        # Active assignments / shifts
        active_assignments = await Assignment.find(
            {**scope, "status": {"$in": ["scheduled", "active"]}}
        ).count()

        # Active customers
        active_customers = await Customer.find({**scope, "is_active": True}).count()

        # Revenue from paid/realised payments
        paid_payments = await Payment.find(
            {**scope, "status": {"$ne": "pending"}}
        ).to_list()
        total_revenue = sum(p.amount for p in paid_payments)

        paid_expenses = await Expense.find(scope).to_list()
        total_expense = sum(e.amount for e in paid_expenses)
        profit = total_revenue - total_expense

        # Organization-level counts
        (
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
        ) = (
            await Booking.find({}).count(),
            await Booking.find({"status": BookingStatus.PENDING}).count(),
            await Booking.find({"status": BookingStatus.APPROVED}).count(),
            await Booking.find({"status": BookingStatus.COMPLETED}).count(),
            await Service.find({}).count(),
            await Equipment.find({}).count(),
            await EquipmentRental.find({}).count(),
            await JobApplication.find({}).count(),
            await ContactMessage.find({"status": "new"}).count(),
            await Blog.find({}).count(),
        )

        # Per-branch rollup for Super Admin
        branches_summary: List[Dict[str, Any]] = []
        if not is_scoped:
            all_locs = await Location.find({"is_active": True}).sort("order").to_list()
            for loc in all_locs:
                loc_id = str(loc.id)
                loc_scope = {"location_id": loc_id}
                b_staff = await Employee.find(loc_scope).count()
                b_working = await Employee.find({**loc_scope, "status": "assigned"}).count()
                b_payments = await Payment.find(
                    {**loc_scope, "status": {"$ne": "pending"}}
                ).to_list()
                b_rev = sum(p.amount for p in b_payments)
                branches_summary.append({
                    "id": loc_id,
                    "name": loc.name,
                    "code": loc.code,
                    "city": loc.city,
                    "total_staff": b_staff,
                    "working_staff": b_working,
                    "revenue": b_rev,
                })

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
                "payment_count": len(paid_payments),
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
