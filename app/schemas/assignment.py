"""Schemas for assignments (staff-to-customer daily shifts)."""

from __future__ import annotations

import datetime as dt
from typing import Any, List, Optional, Self

from pydantic import BaseModel, Field, computed_field, field_validator, model_validator

from app.models.assignment import DutyBreak, ShiftWindow
from app.models.enums import (
    AcceptanceStatus,
    AssignmentPlace,
    AssignmentStatus,
    PaymentStatus,
    PaymentType,
    ShiftType,
)
from app.schemas.common import IdTimestampSchema


class DutyBreakCreate(BaseModel):
    """Payload to add a temporary pause / duty break."""

    start_date: dt.date
    end_date: dt.date
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    reason: Optional[str] = "Customer request / staff leave"

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def validate_dates(cls, v):
        return _parse_flexible_date(v)


class DutyBreakResponse(BaseModel):
    """Duty break record representation."""

    id: str
    start_date: dt.date
    end_date: dt.date
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    reason: Optional[str] = None
    created_at: Optional[dt.datetime] = None


def _parse_flexible_date(v):
    if v is None or v == "":
        return None
    if isinstance(v, dt.date):
        return v
    if isinstance(v, str):
        v = v.strip()
        if not v:
            return None
        if "T" in v:
            v = v.split("T")[0]
        if "/" in v:
            parts = v.split("/")
            if len(parts) == 3:
                if len(parts[2]) == 4:
                    return dt.date(int(parts[2]), int(parts[1]), int(parts[0]))
                elif len(parts[0]) == 4:
                    return dt.date(int(parts[0]), int(parts[1]), int(parts[2]))
        elif "-" in v:
            parts = v.split("-")
            if len(parts) == 3:
                if len(parts[2]) == 4:
                    return dt.date(int(parts[2]), int(parts[1]), int(parts[0]))
                elif len(parts[0]) == 4:
                    return dt.date(int(parts[0]), int(parts[1]), int(parts[2]))
    return v


def _fmt_time(hhmm: Optional[str]) -> Optional[str]:
    """'18:00' -> '6:00 PM'. Returns None on empty/bad input."""
    if not hhmm:
        return None
    try:
        h, m = hhmm.split(":")
        h_i, m_i = int(h), int(m)
    except (ValueError, AttributeError):
        return hhmm
    suffix = "AM" if h_i < 12 else "PM"
    h12 = h_i % 12 or 12
    return f"{h12}:{m_i:02d} {suffix}"


def _calculate_raw_duty_days(
    start_date: dt.date,
    end_date: Optional[dt.date],
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    shift: ShiftType = ShiftType.FULL_24H,
    shift_windows: Optional[Sequence[Any]] = None,
    is_handover: bool = False,
) -> float:
    """Calculate the base duty days without deducting breaks."""
    if not end_date:
        return 0.0
    if end_date < start_date:
        return 0.0

    cal_diff = (end_date - start_date).days
    calendar_days = cal_diff + 1

    if start_time and end_time:
        try:
            st_h, st_m = map(int, start_time.split(":")[:2])
            et_h, et_m = map(int, end_time.split(":")[:2])

            if shift == ShiftType.FULL_24H:
                st_dt = dt.datetime.combine(start_date, dt.time(st_h, st_m))
                et_dt = dt.datetime.combine(end_date, dt.time(et_h, et_m))
                diff_seconds = (et_dt - st_dt).total_seconds()
                if diff_seconds > 0:
                    duty_days = round((diff_seconds / 3600.0 / 24.0) * 2) / 2
                    if duty_days > 0:
                        return duty_days
                elif is_handover and diff_seconds == 0:
                    return 0.0
            else:
                st_mins = st_h * 60 + st_m
                et_mins = et_h * 60 + et_m
                if cal_diff == 0:
                    if is_handover and et_mins == st_mins:
                        return 0.0
                    return 1.0
                if et_mins == st_mins:
                    return float(cal_diff)
                if et_mins < st_mins:
                    # Multi-window custom duty explicitly spanning consecutive calendar days
                    if shift == ShiftType.CUSTOM and shift_windows and cal_diff == 1 and len(shift_windows) >= 2:
                        return float(max(calendar_days, len(shift_windows)))
                    # Overnight shift ending next morning or handover before shift start
                    return float(cal_diff)
                # et_mins > st_mins
                if is_handover and shift in (ShiftType.DAY, ShiftType.NIGHT) and (et_mins - st_mins) <= 6 * 60:
                    return float(cal_diff) + 0.5
                return float(calendar_days)
        except Exception:
            pass

    return float(calendar_days)


