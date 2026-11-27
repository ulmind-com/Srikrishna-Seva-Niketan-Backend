"""IST (Asia/Kolkata) datetime & duty-interval engine.

Single source of truth for converting assignment schedules, shift windows,
duty handovers, and duty breaks/pauses into exact Asia/Kolkata intervals so
assignments, attendance logs, payslips, bills, and reports always agree.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from dateutil import parser as dateutil_parser

from app.models.enums import AssignmentStatus, AttendanceStatus, ShiftType

IST = ZoneInfo("Asia/Kolkata")

_DEFAULT_SHIFT_TIMES: dict[ShiftType, tuple[str, str]] = {
    ShiftType.FULL_24H: ("08:00", "08:00"),
    ShiftType.DAY: ("08:00", "20:00"),
    ShiftType.NIGHT: ("20:00", "08:00"),
    ShiftType.MORNING: ("07:00", "15:00"),
    ShiftType.CUSTOM: ("08:00", "20:00"),
}


def now_ist() -> dt.datetime:
    """Current datetime in Asia/Kolkata (business timezone)."""
    return dt.datetime.now(IST)


def today_ist() -> dt.date:
    """Today's calendar date in Asia/Kolkata (never UTC server date)."""
    return now_ist().date()


def parse_flexible_date(v: Any) -> Optional[dt.date]:
    """Parse date/datetime/ISO/DD-MM-YYYY string cleanly to dt.date."""
    if v is None or v == "":
        return None
    if isinstance(v, dt.datetime):
        if v.tzinfo is not None:
            return v.astimezone(IST).date()
        return v.date()
    if isinstance(v, dt.date):
        return v
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        if "T" in s:
            try:
                parsed = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    return parsed.astimezone(IST).date()
                return parsed.date()
            except ValueError:
                s = s.split("T")[0]
        for sep in ("/", "-"):
            if sep in s:
                parts = s.split(sep)
                if len(parts) == 3:
                    try:
                        if len(parts[2]) == 4:
                            return dt.date(int(parts[2]), int(parts[1]), int(parts[0]))
                        if len(parts[0]) == 4:
                            return dt.date(int(parts[0]), int(parts[1]), int(parts[2]))
                    except ValueError:
                        return None
        try:
            parsed = dateutil_parser.parse(s, dayfirst="/" in s)
            if parsed.tzinfo is not None:
                return parsed.astimezone(IST).date()
            return parsed.date()
        except (ValueError, TypeError, OverflowError):
            return None
    return None


def parse_hhmm(hhmm: Optional[str]) -> Optional[dt.time]:
    """Parse 'HH:MM' string into dt.time; returns None if invalid/missing."""
    if not hhmm:
        return None
    try:
        parts = str(hhmm).strip().split(":")
        h, m = int(parts[0]), int(parts[1])
        if 0 <= h <= 23 and 0 <= m <= 59:
            return dt.time(h, m)
    except (ValueError, IndexError, TypeError):
        pass
    return None


def fmt_hhmm(t: dt.time) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"


def ist_dt(d: dt.date, t: dt.time) -> dt.datetime:
    """Combine date + clock time anchored to Asia/Kolkata."""
    return dt.datetime.combine(d, t, tzinfo=IST)


def _merge_intervals(
    intervals: List[Tuple[dt.datetime, dt.datetime, str]],
) -> List[Tuple[dt.datetime, dt.datetime, str]]:
    """Merge overlapping or adjacent IST intervals while preserving the first reason."""
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda x: (x[0], x[1]))
    merged: List[Tuple[dt.datetime, dt.datetime, str]] = [ordered[0]]
    for s, e, reason in ordered[1:]:
        prev_s, prev_e, prev_reason = merged[-1]
        if s <= prev_e:
            merged[-1] = (prev_s, max(prev_e, e), prev_reason or reason)
        else:
            merged.append((s, e, reason))
    return merged


