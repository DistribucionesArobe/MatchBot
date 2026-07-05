"""
MatchBot - Main FastAPI Application
https://matchbot.live
"""
import hashlib
import hmac
import json
import logging
import asyncio
from datetime import date, datetime
from typing import Optional
from collections import defaultdict

from fastapi import FastAPI, Request, HTTPException, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse

from config.settings import settings
from db.database import init_db, execute
from api.availability import get_available_slots, get_slots_summary
from api.bookings import (
    create_booking, confirm_booking, cancel_booking,
    get_bookings_for_date, get_customer_bookings, BookingError
)
from whatsapp.booking_flow import handle_message

# ─── Logging ───
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("matchbot")

# ─── App ───
app = FastAPI(title="MatchBot", version="1.0.0", docs_url="/docs")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict in production
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Message dedup & buffering (from CotizaExpress pattern) ───
_processed_msgs: dict[str, float] = {}
_msg_buffers: dict[str, list] = defaultdict(list)
_buffer_tasks: dict[str, asyncio.Task] = {}
MSG_DEDUP_WINDOW = 120  # seconds


# ─────────────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    try:
        init_db()
        logger.info("🎾 MatchBot started — DB connected")
    except Exception as e:
        logger.warning(f"⚠️ DB not available: {e} — running without database")
    logger.info("🎾 MatchBot started — matchbot.live")


# ─────────────────────────────────────────────────────
# CLUB ROUTING — map WhatsApp phone_number_id → club
# ─────────────────────────────────────────────────────

import os

# Env-based routing: PHONE_NUMBER_ID_PADEL → club_id 1, PHONE_NUMBER_ID_SALON → club_id 2
_PHONE_MAP = {}

def _build_phone_map():
    """Build phone_number_id → club mapping from env vars."""
    global _PHONE_MAP
    pairs = [
        (os.getenv("PHONE_NUMBER_ID_PADEL", ""), 1, "Club de Padel Victoria"),
        (os.getenv("PHONE_NUMBER_ID_SALON", ""), 2, "Salón Multiusos Victoria"),
    ]
    for phone_id, club_id, name in pairs:
        if phone_id:
            _PHONE_MAP[phone_id] = {
                "id": club_id,
                "name": name,
                "wa_phone_id": phone_id,
                "wa_token": os.getenv("WHATSAPP_TOKEN", ""),
            }

_build_phone_map()


def _resolve_club(phone_number_id: str) -> dict | None:
    """Resolve phone_number_id to club dict. Uses env vars, falls back to DB."""
    if phone_number_id in _PHONE_MAP:
        return _PHONE_MAP[phone_number_id]

    # Fallback: try DB
    try:
        row = execute(
            "SELECT * FROM clubs WHERE id = 1", [], fetch_one=True
        )
        if row:
            return {
                "id": row["id"],
                "name": row["name"],
                "wa_phone_id": phone_number_id,
                "wa_token": os.getenv("WHATSAPP_TOKEN", ""),
            }
    except:
        pass

    return None


# ─────────────────────────────────────────────────────
# WHATSAPP WEBHOOK (Meta Cloud API)
# ─────────────────────────────────────────────────────

