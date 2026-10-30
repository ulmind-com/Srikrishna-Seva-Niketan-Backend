"""Booking business logic: creation, workflow transitions and export."""

from __future__ import annotations

import csv
import io
import re
import secrets
import string
from typing import Any, Dict, List, Optional, Tuple

from app.core.exceptions import BadRequestException, ForbiddenException, NotFoundException
from app.dependencies.auth import ActorContext
from app.dependencies.scoping import resolve_create_location
from app.models.booking import Booking
from app.models.customer import Customer
from app.models.location import Location
from app.models.enums import ActivityAction, BookingStatus, CareType, NotificationType
from app.repositories.base import BaseRepository
from app.services.activity_service import activity_service
from app.services.geo_service import geo_service
from app.services.notification_service import notification_service
from app.utils.references import generate_reference


async def _resolve_booking_location_id(
    city: Optional[str], address: Optional[str] = None
) -> Optional[str]:
    """Attempt to resolve a branch location_id based on booking city or address."""
    locations = await Location.find({"is_active": True}).to_list()
    if not locations:
        return None

    # 1. Exact match on city
    if city and city.strip():
        city_clean = city.strip().lower()
        for loc in locations:
            if loc.city and loc.city.strip().lower() == city_clean:
                return str(loc.id)
            if loc.name and loc.name.strip().lower() == city_clean:
                return str(loc.id)

    # 2. Substring match on city or location name
    if city and city.strip():
        city_clean = city.strip().lower()
        for loc in locations:
            if loc.city and (city_clean in loc.city.strip().lower() or loc.city.strip().lower() in city_clean):
                return str(loc.id)
            if loc.name and (city_clean in loc.name.strip().lower() or loc.name.strip().lower() in city_clean):
                return str(loc.id)

    # 3. Match address if address mentions any location name or city
    if address and address.strip():
        addr_clean = address.lower()
        for loc in locations:
            if loc.city and loc.city.strip().lower() in addr_clean:
                return str(loc.id)
            if loc.name and loc.name.strip().lower() in addr_clean:
                return str(loc.id)

    return None