def _build_break_intervals(
    duty_breaks: Optional[Sequence[Any]],
    shift: ShiftType,
    shift_start_time: Optional[str],
) -> List[Tuple[dt.datetime, dt.datetime, str]]:
    """Convert raw duty_breaks into merged IST [start, end) intervals."""
    if not duty_breaks:
        return []
    raw: List[Tuple[dt.datetime, dt.datetime, str]] = []
    for b in duty_breaks:
        b_dict = (
            b
            if isinstance(b, dict)
            else (b.model_dump() if hasattr(b, "model_dump") else b.__dict__)
        )
        b_start = parse_flexible_date(b_dict.get("start_date"))
        b_end = parse_flexible_date(b_dict.get("end_date"))
        if not b_start or not b_end or b_end < b_start:
            continue

        b_st = parse_hhmm(b_dict.get("start_time"))
        b_et = parse_hhmm(b_dict.get("end_time"))
        reason = b_dict.get("reason") or "Duty pause"

        if b_st and b_et:
            s_dt = ist_dt(b_start, b_st)
            e_dt = ist_dt(b_end, b_et)
            if e_dt == s_dt and b_start == b_end:
                # Same-day break entered with equal clock times (e.g. 07:00 -> 07:00) means 1 full day.
                e_dt = s_dt + dt.timedelta(days=1)
            elif e_dt <= s_dt:
                # Break window ending next morning (e.g. overnight or wrap)
                e_dt = ist_dt(b_end + dt.timedelta(days=1), b_et)
        else:
            # No clock times: full calendar dates [b_start 00:00, b_end + 1d 00:00) IST
            s_dt = ist_dt(b_start, dt.time.min)
            e_dt = ist_dt(b_end + dt.timedelta(days=1), dt.time.min)

        if e_dt > s_dt:
            raw.append((s_dt, e_dt, reason))
    return _merge_intervals(raw)


def _window_to_dict(w: Any) -> dict:
    raw_date = w.get("date") if isinstance(w, dict) else getattr(w, "date", None)
    parsed_date = parse_flexible_date(raw_date)
    return {
        "date": parsed_date.isoformat() if parsed_date else None,
        "start": w.get("start") if isinstance(w, dict) else getattr(w, "start", None),
        "end": w.get("end") if isinstance(w, dict) else getattr(w, "end", None),
    }