@app.get("/webhook")
async def webhook_verify(request: Request):
    """WhatsApp webhook verification (GET)."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == settings.WA_VERIFY_TOKEN:
        logger.info("Webhook verified")
        return PlainTextResponse(challenge)

    raise HTTPException(403, "Verification failed")


@app.post("/webhook")
async def webhook_receive(request: Request):
    """WhatsApp incoming message webhook (POST)."""
    body = await request.json()

    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            messages = value.get("messages", [])

            if not messages:
                continue

            # Get phone_number_id to identify which club
            metadata = value.get("metadata", {})
            phone_number_id = metadata.get("phone_number_id", "")

            # Map phone_number_id → club using env vars
            club = _resolve_club(phone_number_id)
            if not club:
                logger.warning(f"Unknown phone_number_id: {phone_number_id}")
                continue

            # Extract WhatsApp profile name from contacts
            contacts = value.get("contacts", [])
            wa_profile_name = ""
            if contacts:
                wa_profile_name = contacts[0].get("profile", {}).get("name", "")

            for msg in messages:
                wa_msg_id = msg.get("id", "")

                # Dedup
                now = datetime.now().timestamp()
                if wa_msg_id in _processed_msgs:
                    continue
                _processed_msgs[wa_msg_id] = now

                # Clean old dedup entries
                cutoff = now - MSG_DEDUP_WINDOW
                stale = [k for k, v in _processed_msgs.items() if v < cutoff]
                for k in stale:
                    del _processed_msgs[k]

                sender = msg.get("from", "")

                # Log inbound message
                execute("""
                    INSERT INTO wa_messages (club_id, wa_phone, direction, message_type, content, wa_message_id)
                    VALUES (%s, %s, 'inbound', %s, %s::jsonb, %s)
                """, [club["id"], sender, msg.get("type", ""), json.dumps(msg), wa_msg_id])

                # Buffer rapid messages (5s window)
                buf_key = f"{club['id']}:{sender}"
                _msg_buffers[buf_key].append(msg)

                if buf_key in _buffer_tasks:
                    _buffer_tasks[buf_key].cancel()

                _buffer_tasks[buf_key] = asyncio.create_task(
                    _process_buffered(buf_key, dict(club), sender, wa_profile_name)
                )

    return JSONResponse({"status": "ok"})


async def _process_buffered(buf_key: str, club: dict, sender: str, profile_name: str = ""):
    """Wait for buffer window, then process the last message."""
    await asyncio.sleep(settings.MSG_BUFFER_SECONDS)

    messages = _msg_buffers.pop(buf_key, [])
    _buffer_tasks.pop(buf_key, None)

    if not messages:
        return

    # Process only the last message (user's final intent)
    last_msg = messages[-1]
    try:
        await handle_message(club, sender, last_msg, profile_name=profile_name)
    except Exception as e:
        logger.error(f"Error handling message from {sender}: {e}", exc_info=True)


# ─────────────────────────────────────────────────────
# ADMIN API — Courts
# ─────────────────────────────────────────────────────

@app.get("/api/clubs/{club_id}/courts")
async def api_get_courts(club_id: int):
    """Get all courts for a club."""
    courts = execute(
        "SELECT * FROM courts WHERE club_id = %s AND active = TRUE ORDER BY sort_order",
        [club_id], fetch_all=True
    )
    return [dict(c) for c in courts]


# ─────────────────────────────────────────────────────
# ADMIN API — Availability
# ─────────────────────────────────────────────────────

@app.get("/api/clubs/{club_id}/availability")
async def api_get_availability(
    club_id: int,
    date: str = Query(..., description="Date in YYYY-MM-DD format"),
    court_id: Optional[int] = None,
    court_type: Optional[str] = None,
):
    """Get available slots for a date."""
    try:
        target = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "Invalid date format. Use YYYY-MM-DD")

    slots = get_available_slots(club_id, target, court_id=court_id, court_type=court_type)
    return slots


# ─────────────────────────────────────────────────────
# ADMIN API — Bookings
# ─────────────────────────────────────────────────────

@app.get("/api/clubs/{club_id}/bookings")
async def api_get_bookings(
    club_id: int,
    date: str = Query(..., description="Date in YYYY-MM-DD format"),
):
    """Get all bookings for a date (admin calendar view)."""
    try:
        target = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "Invalid date format")

    bookings = get_bookings_for_date(club_id, target)
    # Serialize time objects
    for b in bookings:
        b["start_time"] = str(b["start_time"])[:5]
        b["end_time"] = str(b["end_time"])[:5]
        b["booking_date"] = str(b["booking_date"])
        if b.get("created_at"):
            b["created_at"] = str(b["created_at"])
        if b.get("updated_at"):
            b["updated_at"] = str(b["updated_at"])
        if b.get("cancelled_at"):
            b["cancelled_at"] = str(b["cancelled_at"])
    return bookings


@app.post("/api/clubs/{club_id}/bookings")
async def api_create_booking(club_id: int, request: Request):
    """Create a booking from admin panel."""
    data = await request.json()
    try:
        booking = create_booking(
            club_id=club_id,
            court_id=data["court_id"],
            booking_date=date.fromisoformat(data["booking_date"]),
            start_time=data["start_time"],
            end_time=data["end_time"],
            wa_phone=data.get("wa_phone", "admin"),
            customer_name=data.get("customer_name"),
            booking_type=data.get("booking_type", "regular"),
            payment_method=data.get("payment_method"),
            amount_cents=data.get("amount_cents", 0),
            notes=data.get("notes"),
        )
        # Serialize
        booking["start_time"] = str(booking["start_time"])[:5]
        booking["end_time"] = str(booking["end_time"])[:5]
        booking["booking_date"] = str(booking["booking_date"])
        return booking
    except BookingError as e:
        raise HTTPException(409, str(e))


@app.patch("/api/clubs/{club_id}/bookings/{booking_id}/confirm")
async def api_confirm_booking(club_id: int, booking_id: int, request: Request):
    """Confirm/pay a booking from admin."""
    data = await request.json()
    try:
        booking = confirm_booking(booking_id, data.get("payment_method", "cash"))
        return {"status": "confirmed", "booking_id": booking_id}
    except BookingError as e:
        raise HTTPException(400, str(e))


@app.patch("/api/clubs/{club_id}/bookings/{booking_id}/cancel")
async def api_cancel_booking(club_id: int, booking_id: int):
    """Cancel a booking from admin."""
    try:
        cancel_booking(booking_id, club_id)
        return {"status": "cancelled", "booking_id": booking_id}
    except BookingError as e:
        raise HTTPException(400, str(e))


# ─────────────────────────────────────────────────────
# ADMIN API — Customers
# ─────────────────────────────────────────────────────

@app.get("/api/clubs/{club_id}/customers")
async def api_get_customers(
    club_id: int,
    search: Optional[str] = None,
    limit: int = 50,
):
    """List customers for a club."""
    if search:
        customers = execute("""
            SELECT * FROM customers
            WHERE club_id = %s AND (name ILIKE %s OR phone ILIKE %s)
            ORDER BY last_booking DESC NULLS LAST
            LIMIT %s
        """, [club_id, f"%{search}%", f"%{search}%", limit], fetch_all=True)
    else:
        customers = execute("""
            SELECT * FROM customers WHERE club_id = %s
            ORDER BY last_booking DESC NULLS LAST LIMIT %s
        """, [club_id, limit], fetch_all=True)

    return [dict(c) for c in customers]


# ─────────────────────────────────────────────────────
# ADMIN API — Dashboard Stats
# ─────────────────────────────────────────────────────

@app.get("/api/clubs/{club_id}/stats")
async def api_get_stats(club_id: int):
    """Dashboard summary stats."""
    today = date.today()

    today_bookings = execute(
        "SELECT COUNT(*) as cnt FROM bookings WHERE club_id = %s AND booking_date = %s AND status != 'cancelled'",
        [club_id, today], fetch_one=True
    )
    today_revenue = execute(
        "SELECT COALESCE(SUM(amount_cents), 0) as total FROM bookings WHERE club_id = %s AND booking_date = %s AND payment_status = 'paid'",
        [club_id, today], fetch_one=True
    )
    total_customers = execute(
        "SELECT COUNT(*) as cnt FROM customers WHERE club_id = %s",
        [club_id], fetch_one=True
    )

    return {
        "today_bookings": today_bookings["cnt"],
        "today_revenue_cents": today_revenue["total"],
        "total_customers": total_customers["cnt"],
    }


# ─────────────────────────────────────────────────────
# ADMIN API — Playtomic (list/cancel matches)
# ─────────────────────────────────────────────────────

from api.playtomic_client import playtomic

@app.get("/api/playtomic/matches")
async def api_playtomic_matches(date: str = Query(..., description="Date YYYY-MM-DD")):
    """List Playtomic matches for a date (admin)."""
    matches = await playtomic.list_matches(date)
    # Summarize for easy reading
    summary = []
    for m in matches:
        summary.append({
            "match_id": m.get("match_id", ""),
            "resource_id": m.get("resource_id", ""),
            "start": m.get("start_date", ""),
            "end": m.get("end_date", ""),
            "status": m.get("status", ""),
            "teams": m.get("teams", []),
            "raw_keys": list(m.keys()),
        })
    return {"count": len(matches), "matches": summary, "raw": matches}


@app.delete("/api/playtomic/matches/{match_id}")
async def api_playtomic_cancel(match_id: str):
    """Cancel a Playtomic match by ID (admin)."""
    result = await playtomic.cancel_match(match_id)
    if result.get("success"):
        return {"status": "cancelled", "match_id": match_id}
    raise HTTPException(400, result.get("error", "Cancel failed"))


# ─────────────────────────────────────────────────────
# Health check
# ─────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "app": "MatchBot", "version": "1.0.0"}
