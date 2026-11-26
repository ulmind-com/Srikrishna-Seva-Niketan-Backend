"""Cascading deletion helpers so deleting a branch, staff, customer, or assignment
cleanly removes all dependent records across every module (assignments, attendance,
payslips, bills, payments, expenses, bookings, notifications, and reports).
"""

from __future__ import annotations

from typing import Iterable, List, Optional

from app.dependencies.auth import invalidate_user_cache
from app.dependencies.scoping import get_default_location_id, set_default_location_id
from app.dependencies.staff_auth import invalidate_staff_cache
from app.models.activity_log import ActivityLog
from app.models.assignment import Assignment
from app.models.attendance import AttendanceRecord, StaffLeave
from app.models.billing import MonthlyBill
from app.models.booking import Booking
from app.models.customer import Customer
from app.models.employee import Employee
from app.models.enums import AssignmentStatus, EmployeeStatus
from app.models.finance import Expense, Payment, StaffPayment
from app.models.location import Location
from app.models.notification import Notification
from app.models.push_subscription import PushSubscription
from app.models.user import User
from app.services.dashboard_service import invalidate_dashboard_cache


def _non_empty(values: Iterable[Optional[str]]) -> List[str]:
    return [v for v in values if v]


async def _delete_assignment_dependents(asgn_ids: List[str]) -> None:
    """Remove attendance, payments, and notifications linked to assignment ids."""
    if not asgn_ids:
        return
    await AttendanceRecord.get_motor_collection().delete_many(
        {"assignment_id": {"$in": asgn_ids}}
    )
    await StaffLeave.get_motor_collection().delete_many(
        {"assignment_id": {"$in": asgn_ids}}
    )
    await Payment.get_motor_collection().delete_many(
        {"assignment_id": {"$in": asgn_ids}}
    )
    await StaffPayment.get_motor_collection().delete_many(
        {"assignment_id": {"$in": asgn_ids}}
    )
    await Notification.get_motor_collection().delete_many(
        {"reference_id": {"$in": asgn_ids}}
    )


async def _refresh_employee_availability(employee_ids: Iterable[str]) -> None:
    """Mark employees AVAILABLE when they have no remaining occupying duties."""
    for emp_id in set(_non_empty(employee_ids)):
        has_active = await Assignment.find_one(
            {
                "employee_id": emp_id,
                "status": {
                    "$in": [
                        AssignmentStatus.SCHEDULED.value,
                        AssignmentStatus.ACTIVE.value,
                    ]
                },
            }
        )
        if not has_active:
            emp = await Employee.get(emp_id)
            if emp and emp.status == EmployeeStatus.ASSIGNED:
                emp.status = EmployeeStatus.AVAILABLE
                await emp.save()


async def cascade_delete_location(location_id: str) -> None:
    """Delete everything under a branch: staff, customers, duties, attendance,
    payslips, bills, expenses, payments, bookings, notifications, and logs.
    """
    emp_docs = await Employee.find({"location_id": location_id}).to_list()
    emp_ids = [str(e.id) for e in emp_docs]

    cust_docs = await Customer.find({"location_id": location_id}).to_list()
    cust_ids = [str(c.id) for c in cust_docs]

    asgn_filter: dict = {
        "$or": [
            {"location_id": location_id},
            *([{"employee_id": {"$in": emp_ids}}] if emp_ids else []),
            *([{"customer_id": {"$in": cust_ids}}] if cust_ids else []),
        ]
    }
    asgn_docs = await Assignment.find(asgn_filter).to_list()
    asgn_ids = [str(a.id) for a in asgn_docs]

    att_or = [{"location_id": location_id}]
    if emp_ids:
        att_or.append({"employee_id": {"$in": emp_ids}})
    if asgn_ids:
        att_or.append({"assignment_id": {"$in": asgn_ids}})
    await AttendanceRecord.get_motor_collection().delete_many({"$or": att_or})

    bill_or = [{"location_id": location_id}]
    if emp_ids:
        bill_or.append({"employee_id": {"$in": emp_ids}})
    if cust_ids:
        bill_or.append({"customer_id": {"$in": cust_ids}})
    await MonthlyBill.get_motor_collection().delete_many({"$or": bill_or})

    pay_or = [{"location_id": location_id}]
    if cust_ids:
        pay_or.append({"customer_id": {"$in": cust_ids}})
    if asgn_ids:
        pay_or.append({"assignment_id": {"$in": asgn_ids}})
    await Payment.get_motor_collection().delete_many({"$or": pay_or})

    sp_or = [{"location_id": location_id}]
    if emp_ids:
        sp_or.append({"employee_id": {"$in": emp_ids}})
    if asgn_ids:
        sp_or.append({"assignment_id": {"$in": asgn_ids}})
    await StaffPayment.get_motor_collection().delete_many({"$or": sp_or})

    leave_or = [{"location_id": location_id}]
    if emp_ids:
        leave_or.append({"employee_id": {"$in": emp_ids}})
    if asgn_ids:
        leave_or.append({"assignment_id": {"$in": asgn_ids}})
    await StaffLeave.get_motor_collection().delete_many({"$or": leave_or})

    await Expense.get_motor_collection().delete_many({"location_id": location_id})
    await Assignment.get_motor_collection().delete_many(asgn_filter)

    booking_or = [{"location_id": location_id}]
    if cust_ids:
        booking_or.append({"customer_id": {"$in": cust_ids}})
    if emp_ids:
        booking_or.append({"assigned_staff_id": {"$in": emp_ids}})
    await Booking.get_motor_collection().delete_many({"$or": booking_or})

    ref_ids = [location_id, *emp_ids, *cust_ids, *asgn_ids]
    notif_or = [{"reference_id": {"$in": ref_ids}}]
    if emp_ids:
        notif_or.append({"employee_id": {"$in": emp_ids}})
    await Notification.get_motor_collection().delete_many({"$or": notif_or})

    if emp_ids:
        await PushSubscription.get_motor_collection().delete_many(
            {"employee_id": {"$in": emp_ids}}
        )

    await Employee.get_motor_collection().delete_many({"location_id": location_id})
    await Customer.get_motor_collection().delete_many({"location_id": location_id})

    await ActivityLog.get_motor_collection().delete_many(
        {
            "$or": [
                {"entity_id": {"$in": ref_ids}},
                {"metadata.location_id": location_id},
            ]
        }
    )

    await User.get_motor_collection().update_many(
        {"location_ids": location_id},
        {"$pull": {"location_ids": location_id}},
    )

    next_loc = await Location.find_one({"is_active": True}) or await Location.find_one({})
    next_id = str(next_loc.id) if next_loc else ""
    if get_default_location_id() == location_id:
        set_default_location_id(next_id)

    invalidate_staff_cache()
    invalidate_user_cache()
    invalidate_dashboard_cache()


