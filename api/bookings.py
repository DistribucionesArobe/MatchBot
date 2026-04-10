"""
MatchBot - Booking CRUD operations
"""
from datetime import date, time, datetime, timedelta
from typing import Optional
from db.database import execute, get_cursor
from api.availability import check_slot_available
from config.settings import settings


class BookingError(Exception):
    pass


def create_booking(
    club_id: int,
    court_id: int,
    booking_date: date,
    start_time: str,
    end_time: str,
    wa_phone: str,
    customer_name: Optional[str] = None,
    booking_type: str = "regular",
    payment_method: Optional[str] = None,
    amount_cents: int = 0,
    notes: Optional[str] = None,
) -> dict:
    """
    Create a new booking. Returns the booking dict.
    Raises BookingError if slot is taken.
    """
    # Double-check availability (prevent race condition)
    if not check_slot_available(club_id, court_id, booking_date, start_time):
        raise BookingError("Lo siento, ese horario acaba de ser reservado por alguien más.")

    # Find or create customer
    customer_id = _upsert_customer(club_id, wa_phone, customer_name)

    booking = execute("""
        INSERT INTO bookings
            (club_id, court_id, customer_id, booking_date, start_time, end_time,
             status, payment_status, payment_method, amount_cents, booking_type,
             booked_via, wa_phone, notes)
        VALUES (%s, %s, %s, %s, %s::TIME, %s::TIME,
                'pending', 'unpaid', %s::payment_method, %s, %s,
                'whatsapp', %s, %s)
        RETURNING *
    """, [
        club_id, court_id, customer_id, booking_date, start_time, end_time,
        payment_method, amount_cents, booking_type, wa_phone, notes
    ], fetch_one=True)

    # Update customer stats
    execute("""
        UPDATE customers
        SET total_bookings = total_bookings + 1,
            last_booking = NOW()
        WHERE id = %s
    """, [customer_id])

    return dict(booking)


def confirm_booking(booking_id: int, payment_method: str = "cash") -> dict:
    """Mark booking as confirmed/paid."""
    booking = execute("""
        UPDATE bookings
        SET status = 'confirmed',
            payment_status = 'paid',
            payment_method = %s::payment_method,
            updated_at = NOW()
        WHERE id = %s
        RETURNING *
    """, [payment_method, booking_id], fetch_one=True)

    if not booking:
        raise BookingError("Reserva no encontrada.")

    # Update customer total_spent
    execute("""
        UPDATE customers
        SET total_spent = total_spent + %s
        WHERE id = %s
    """, [booking["amount_cents"], booking["customer_id"]])

    return dict(booking)


def cancel_booking(booking_id: int, club_id: int) -> dict:
    """Cancel a booking. Checks cancellation policy."""
    booking = execute("""
        SELECT * FROM bookings
        WHERE id = %s AND club_id = %s AND status NOT IN ('cancelled', 'completed')
    """, [booking_id, club_id], fetch_one=True)

    if not booking:
        raise BookingError("Reserva no encontrada o ya cancelada.")

    # Check cancellation window
    booking_dt = datetime.combine(booking["booking_date"], booking["start_time"])
    hours_until = (booking_dt - datetime.now()).total_seconds() / 3600

    if hours_until < settings.CANCELLATION_HOURS:
        raise BookingError(
            f"Solo se puede cancelar con al menos {settings.CANCELLATION_HOURS} horas de anticipación. "
            f"Tu reserva es en {hours_until:.1f} horas."
        )

    result = execute("""
        UPDATE bookings
        SET status = 'cancelled',
            cancelled_at = NOW(),
            updated_at = NOW()
        WHERE id = %s
        RETURNING *
    """, [booking_id], fetch_one=True)

    return dict(result)


def get_customer_bookings(club_id: int, wa_phone: str, upcoming_only: bool = True) -> list[dict]:
    """Get bookings for a customer (by phone)."""
    date_filter = "AND b.booking_date >= CURRENT_DATE" if upcoming_only else ""

    bookings = execute(f"""
        SELECT b.*, c.name AS court_name, c.court_type
        FROM bookings b
        JOIN courts c ON c.id = b.court_id
        WHERE b.club_id = %s
          AND b.wa_phone = %s
          AND b.status NOT IN ('cancelled')
          {date_filter}
        ORDER BY b.booking_date, b.start_time
        LIMIT 10
    """, [club_id, wa_phone], fetch_all=True)

    return [dict(b) for b in bookings]


def get_bookings_for_date(club_id: int, target_date: date) -> list[dict]:
    """Get all bookings for a date (admin view)."""
    bookings = execute("""
        SELECT b.*, c.name AS court_name, c.short_name AS court_short,
               c.court_type, cu.name AS customer_name, cu.phone AS customer_phone
        FROM bookings b
        JOIN courts c ON c.id = b.court_id
        LEFT JOIN customers cu ON cu.id = b.customer_id
        WHERE b.club_id = %s
          AND b.booking_date = %s
        ORDER BY c.sort_order, b.start_time
    """, [club_id, target_date], fetch_all=True)

    return [dict(b) for b in bookings]


def _upsert_customer(club_id: int, phone: str, name: Optional[str] = None) -> int:
    """Find or create a customer by phone. Returns customer id."""
    existing = execute("""
        SELECT id FROM customers WHERE club_id = %s AND phone = %s
    """, [club_id, phone], fetch_one=True)

    if existing:
        if name:
            execute("UPDATE customers SET name = %s WHERE id = %s", [name, existing["id"]])
        return existing["id"]

    new = execute("""
        INSERT INTO customers (club_id, phone, name)
        VALUES (%s, %s, %s)
        RETURNING id
    """, [club_id, phone, name], fetch_one=True)
    return new["id"]
