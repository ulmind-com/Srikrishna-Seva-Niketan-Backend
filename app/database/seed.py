"""Idempotent database seeding: permissions, roles and the bootstrap admin."""

from __future__ import annotations

import datetime as dt

from app.config import settings
from app.core.logging import get_logger
from app.core.permissions import DEFAULT_ROLES, all_permission_codes
from app.core.security import hash_password
from app.models.employee import Employee
from app.models.location import Location
from app.models.rbac import Permission, Role
from app.models.user import User

logger = get_logger(__name__)


async def seed_permissions() -> None:
    """Ensure every catalogue permission exists as a document."""
    existing = {p.code for p in await Permission.find_all().to_list()}
    to_create = []
    for code in all_permission_codes():
        if code in existing:
            continue
        resource, action = code.split(":", 1)
        to_create.append(
            Permission(
                code=code,
                name=f"{action.capitalize()} {resource}",
                description=f"Allows the user to {action} {resource}",
                group=resource,
            )
        )
    if to_create:
        await Permission.insert_many(to_create)
        logger.info("Seeded permissions", extra={"count": len(to_create)})


async def seed_roles() -> None:
    """Ensure the default system roles exist."""
    for slug, spec in DEFAULT_ROLES.items():
        role = await Role.find_one({"slug": slug})
        if role is None:
            await Role(
                slug=slug,
                name=spec["name"],
                description=spec["description"],
                permissions=spec["permissions"],
                is_system=spec.get("is_system", False),
            ).insert()
            logger.info("Seeded role", extra={"role": slug})
        else:
            # Keep system-role permissions in sync with the catalogue.
            if role.is_system and role.permissions != spec["permissions"]:
                role.permissions = spec["permissions"]
                role.touch()
                await role.save()


async def seed_admin() -> None:
    """Create or update the bootstrap super-admin user."""
    email = settings.FIRST_ADMIN_EMAIL.lower().strip()
    existing = await User.find_one({"email": email})
    if existing is not None:
        # Update password if it has changed in env
        from app.core.security import verify_password

        if not verify_password(settings.FIRST_ADMIN_PASSWORD, existing.hashed_password):
            existing.hashed_password = hash_password(settings.FIRST_ADMIN_PASSWORD)
            existing.touch()
            await existing.save()
            logger.info("Updated bootstrap admin password", extra={"email": email})
        return
    await User(
        name=settings.FIRST_ADMIN_NAME,
        email=email,
        hashed_password=hash_password(settings.FIRST_ADMIN_PASSWORD),
        role="super_admin",
        is_active=True,
        is_superuser=True,
    ).insert()
    logger.info("Seeded bootstrap admin", extra={"email": email})


async def seed_locations() -> None:
    """Ensure standard Kolkata branches exist."""
    branches = [
        {"name": "Head Office", "code": "SSN-HO-0001", "city": "Kolkata", "order": 0},
        {"name": "Salt Lake Branch", "code": "SSN-SL-0002", "city": "Salt Lake", "order": 1},
        {"name": "New Town Branch", "code": "SSN-NT-0003", "city": "New Town", "order": 2},
        {"name": "Park Street Branch", "code": "SSN-PS-0004", "city": "Park Street", "order": 3},
    ]
    for b in branches:
        existing = await Location.find_one({"code": b["code"]})
        if existing is None:
            await Location(
                name=b["name"],
                code=b["code"],
                city=b["city"],
                is_active=True,
                order=b["order"],
            ).insert()
            logger.info("Seeded location", extra={"code": b["code"]})


async def seed_employees() -> None:
    """Ensure initial verified staff exist for assignment and testing."""
    if await Employee.find_one({}) is not None:
        return

    ho = await Location.find_one({"code": "SSN-HO-0001"})
    sl = await Location.find_one({"code": "SSN-SL-0002"}) or ho
    loc_id = str(ho.id) if ho else "ho"
    sl_id = str(sl.id) if sl else loc_id

    from app.models.enums import EmployeeStatus, Gender, StaffVerificationStatus

    pw_hash = hash_password("admin12345")
    sample_staff = [
        Employee(
            code="EMP-SSN-0001",
            name="Arnab Senapati",
            email="nurse@nupunhealth.com",
            phone="8537861040",
            category="nurse",
            designation="Senior Staff Nurse",
            hrms_code="HRMS-7701",
            gender=Gender.MALE,
            city="Kolkata",
            location_id=loc_id,
            status=EmployeeStatus.AVAILABLE,
            verification_status=StaffVerificationStatus.VERIFIED,
            hashed_password=pw_hash,
            auth_provider="password",
            verified_at=dt.datetime.now(dt.timezone.utc),
            join_date=dt.date.today(),
            daily_rate=500,
        ),
        Employee(
            code="EMP-SSN-0002",
            name="Rupa Dey",
            email="rupadey@nupunhealth.com",
            phone="9876543211",
            category="nurse",
            designation="Critical Care Specialist",
            hrms_code="HRMS-7702",
            gender=Gender.FEMALE,
            city="Salt Lake",
            location_id=sl_id,
            status=EmployeeStatus.AVAILABLE,
            verification_status=StaffVerificationStatus.VERIFIED,
            hashed_password=pw_hash,
            auth_provider="password",
            verified_at=dt.datetime.now(dt.timezone.utc),
            join_date=dt.date.today(),
            daily_rate=600,
        ),
        Employee(
            code="EMP-SSN-0003",
            name="Priya Mondal",
            email="priyamondal@nupunhealth.com",
            phone="9876543212",
            category="aaya",
            designation="General Duty Assistant (GDA)",
            hrms_code="HRMS-7703",
            gender=Gender.FEMALE,
            city="Kolkata",
            location_id=loc_id,
            status=EmployeeStatus.AVAILABLE,
            verification_status=StaffVerificationStatus.VERIFIED,
            hashed_password=pw_hash,
            auth_provider="password",
            verified_at=dt.datetime.now(dt.timezone.utc),
            join_date=dt.date.today(),
            daily_rate=400,
        ),
    ]
    for emp in sample_staff:
        await emp.insert()
        logger.info("Seeded employee", extra={"code": emp.code, "name": emp.name})


async def run_seed() -> None:
    """Run all seeders in order (safe to run on every startup)."""
    await seed_permissions()
    await seed_roles()
    await seed_admin()
    await seed_locations()
    await seed_employees()