def _build_worked_slots_ist(
    start_date: dt.date,
    end_date: Optional[dt.date],
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    shift: ShiftType = ShiftType.FULL_24H,
    shift_windows: Optional[Sequence[Any]] = None,
    is_handover: bool = False,
) -> List[dict]:
    """Build exact gross IST worked slots before deducting breaks.

    Each returned item contains:
      date, slot_start (IST dt), slot_end (IST dt),
      start_time ("HH:MM"), end_time ("HH:MM"),
      shift_windows, unit_weight (1.0 or 0.5).
    """
    if not end_date or end_date < start_date:
        return []

    def_st, def_et = _DEFAULT_SHIFT_TIMES.get(shift, ("08:00", "20:00"))
    st_parsed = parse_hhmm(start_time)
    et_parsed = parse_hhmm(end_time)

    # 1. 24-hour continuous duty: exact hour-by-hour IST interval cycles
    if shift == ShiftType.FULL_24H and st_parsed and et_parsed:
        start_dt = ist_dt(start_date, st_parsed)
        end_dt = ist_dt(end_date, et_parsed)
        if end_dt == start_dt:
            if is_handover:
                return []
            # Single calendar day explicitly closed as a 24h duty -> 1 full 24h cycle
            end_dt = start_dt + dt.timedelta(hours=24)
        elif end_dt < start_dt:
            return []

        total_hours = (end_dt - start_dt).total_seconds() / 3600.0
        target_units = round((total_hours / 24.0) * 2) / 2
        if target_units <= 0:
            return []

        slots: List[dict] = []
        cursor = start_dt
        remaining_units = target_units
        st_str = fmt_hhmm(st_parsed)
        while remaining_units > 0 and cursor < end_dt:
            if remaining_units >= 1.0:
                slot_end = min(cursor + dt.timedelta(hours=24), end_dt)
                slots.append(
                    {
                        "date": cursor.date(),
                        "slot_start": cursor,
                        "slot_end": slot_end,
                        "start_time": st_str,
                        "end_time": st_str,
                        "shift_windows": None,
                        "unit_weight": 1.0,
                    }
                )
                cursor = cursor + dt.timedelta(hours=24)
                remaining_units -= 1.0
            else:
                slot_end = end_dt
                slots.append(
                    {
                        "date": cursor.date(),
                        "slot_start": cursor,
                        "slot_end": slot_end,
                        "start_time": fmt_hhmm(cursor.time()),
                        "end_time": fmt_hhmm(slot_end.time()),
                        "shift_windows": None,
                        "unit_weight": 0.5,
                    }
                )
                remaining_units = 0.0
                break
        return slots

    # 2. Explicit per-date Custom Shift entries (each row carries date + start + end)
    if shift == ShiftType.CUSTOM and shift_windows:
        win_dicts_all = [_window_to_dict(w) for w in shift_windows]
        dated_pairs: List[Tuple[dt.date, dict]] = []
        for wd in win_dicts_all:
            d_val = parse_flexible_date(wd.get("date"))
            if d_val is not None:
                dated_pairs.append((d_val, wd))
        if dated_pairs:
            by_day: dict[dt.date, List[dict]] = {}
            for d_val, wd in dated_pairs:
                if d_val < start_date or d_val > end_date:
                    continue
                by_day.setdefault(d_val, []).append(wd)
            slots = []
            for d_val in sorted(by_day.keys()):
                day_wins = by_day[d_val]
                first_w = day_wins[0]
                last_w = day_wins[-1]
                w_st = parse_hhmm(first_w.get("start")) or st_parsed or parse_hhmm(def_st) or dt.time(8, 0)
                w_et = parse_hhmm(last_w.get("end")) or et_parsed or parse_hhmm(def_et) or dt.time(20, 0)
                s_dt = ist_dt(d_val, w_st)
                e_dt = ist_dt(d_val + dt.timedelta(days=1) if w_et <= w_st else d_val, w_et)
                slots.append(
                    {
                        "date": d_val,
                        "slot_start": s_dt,
                        "slot_end": e_dt,
                        "start_time": fmt_hhmm(w_st),
                        "end_time": fmt_hhmm(w_et),
                        "shift_windows": day_wins,
                        "unit_weight": 1.0,
                    }
                )
            return slots

    # 3. Daily recurring / custom shifts
    cal_diff = (end_date - start_date).days
    calendar_days = cal_diff + 1
    st_eff = st_parsed or parse_hhmm(def_st) or dt.time(8, 0)
    et_eff = et_parsed or (
        (st_parsed or parse_hhmm(def_et) or dt.time(8, 0))
        if shift == ShiftType.FULL_24H
        else (parse_hhmm(def_et) or dt.time(20, 0))
    )

    # Multi-window custom duty explicitly spanning consecutive calendar days
    if (
        shift == ShiftType.CUSTOM
        and shift_windows
        and cal_diff == 1
        and len(shift_windows) >= 2
    ):
        slots = []
        n_wins = max(calendar_days, len(shift_windows))
        for idx in range(n_wins):
            d = start_date + dt.timedelta(days=idx)
            w = _window_to_dict(shift_windows[idx]) if idx < len(shift_windows) else None
            w_st = parse_hhmm(w.get("start")) if w else st_eff
            w_et = parse_hhmm(w.get("end")) if w else et_eff
            w_st = w_st or st_eff
            w_et = w_et or et_eff
            s_dt = ist_dt(d, w_st)
            e_dt = ist_dt(d + dt.timedelta(days=1) if w_et <= w_st else d, w_et)
            slots.append(
                {
                    "date": d,
                    "slot_start": s_dt,
                    "slot_end": e_dt,
                    "start_time": fmt_hhmm(w_st),
                    "end_time": fmt_hhmm(w_et),
                    "shift_windows": [w] if w else None,
                    "unit_weight": 1.0,
                }
            )
        return slots

    has_partial_tail = False
    if st_parsed and et_parsed:
        st_mins = st_parsed.hour * 60 + st_parsed.minute
        et_mins = et_parsed.hour * 60 + et_parsed.minute
        if cal_diff == 0:
            if is_handover and et_mins == st_mins:
                return []
            total_full_days = 1
        elif et_mins <= st_mins:
            # Overnight cycle ending next morning (e.g. Night 20:00 -> 08:00) or handover at/before start
            total_full_days = cal_diff
        elif is_handover and shift in (ShiftType.DAY, ShiftType.NIGHT) and (et_mins - st_mins) <= 6 * 60:
            total_full_days = cal_diff
            has_partial_tail = True
        else:
            total_full_days = calendar_days
    else:
        total_full_days = calendar_days

    st_str = fmt_hhmm(st_parsed) if st_parsed else (def_st if shift != ShiftType.FULL_24H else None)
    if et_parsed:
        et_str = fmt_hhmm(et_parsed)
    elif shift == ShiftType.FULL_24H:
        et_str = st_str
    else:
        et_str = def_et

    slots = []
    win_dicts = [_window_to_dict(w) for w in shift_windows] if shift_windows else None
    for idx in range(total_full_days):
        d = start_date + dt.timedelta(days=idx)
        w_for_day = None
        day_st = st_str
        day_et = et_str
        if win_dicts and len(win_dicts) == total_full_days and idx < len(win_dicts):
            w_for_day = [win_dicts[idx]]
            day_st = win_dicts[idx].get("start") or day_st
            day_et = win_dicts[idx].get("end") or day_et
        elif win_dicts:
            w_for_day = win_dicts

        slot_s_t = parse_hhmm(day_st) or st_eff
        slot_e_t = parse_hhmm(day_et) or et_eff
        s_dt = ist_dt(d, slot_s_t)
        e_dt = ist_dt(d + dt.timedelta(days=1) if slot_e_t <= slot_s_t else d, slot_e_t)
        slots.append(
            {
                "date": d,
                "slot_start": s_dt,
                "slot_end": e_dt,
                "start_time": day_st,
                "end_time": day_et,
                "shift_windows": w_for_day,
                "unit_weight": 1.0,
            }
        )

    if has_partial_tail and st_parsed and et_parsed:
        s_dt = ist_dt(end_date, st_parsed)
        e_dt = ist_dt(end_date, et_parsed)
        slots.append(
            {
                "date": end_date,
                "slot_start": s_dt,
                "slot_end": e_dt,
                "start_time": fmt_hhmm(st_parsed),
                "end_time": fmt_hhmm(et_parsed),
                "shift_windows": None,
                "unit_weight": 0.5,
            }
        )

    return slots