async def cascade_delete_employee(employee_id: str) -> None:
    """Delete all duties, attendance, payslips, bills, notifications, and logs for an employee."""
    asgn_docs = await Assignment.find({"employee_id": employee_id}).to_list()
    asgn_ids = [str(a.id) for a in asgn_docs]

    await _delete_assignment_dependents(asgn_ids)

    await AttendanceRecord.get_motor_collection().delete_many({"employee_id": employee_id})
    await StaffLeave.get_motor_collection().delete_many({"employee_id": employee_id})
    await StaffPayment.get_motor_collection().delete_many({"employee_id": employee_id})
    await MonthlyBill.get_motor_collection().delete_many({"employee_id": employee_id})
    await Assignment.get_motor_collection().delete_many({"employee_id": employee_id})

    await Assignment.get_motor_collection().update_many(
        {"replacement_for_employee_id": employee_id},
        {"$set": {"replacement_for_employee_id": None}},
    )
    await Assignment.get_motor_collection().update_many(
        {"replaced_by_employee_id": employee_id},
        {"$set": {"replaced_by_employee_id": None}},
    )

    await Notification.get_motor_collection().delete_many(
        {"$or": [{"employee_id": employee_id}, {"reference_id": employee_id}]}
    )
    await PushSubscription.get_motor_collection().delete_many({"employee_id": employee_id})
    await Booking.get_motor_collection().update_many(
        {"assigned_staff_id": employee_id},
        {
            "$set": {
                "assigned_staff_id": None,
                "assigned_staff_name": None,
                "staff_acceptance_status": None,
            }
        },
    )
    await ActivityLog.get_motor_collection().delete_many(
        {"entity_id": {"$in": [employee_id, *asgn_ids]}}
    )

    invalidate_staff_cache(employee_id)
    invalidate_dashboard_cache()


async def cascade_delete_customer(customer_id: str) -> None:
    """Delete all duties, attendance, bills, payments, and logs for a customer."""
    asgn_docs = await Assignment.find({"customer_id": customer_id}).to_list()
    asgn_ids = [str(a.id) for a in asgn_docs]
    affected_emps = [a.employee_id for a in asgn_docs]

    await _delete_assignment_dependents(asgn_ids)
    await Payment.get_motor_collection().delete_many({"customer_id": customer_id})
    await MonthlyBill.get_motor_collection().delete_many({"customer_id": customer_id})
    await Notification.get_motor_collection().delete_many({"reference_id": customer_id})
    await Assignment.get_motor_collection().delete_many({"customer_id": customer_id})
    await Booking.get_motor_collection().update_many(
        {"customer_id": customer_id},
        {"$set": {"customer_id": None}},
    )
    await ActivityLog.get_motor_collection().delete_many(
        {"entity_id": {"$in": [customer_id, *asgn_ids]}}
    )

    await _refresh_employee_availability(affected_emps)
    invalidate_dashboard_cache()


async def cascade_delete_assignment(assignment_id: str, employee_id: Optional[str] = None) -> None:
    """Delete attendance, payments, notifications, and logs tied to an assignment."""
    await _delete_assignment_dependents([assignment_id])
    await ActivityLog.get_motor_collection().delete_many({"entity_id": assignment_id})
    if employee_id:
        await _refresh_employee_availability([employee_id])
    invalidate_dashboard_cache()
