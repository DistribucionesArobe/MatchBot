"""
MatchBot - Availability Engine
Core logic: given a club + date, return available time slots per court.
"""
from datetime import date, time, datetime, timedelta
from typing import Optional
from db.database import execute


def get_available_slots(
    club_id: int,
    target_date: date,
    court_id: Optional[int] = None,
    court_type: Optional[str] = None,   # 'open' or 'covered'
) -> list[dict]:
    """
    Returns available slots for a given club and date.

    Each slot is:
    {
        "court_id": 4,
        "court_name": "Cancha Techada 4 - Aceromax",
        "court_type": "covered",
        "start_time": "09:00",
        "end_time": "10:30",
        "price_cents": 45000,
        "is_peak": False
    }
    """
    dow = target_date.weekday()  # 0=Monday matches our schema

    # Build query: get all scheduled slots minus already-booked ones
    params: list = [club_id, dow]
    filters = []

    if court_id:
        filters.append("AND c.id = %s")
        params.append(court_id)
    if court_type:
        filters.append("AND c.court_type = %s::court_type")
        params.append(court_type)

    extra_where = " ".join(filters)

    # Get court schedules for this day of week
    schedules = execute(f"""
        SELECT
            c.id AS court_id,
            c.name AS court_name,
            c.court_type,
            cs.open_time,
            cs.close_time,
            cs.slot_minutes,
            cs.price_cents,
            cs.peak_price_cents,
            cs.peak_start,
            cs.peak_end
        FROM courts c
        JOIN court_schedules cs ON cs.court_id = c.id
        WHERE c.club_id = %s
          AND c.active = TRUE
          AND cs.active = TRUE
          AND cs.day_of_week = %s
          {extra_where}
        ORDER BY c.sort_order
    """, params, fetch_all=True)

    if not schedules:
        return []

    # Get existing bookings for this date (non-cancelled)
    booked = execute("""
        SELECT court_id, start_time, end_time
        FROM bookings
        WHERE club_id = %s
          AND booking_date = %s
          AND status NOT IN ('cancelled')
    """, [club_id, target_date], fetch_all=True)

    booked_set = set()
    for b in booked:
        booked_set.add((b["court_id"], str(b["start_time"])[:5]))

    # Generate slots
    now = datetime.now()
    is_today = target_date == now.date()
    available = []

    for sch in schedules:
        slot_dur = timedelta(minutes=sch["slot_minutes"])
        current = datetime.combine(target_date, sch["open_time"])
        close = datetime.combine(target_date, sch["close_time"])

        while current + slot_dur <= close:
            start_str = current.strftime("%H:%M")
            end_str = (current + slot_dur).strftime("%H:%M")

            # Skip past slots if today
            if is_today and current <= now:
                current += slot_dur
                continue

            # Skip if booked
            if (sch["court_id"], start_str) in booked_set:
                current += slot_dur
                continue

            # Determine price (peak vs normal)
            is_peak = False
            price = sch["price_cents"]
            if sch["peak_start"] and sch["peak_end"]:
                slot_t = current.time()
                if sch["peak_start"] <= slot_t < sch["peak_end"]:
                    is_peak = True
                    price = sch["peak_price_cents"] or sch["price_cents"]

            available.append({
                "court_id": sch["court_id"],
                "court_name": sch["court_name"],
                "court_type": sch["court_type"],
                "start_time": start_str,
                "end_time": end_str,
                "price_cents": price,
                "is_peak": is_peak,
            })

            current += slot_dur

    return available


def get_slots_summary(club_id: int, target_date: date) -> dict:
    """
    Returns a summary grouped by time slot for WhatsApp display.
    Example: { "09:00": [{"court": "Cancha 1", ...}, ...], "10:30": [...] }
    """
    slots = get_available_slots(club_id, target_date)
    by_time: dict[str, list] = {}
    for s in slots:
        t = s["start_time"]
        if t not in by_time:
            by_time[t] = []
        by_time[t].append(s)
    return by_time


def check_slot_available(club_id: int, court_id: int, target_date: date, start_time: str) -> bool:
    """Check if a specific slot is still available (for race condition prevention)."""
    result = execute("""
        SELECT COUNT(*) as cnt
        FROM bookings
        WHERE club_id = %s
          AND court_id = %s
          AND booking_date = %s
          AND start_time = %s::TIME
          AND status NOT IN ('cancelled')
    """, [club_id, court_id, target_date, start_time], fetch_one=True)
    return result["cnt"] == 0