def _interval_overlap_seconds(
    a_start: dt.datetime,
    a_end: dt.datetime,
    b_start: dt.datetime,
    b_end: dt.datetime,
) -> float:
    lo = max(a_start, b_start)
    hi = min(a_end, b_end)
    if hi <= lo:
        return 0.0
    return (hi - lo).total_seconds()


def calculate_duty_days_ist(
    start_date: dt.date,
    end_date: Optional[dt.date],
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    shift: ShiftType = ShiftType.FULL_24H,
    duty_breaks: Optional[Sequence[Any]] = None,
    shift_windows: Optional[Sequence[Any]] = None,
    is_handover: bool = False,
) -> float:
    """Compute exact net payable duty days in Asia/Kolkata after merged breaks."""
    if not end_date or end_date < start_date:
        return 0.0

    slots = _build_worked_slots_ist(
        start_date, end_date, start_time, end_time, shift, shift_windows, is_handover
    )
    if not slots:
        return 0.0

    if not duty_breaks:
        return round(sum(s["unit_weight"] for s in slots) * 2) / 2

    breaks = _build_break_intervals(duty_breaks, shift, start_time)
    if not breaks:
        return round(sum(s["unit_weight"] for s in slots) * 2) / 2

    total_payable = 0.0
    for s in slots:
        slot_secs = (s["slot_end"] - s["slot_start"]).total_seconds()
        if slot_secs <= 0:
            continue
        overlap_secs = sum(
            _interval_overlap_seconds(s["slot_start"], s["slot_end"], bs, be)
            for bs, be, _ in breaks
        )
        ratio = overlap_secs / slot_secs
        weight = s["unit_weight"]
        if ratio >= 0.75:
            net = 0.0
        elif ratio >= 0.25:
            net = 0.5 if weight >= 1.0 else 0.0
        else:
            net = weight
        total_payable += net

    return max(0.0, round(total_payable * 2) / 2)