def calculate_duty_days(
    start_date: dt.date,
    end_date: Optional[dt.date],
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    shift: ShiftType = ShiftType.FULL_24H,
    duty_breaks: Optional[Sequence[Any]] = None,
    shift_windows: Optional[Sequence[Any]] = None,
    is_handover: bool = False,
) -> float:
    """Calculate the net payable duty days, deducting any temporary breaks / pause."""
    if not end_date:
        return 0.0
    if end_date < start_date:
        return 0.0

    gross_days = _calculate_raw_duty_days(
        start_date, end_date, start_time, end_time, shift, shift_windows, is_handover
    )
    if not duty_breaks:
        return gross_days

    total_break_days = 0.0
    for b in duty_breaks:
        b_dict = b if isinstance(b, dict) else (b.model_dump() if hasattr(b, "model_dump") else b.__dict__)
        b_start = _parse_flexible_date(b_dict.get("start_date"))
        b_end = _parse_flexible_date(b_dict.get("end_date"))
        if not b_start or not b_end:
            continue
        c_start = max(b_start, start_date)
        c_end = min(b_end, end_date)
        if c_end < c_start:
            continue
        b_st = b_dict.get("start_time")
        b_et = b_dict.get("end_time")
        if b_st and b_et:
            b_days = _calculate_raw_duty_days(c_start, c_end, b_st, b_et, shift)
        else:
            b_days = float((c_end - c_start).days + 1)
        total_break_days += b_days

    return max(0.0, round((gross_days - total_break_days) * 2) / 2)


_SHIFT_LABEL = {
    "morning": "Morning",
    "day": "Day",
    "night": "Night",
    "full_24h": "24 Hours",
    "custom": "Custom",
}


def _format_shift(shift, start_time: Optional[str], end_time: Optional[str], windows=None) -> str:
    """Readable shift: label + time window(s) when available."""
    s = str(getattr(shift, "value", shift))
    label = _SHIFT_LABEL.get(s, s.capitalize())
    if s == "custom":
        parts = []
        for w in windows or []:
            start = getattr(w, "start", None) if not isinstance(w, dict) else w.get("start")
            end = getattr(w, "end", None) if not isinstance(w, dict) else w.get("end")
            if start or end:
                parts.append(f"{_fmt_time(start) or '?'} - {_fmt_time(end) or '?'}")
        if not parts and (start_time or end_time):
            parts.append(f"{_fmt_time(start_time) or '?'} - {_fmt_time(end_time) or '?'}")
        if parts:
            return f"Custom ({', '.join(parts)})"
        return "Custom"

    if start_time and end_time:
        return f"{label} ({_fmt_time(start_time)} - {_fmt_time(end_time)})"
    elif start_time:
        return f"{label} (from {_fmt_time(start_time)})"
    return label


class AssignmentCreate(BaseModel):
    customer_id: str
    employee_id: str
    place_type: AssignmentPlace = AssignmentPlace.HOME
    place_name: Optional[str] = Field(None, max_length=200)
    purpose: Optional[str] = Field(None, max_length=300)
    address: Optional[str] = Field(None, max_length=500)
    shift: ShiftType = ShiftType.FULL_24H
    # Custom shift window, "HH:MM" 24-hour (used when shift == custom).
    start_time: Optional[str] = Field(None, max_length=5)
    end_time: Optional[str] = Field(None, max_length=5)
    shift_windows: Optional[List[ShiftWindow]] = None
    start_date: dt.date
    end_date: Optional[dt.date] = None
    # Convenience input: how many days the engagement runs for. When given
    # without an explicit end_date the router derives end_date from it.
    duration_days: Optional[int] = Field(None, ge=1, le=3650)
    status: AssignmentStatus = AssignmentStatus.SCHEDULED
    payment_type: Optional[PaymentType] = PaymentType.PREPAID
    payment_status: Optional[PaymentStatus] = PaymentStatus.PENDING
    # Per-day rates (preferred). Stored totals are derived = daily × total_days.
    customer_daily_rate: Optional[int] = Field(None, ge=0)
    employee_daily_rate: Optional[int] = Field(None, ge=0)
    contract_amount: Optional[int] = Field(None, ge=0)
    customer_rate: Optional[int] = Field(None, ge=0)
    employee_payout: Optional[int] = Field(None, ge=0)
    other_expense: Optional[int] = Field(None, ge=0)
    notes: Optional[str] = Field(None, max_length=2000)
    # Explicit confirmed coordinates (admin pin drop on the assignment)
    dest_lat: Optional[float] = Field(None, ge=-90, le=90)
    dest_lng: Optional[float] = Field(None, ge=-180, le=180)

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def validate_dates(cls, v):
        return _parse_flexible_date(v)

    @field_validator(
        "duration_days",
        "customer_daily_rate",
        "employee_daily_rate",
        "contract_amount",
        "customer_rate",
        "employee_payout",
        "other_expense",
        "dest_lat",
        "dest_lng",
        "place_name",
        "purpose",
        "address",
        "notes",
        "start_time",
        "end_time",
        mode="before",
    )
    @classmethod
    def sanitize_empty_values(cls, v):
        if v == "" or v is None:
            return None
        return v
    # location_id is derived from the customer/employee, not supplied directly.