class BookingService:
    """Coordinates the booking lifecycle."""

    def __init__(self) -> None:
        self.repo: BaseRepository[Booking] = BaseRepository(Booking)
        self.repo.search_fields = (
            "reference",
            "patient_name",
            "contact_phone",
            "service_name",
        )

    async def create(self, data: Dict[str, Any]) -> Booking:
        """Create a booking from a public submission (no auth)."""
        if not data.get("location_id"):
            loc_id = await _resolve_booking_location_id(data.get("city"), data.get("address"))
            if loc_id:
                data["location_id"] = loc_id

        booking = Booking(reference=generate_reference("NHB"), **data)
        await self.repo.create(booking)

        branch_note = f" ({booking.city})" if booking.city else ""
        await notification_service.create(
            title="New booking received",
            message=f"{booking.patient_name} booked {booking.service_name}{branch_note}",
            type=NotificationType.BOOKING,
            reference_id=str(booking.id),
        )
        return booking

    async def get_or_404(
        self, booking_id: str, actor: Optional[ActorContext] = None
    ) -> Booking:
        booking = await self.repo.get(booking_id)
        if booking is None:
            raise NotFoundException("Booking not found")

        if actor and actor.is_scoped:
            if booking.location_id and booking.location_id in actor.location_ids:
                return booking
            # Also allow if city matches actor branch
            if booking.city:
                branch_locs = await Location.find({"is_active": True}).to_list()
                accessible_cities = {
                    loc.city.strip().lower()
                    for loc in branch_locs
                    if str(loc.id) in actor.location_ids and loc.city
                } | {
                    loc.name.strip().lower()
                    for loc in branch_locs
                    if str(loc.id) in actor.location_ids and loc.name
                }
                if booking.city.strip().lower() in accessible_cities:
                    return booking
            raise NotFoundException("Booking not found")

        return booking

    async def update(
        self, booking_id: str, data: Dict[str, Any], actor: ActorContext
    ) -> Booking:
        booking = await self.get_or_404(booking_id, actor)
        await self.repo.update(booking, data)
        await self._audit(ActivityAction.UPDATE, booking, actor, "Updated booking")
        return booking

    async def change_status(
        self,
        booking_id: str,
        status: BookingStatus,
        actor: ActorContext,
        reason: Optional[str] = None,
    ) -> Booking:
        """Transition a booking to a new status with validation."""
        booking = await self.get_or_404(booking_id, actor)

        booking.status = status
        if reason:
            note = f"[{status}] {reason}"
            booking.admin_notes = (
                f"{booking.admin_notes}\n{note}" if booking.admin_notes else note
            )
        booking.touch()
        await booking.save()

        if booking.status != BookingStatus.PENDING:
            await notification_service.mark_read_by_reference(str(booking.id))

        action_map = {
            BookingStatus.APPROVED: ActivityAction.APPROVE,
            BookingStatus.REJECTED: ActivityAction.REJECT,
            BookingStatus.ASSIGNED: ActivityAction.UPDATE,
            BookingStatus.CANCELLED: ActivityAction.UPDATE,
        }
        await self._audit(
            action_map.get(status, ActivityAction.UPDATE),
            booking, actor, f"Booking {status}",
        )
        return booking

    async def assign_staff(
        self, booking_id: str, staff_id: str, staff_name: str, actor: ActorContext
    ) -> Booking:
        booking = await self.get_or_404(booking_id, actor)
        booking.assigned_staff_id = staff_id
        booking.assigned_staff_name = staff_name
        booking.status = BookingStatus.ASSIGNED
        booking.touch()
        await booking.save()
        asyncio.create_task(notification_service.mark_read_by_reference(str(booking.id)))
        asyncio.create_task(
            self._audit(ActivityAction.UPDATE, booking, actor, f"Assigned {staff_name}")
        )
        return booking

    async def delete(self, booking_id: str, actor: ActorContext) -> None:
        booking = await self.get_or_404(booking_id, actor)
        await self.repo.delete(booking)
        await self._audit(ActivityAction.DELETE, booking, actor, "Deleted booking")

    async def paginate(
        self,
        page: int,
        page_size: int,
        search: Optional[str] = None,
        sort_by: Optional[str] = None,
        sort_order: int = -1,
        status: Optional[str] = None,
        service_id: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        source: Optional[str] = None,
        actor: Optional[ActorContext] = None,
        location_id: Optional[str] = None,
    ) -> Tuple[List[Booking], int]:
        filters: Dict[str, Any] = {}
        if status:
            filters["status"] = status
        if service_id:
            filters["service_id"] = service_id
        if source:
            filters["source"] = source
        date_filter: Dict[str, Any] = {}
        if date_from:
            date_filter["$gte"] = date_from
        if date_to:
            date_filter["$lte"] = date_to
        if date_filter:
            filters["preferred_date"] = date_filter

        # Scoping logic
        if actor and actor.is_scoped:
            loc_ids = actor.location_ids
            all_locs = await Location.find({"is_active": True}).to_list()
            accessible_names: List[str] = []
            for loc in all_locs:
                if str(loc.id) in loc_ids:
                    if loc.city:
                        accessible_names.append(loc.city.strip())
                    if loc.name:
                        accessible_names.append(loc.name.strip())

            or_clauses: List[Dict[str, Any]] = [{"location_id": {"$in": loc_ids}}]
            for nm in set(accessible_names):
                or_clauses.append({"city": {"$regex": f"^{re.escape(nm)}$", "$options": "i"}})
            filters["$or"] = or_clauses
        elif location_id:
            filters["location_id"] = location_id

        return await self.repo.paginate(
            filters=filters or None, page=page, page_size=page_size,
            search=search, sort_by=sort_by or "created_at", sort_order=sort_order,
        )

    async def export_csv(
        self,
        status: Optional[str] = None,
        source: Optional[str] = None,
        actor: Optional[ActorContext] = None,
        location_id: Optional[str] = None,
    ) -> str:
        """Export bookings as a CSV string."""
        filters: Dict[str, Any] = {}
        if status:
            filters["status"] = status
        if source:
            filters["source"] = source

        if actor and actor.is_scoped:
            loc_ids = actor.location_ids
            all_locs = await Location.find({"is_active": True}).to_list()
            accessible_names: List[str] = []
            for loc in all_locs:
                if str(loc.id) in loc_ids:
                    if loc.city:
                        accessible_names.append(loc.city.strip())
                    if loc.name:
                        accessible_names.append(loc.name.strip())

            or_clauses: List[Dict[str, Any]] = [{"location_id": {"$in": loc_ids}}]
            for nm in set(accessible_names):
                or_clauses.append({"city": {"$regex": f"^{re.escape(nm)}$", "$options": "i"}})
            filters["$or"] = or_clauses
        elif location_id:
            filters["location_id"] = location_id

        bookings = await self.repo.list(
            filters=filters, sort=[("created_at", -1)], limit=5000
        )
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            ["Reference", "Patient", "Phone", "Service", "Preferred Date",
             "Status", "City", "Created At"]
        )
        for b in bookings:
            writer.writerow(
                [b.reference, b.patient_name, b.contact_phone, b.service_name,
                 b.preferred_date, b.status, b.city or "", b.created_at.isoformat()]
            )
        return buffer.getvalue()

    async def _audit(
        self, action: ActivityAction, booking: Booking, actor: ActorContext, desc: str
    ) -> None:
        await activity_service.log(
            action, "bookings", user_id=actor.user_id, user_email=actor.email,
            entity_id=str(booking.id), description=desc,
            ip_address=actor.ip_address, user_agent=actor.user_agent,
        )

    async def convert_to_customer(
        self, booking_id: str, actor: ActorContext, location_id: Optional[str] = None
    ) -> Tuple[Customer, Booking]:
        """Convert a service booking into a formal Customer record for nurse assignment."""
        booking = await self.get_or_404(booking_id)

        customer = None
        if booking.customer_id:
            customer = await Customer.get(booking.customer_id)

        if customer is None:
            # Check by phone to prevent duplicates
            customer = await Customer.find_one({"phone": booking.contact_phone})

        if customer is None:
            resolved_loc: Optional[str] = None
            if location_id:
                resolved_loc = resolve_create_location(actor, location_id)
            elif actor.is_scoped and actor.location_ids:
                resolved_loc = actor.location_ids[0]
            elif booking.location_id:
                resolved_loc = booking.location_id
            else:
                if booking.city:
                    match_loc = await Location.find_one(
                        {"city": {"$regex": f"^{re.escape(booking.city)}$", "$options": "i"}, "is_active": True}
                    )
                    if match_loc:
                        resolved_loc = str(match_loc.id)
                if not resolved_loc:
                    first_loc = await Location.find_one({"is_active": True})
                    if first_loc:
                        resolved_loc = str(first_loc.id)
                if not resolved_loc:
                    resolved_loc = resolve_create_location(actor, location_id)

            code = await _unique_customer_code()

            lat = None
            lng = None
            if booking.address:
                coords = await geo_service.geocode(booking.address)
                if coords:
                    lat, lng = coords

            customer = Customer(
                code=code,
                name=booking.patient_name,
                phone=booking.contact_phone,
                alt_phone=booking.whatsapp_number or booking.emergency_contact_phone,
                address=booking.address,
                city=booking.city,
                pincode=booking.pincode,
                lat=lat,
                lng=lng,
                care_type=CareType.HOME,
                service_label=booking.service_name,
                location_id=resolved_loc,
                notes=f"Converted from Booking #{booking.reference}. Care required: {booking.care_required or 'N/A'}",
                is_active=True,
            )
            await customer.insert()
            await activity_service.log(
                ActivityAction.CREATE,
                "customers",
                user_id=actor.user_id,
                user_email=actor.email,
                entity_id=str(customer.id),
                description=f"Created customer {customer.name} ({customer.code}) from booking {booking.reference}",
                ip_address=actor.ip_address,
                user_agent=actor.user_agent,
            )

        booking.customer_id = str(customer.id)
        if booking.status == BookingStatus.PENDING:
            booking.status = BookingStatus.APPROVED
        booking.touch()
        await booking.save()

        await self._audit(
            ActivityAction.UPDATE,
            booking,
            actor,
            f"Linked booking to customer {customer.code}",
        )

        return customer, booking


_ALPHABET = string.ascii_uppercase + string.digits


async def _unique_customer_code() -> str:
    for _ in range(10):
        code = "CUST-" + "".join(secrets.choice(_ALPHABET) for _ in range(6))
        if await Customer.find_one({"code": code}) is None:
            return code
    return "CUST-" + secrets.token_hex(4).upper()


booking_service = BookingService()