def assignment_attendance_schedule_ist(
    a: Any,
    cap_date: Optional[dt.date] = None,
) -> List[dict]:
    """Generate the exact per-date attendance schedule for an assignment up to cap_date.

    Matches `calculate_duty_days_ist` 1-to-1 so attendance & payslips NEVER disagree
    with Assignments, including partial/half days, overnight timings, shift edits,
    handover end-times, and merged IST pause/break intervals.
    """
    today = today_ist()
    eff_cap = cap_date or today
    status_val = str(getattr(a.status, "value", a.status))
    if status_val == AssignmentStatus.CANCELLED.value:
        return []

    is_closed = a.end_date is not None or status_val in (
        AssignmentStatus.COMPLETED.value,
        AssignmentStatus.REPLACED.value,
    )

    if a.end_date is not None:
        slots = _build_worked_slots_ist(
            a.start_date,
            a.end_date,
            a.start_time,
            a.end_time,
            a.shift,
            a.shift_windows,
            is_handover=(status_val == AssignmentStatus.REPLACED.value),
        )
    elif is_closed and a.days_worked is not None:
        # Legacy closed assignment lacking end_date: infer calendar days from days_worked
        n_days = max(int(-(-a.days_worked // 1)), 0)
        if n_days == 0:
            return []
        pseudo_end = a.start_date + dt.timedelta(days=n_days - 1)
        slots = _build_worked_slots_ist(
            a.start_date,
            pseudo_end,
            a.start_time,
            a.end_time,
            a.shift,
            a.shift_windows,
            is_handover=False,
        )
        if a.days_worked % 1 != 0 and slots:
            slots[-1]["unit_weight"] = 0.5
    else:
        # Ongoing engagement: active through eff_cap
        if eff_cap < a.start_date:
            return []
        end_for_ongoing = eff_cap
        # If shift is FULL_24H, each calendar date started up to eff_cap gets its 24h cycle starting at a.start_time
        slots = _build_worked_slots_ist(
            a.start_date,
            end_for_ongoing,
            a.start_time,
            None,
            a.shift,
            a.shift_windows,
            is_handover=False,
        )

    breaks = _build_break_intervals(a.duty_breaks, a.shift, a.start_time)

    schedule: List[dict] = []
    for s in slots:
        d = s["date"]
        if d > eff_cap:
            continue
        slot_secs = (s["slot_end"] - s["slot_start"]).total_seconds()
        overlap_secs = 0.0
        matched_reason = None
        if breaks and slot_secs > 0:
            for bs, be, r in breaks:
                ov = _interval_overlap_seconds(s["slot_start"], s["slot_end"], bs, be)
                if ov > 0:
                    overlap_secs += ov
                    if matched_reason is None:
                        matched_reason = r

        ratio = (overlap_secs / slot_secs) if slot_secs > 0 else 0.0
        weight = s["unit_weight"]
        if ratio >= 0.75 or (ratio >= 0.25 and weight < 1.0):
            rec_status = AttendanceStatus.OFF
            note = f"Break: {matched_reason or 'Duty pause'}"
            payable_weight = 0.0
        elif ratio >= 0.25 or weight < 1.0:
            rec_status = AttendanceStatus.HALF_DAY
            note = f"Half Break: {matched_reason}" if ratio >= 0.25 else None
            payable_weight = 0.5
        else:
            rec_status = AttendanceStatus.PRESENT
            note = None
            payable_weight = 1.0

        schedule.append(
            {
                "date": d,
                "status": rec_status,
                "start_time": s["start_time"],
                "end_time": s["end_time"],
                "shift_windows": s["shift_windows"],
                "payable_weight": payable_weight,
                "note": note,
            }
        )

    return schedule