class AssignmentUpdate(BaseModel):
    place_type: Optional[AssignmentPlace] = None
    place_name: Optional[str] = Field(None, max_length=200)
    purpose: Optional[str] = Field(None, max_length=300)
    address: Optional[str] = Field(None, max_length=500)
    shift: Optional[ShiftType] = None
    start_time: Optional[str] = Field(None, max_length=5)
    end_time: Optional[str] = Field(None, max_length=5)
    shift_windows: Optional[List[ShiftWindow]] = None
    start_date: Optional[dt.date] = None
    end_date: Optional[dt.date] = None
    duration_days: Optional[int] = Field(None, ge=1, le=3650)
    status: Optional[AssignmentStatus] = None
    payment_type: Optional[PaymentType] = None
    payment_status: Optional[PaymentStatus] = None
    payment_received_at: Optional[dt.datetime] = None
    payment_received_by: Optional[str] = None
    work_completed_at: Optional[dt.datetime] = None
    customer_daily_rate: Optional[int] = Field(None, ge=0)
    employee_daily_rate: Optional[int] = Field(None, ge=0)
    contract_amount: Optional[int] = Field(None, ge=0)
    customer_rate: Optional[int] = Field(None, ge=0)
    employee_payout: Optional[int] = Field(None, ge=0)
    other_expense: Optional[int] = Field(None, ge=0)
    notes: Optional[str] = Field(None, max_length=2000)
    duty_breaks: Optional[List[DutyBreakCreate]] = None
    dest_lat: Optional[float] = Field(None, ge=-90, le=90)
    dest_lng: Optional[float] = Field(None, ge=-180, le=180)

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def validate_update_dates(cls, v):
        return _parse_flexible_date(v)

    @field_validator(
        "duration_days",
        "customer_daily_rate",
        "employee_daily_rate",
        "contract_amount",
        "customer_rate",
        "employee_payout",
        "other_expense",
        "dest_lat",
        "dest_lng",
        "place_name",
        "purpose",
        "address",
        "notes",
        "start_time",
        "end_time",
        mode="before",
    )
    @classmethod
    def sanitize_empty_values(cls, v):
        if v == "" or v is None:
            return None
        return v


class AssignmentResponse(IdTimestampSchema):
    customer_id: str
    customer_name: str
    employee_id: str
    employee_name: str
    location_id: Optional[str] = None
    place_type: AssignmentPlace = AssignmentPlace.HOME
    place_name: Optional[str] = None
    purpose: Optional[str] = None
    address: Optional[str] = None
    shift: ShiftType
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    shift_windows: Optional[List[ShiftWindow]] = None
    start_date: dt.date
    end_date: Optional[dt.date] = None
    status: AssignmentStatus
    payment_type: PaymentType = PaymentType.PREPAID
    payment_status: PaymentStatus = PaymentStatus.PENDING
    payment_received_at: Optional[dt.datetime] = None
    payment_received_by: Optional[str] = None
    work_completed_at: Optional[dt.datetime] = None
    acceptance_status: AcceptanceStatus = AcceptanceStatus.PENDING
    accepted_at: Optional[dt.datetime] = None
    declined_at: Optional[dt.datetime] = None
    decline_reason: Optional[str] = None
    dest_lat: Optional[float] = None
    dest_lng: Optional[float] = None
    customer_daily_rate: Optional[int] = None
    employee_daily_rate: Optional[int] = None
    contract_amount: Optional[int] = None
    customer_rate: Optional[int] = None
    employee_payout: Optional[int] = None
    other_expense: Optional[int] = None
    bill_no: Optional[int] = None
    is_replacement: bool = False
    replacement_for_assignment_id: Optional[str] = None
    replacement_for_employee_id: Optional[str] = None
    replacement_for_employee_name: Optional[str] = None
    replaced_by_employee_id: Optional[str] = None
    replaced_by_employee_name: Optional[str] = None
    replacement_reason: Optional[str] = None
    days_worked: Optional[float] = None
    duty_breaks: Optional[List[DutyBreakResponse]] = Field(default_factory=list)
    notes: Optional[str] = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def shift_label(self) -> str:
        """Readable shift, e.g. '6:00 AM - 2:00 PM, 6:00 PM - 10:00 PM'."""
        return _format_shift(self.shift, self.start_time, self.end_time, self.shift_windows)

    # ---- Derived progress (so the admin sees how much of the engagement is
    # done and how many days are still left, without computing it client-side).

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_days(self) -> Optional[float]:
        """Length of the engagement in days, or None when open-ended."""
        if self.days_worked is not None:
            return self.days_worked
        if self.end_date is None:
            return None
        return calculate_duty_days(
            self.start_date,
            self.end_date,
            self.start_time,
            self.end_time,
            self.shift,
            self.duty_breaks,
            self.shift_windows,
            is_handover=(self.status == AssignmentStatus.REPLACED),
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def days_elapsed(self) -> float:
        """Days already completed — today is still counted as remaining."""
        today = dt.date.today()
        if today <= self.start_date:
            return 0.0
        if self.end_date is not None and today > self.end_date:
            return float(self.total_days or 0.0)
        return float((today - self.start_date).days)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def days_remaining(self) -> Optional[float]:
        """Days still to serve including today, or None when open-ended."""
        total = self.total_days
        if total is None:
            return None
        return max(0.0, float(total - self.days_elapsed))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def progress_pct(self) -> Optional[int]:
        """How far through the engagement we are, 0-100."""
        total = self.total_days
        if not total:
            return None
        return min(100, round(self.days_elapsed / total * 100))

    @model_validator(mode="after")
    def sync_fractional_days_and_amounts(self) -> Self:
        if self.start_date and self.end_date:
            duty_days = calculate_duty_days(
                self.start_date,
                self.end_date,
                self.start_time,
                self.end_time,
                self.shift,
                self.duty_breaks,
                self.shift_windows,
                is_handover=(self.status == AssignmentStatus.REPLACED),
            )
            if duty_days > 0 or self.status in (AssignmentStatus.REPLACED, AssignmentStatus.CANCELLED):
                self.days_worked = duty_days
                if self.customer_daily_rate is not None:
                    self.customer_rate = int(round(self.customer_daily_rate * duty_days))
                if self.employee_daily_rate is not None:
                    self.employee_payout = int(round(self.employee_daily_rate * duty_days))
        return self


class StaffAssignmentResponse(IdTimestampSchema):
    """Assignment as the nurse sees it — deliberately omits every money field.

    No ``customer_rate`` / ``employee_payout`` / ``other_expense`` / ``notes``
    exists on this class, so price can never leak to the staff portal.
    ``customer_phone`` is denormalised by the router from the Customer record.
    """

    customer_name: str
    customer_phone: Optional[str] = None
    place_type: AssignmentPlace = AssignmentPlace.HOME
    place_name: Optional[str] = None
    purpose: Optional[str] = None
    address: Optional[str] = None
    shift: ShiftType
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    shift_windows: Optional[List[ShiftWindow]] = None
    start_date: dt.date
    end_date: Optional[dt.date] = None
    acceptance_status: AcceptanceStatus
    status: AssignmentStatus = AssignmentStatus.SCHEDULED
    payment_type: PaymentType = PaymentType.PREPAID
    payment_status: PaymentStatus = PaymentStatus.PENDING
    collect_amount: Optional[int] = None
    work_completed_at: Optional[dt.datetime] = None
    dest_lat: Optional[float] = None
    dest_lng: Optional[float] = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def shift_label(self) -> str:
        return _format_shift(self.shift, self.start_time, self.end_time, self.shift_windows)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_days(self) -> Optional[float]:
        if self.end_date is None:
            return None
        return calculate_duty_days(self.start_date, self.end_date, self.start_time, self.end_time, self.shift)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def days_remaining(self) -> Optional[float]:
        total = self.total_days
        if total is None:
            return None
        today = dt.date.today()
        if today <= self.start_date:
            return total
        if self.end_date is not None and today > self.end_date:
            return 0.0
        return max(0.0, float(total - (today - self.start_date).days))
