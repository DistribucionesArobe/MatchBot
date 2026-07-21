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
from fastapi.responses import JSONResponse, PlainTextResponse, HTMLResponse

from config.settings import settings
from db.database import init_db, execute
from api.availability import get_available_slots, get_slots_summary
from api.bookings import (
    create_booking, confirm_booking, cancel_booking,
    get_bookings_for_date, get_customer_bookings, BookingError
)
from whatsapp.booking_flow import handle_message
from api.playtomic_client import playtomic

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

    # Pre-load Playtomic court names at startup
    try:
        await playtomic._ensure_resource_names()
        logger.info(f"Playtomic court names loaded: {list(playtomic._resource_names.values())}")
    except Exception as e:
        logger.warning(f"Could not pre-load court names: {e}")

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
    """Resolve phone_number_id to club dict. Uses env vars ONLY.

    Only processes messages for explicitly configured phone numbers
    (PHONE_NUMBER_ID_PADEL, PHONE_NUMBER_ID_SALON). Messages from any
    other number (e.g. CotizaExpress / Aceromax) are ignored.
    """
    if phone_number_id in _PHONE_MAP:
        return _PHONE_MAP[phone_number_id]

    # No fallback - unknown numbers are intentionally ignored so that
    # MatchBot does not hijack messages meant for other bots/WABAs.
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

@app.get("/api/playtomic/matches")
async def api_playtomic_matches(
    date: str = Query(None, description="Date YYYY-MM-DD (optional, shows all if omitted)"),
):
    """List Playtomic matches. Optionally filter by local date."""
    if date:
        matches = await playtomic.list_matches(date)
    else:
        matches = await playtomic.list_all_matches_raw()

    # Load resource names for display
    await playtomic._ensure_resource_names()

    # Summarize for easy reading
    summary = []
    for m in matches:
        resource_id = m.get("resource_id", "")
        court_name = playtomic._resource_names.get(resource_id, resource_id[:8])
        start_utc = m.get("start_date", m.get("start", ""))
        end_utc = m.get("end_date", m.get("end", ""))

        # Convert to local time for display
        local_start = ""
        local_end = ""
        if start_utc and "T" in start_utc:
            local_start = playtomic._local_time_str(start_utc.split("T")[1][:5])
        if end_utc and "T" in end_utc:
            local_end = playtomic._local_time_str(end_utc.split("T")[1][:5])

        # Extract player info
        players = []
        for team in m.get("teams", []):
            for p in team.get("players", []):
                name = p.get("name", "")
                phone = p.get("phone", "")
                if name or phone:
                    players.append({"name": name, "phone": phone})

        summary.append({
            "match_id": m.get("match_id", ""),
            "court": court_name,
            "resource_id": resource_id,
            "start_utc": start_utc,
            "end_utc": end_utc,
            "local_time": f"{local_start}-{local_end}" if local_start else "",
            "status": m.get("status", ""),
            "players": players,
        })
    return {"count": len(matches), "matches": summary}


@app.delete("/api/playtomic/matches/{match_id}")
async def api_playtomic_cancel(match_id: str):
    """Cancel a Playtomic match by ID (admin)."""
    result = await playtomic.cancel_match(match_id)
    if result.get("success"):
        return {"status": "cancelled", "match_id": match_id}
    raise HTTPException(400, result.get("error", "Cancel failed"))


@app.get("/api/playtomic/cancel-match")
async def api_playtomic_cancel_get(match_id: str = Query(...)):
    """TEMP: Cancel a match via GET (for remote admin without curl DELETE)."""
    result = await playtomic.cancel_match(match_id)
    return result


@app.post("/api/playtomic/cancel-bulk")
async def api_playtomic_cancel_bulk(request: Request):
    """Cancel multiple Playtomic matches at once (admin)."""
    data = await request.json()
    match_ids = data.get("match_ids", [])
    results = []
    for mid in match_ids:
        r = await playtomic.cancel_match(mid)
        results.append({"match_id": mid, **r})
    return {"results": results}


# ─────────────────────────────────────────────────────
# ADMIN API — Playtomic Diagnostics
# ─────────────────────────────────────────────────────

@app.get("/api/playtomic/search")
async def api_playtomic_search(date: str = Query("2026-07-05")):
    """Search for bookings across multiple Playtomic API endpoints."""
    return await playtomic.search_bookings(date)


@app.get("/api/playtomic/debug")
async def api_playtomic_debug(date: str = Query(None)):
    """Debug: show raw Playtomic responses for tenant info + availability."""
    import httpx
    from datetime import date as d, timedelta

    if not date:
        offset = int(os.getenv("CLUB_UTC_OFFSET", "-6"))
        local_now = datetime.utcnow() + timedelta(hours=offset)
        date = local_now.strftime("%Y-%m-%d")

    tenant_id = os.getenv("PLAYTOMIC_TENANT_ID", "")
    api = "https://manager.playtomic.io/api"
    results = {"code_version": "v13-single-payer", "date": date, "tenant_id": tenant_id}

    # Show bot auth status
    results["bot_logged_in"] = playtomic.token is not None
    results["bot_user_id"] = playtomic.user_id
    results["bot_resource_names"] = playtomic._resource_names
    results["bot_resource_sports"] = playtomic._resource_sports

    # Try login now if not logged in
    if not playtomic.token:
        try:
            login_url = f"{api}/v3/auth/login"
            login_r = await playtomic.client.post(login_url, json={
                "email": os.getenv("PLAYTOMIC_EMAIL", ""),
                "password": "***",  # masked
            })
            results["login_test"] = {
                "url": login_url,
                "status": login_r.status_code,
                "response": login_r.text[:300],
                "email_set": bool(os.getenv("PLAYTOMIC_EMAIL")),
                "password_set": bool(os.getenv("PLAYTOMIC_PASSWORD")),
            }
            # Also try the real login
            login_ok = await playtomic.login()
            results["login_retry"] = login_ok
            results["bot_logged_in_after_retry"] = playtomic.token is not None
        except Exception as e:
            results["login_test_error"] = str(e)

    # Test availability via bot's own method
    try:
        bot_avail = await playtomic.get_availability(date)
        results["bot_availability_courts"] = len(bot_avail)
        results["bot_availability_slots"] = sum(len(c.get("slots", [])) for c in bot_avail)
        if bot_avail:
            results["bot_availability_preview"] = [
                {"name": c["name"], "slots": len(c["slots"])} for c in bot_avail
            ]
    except Exception as e:
        results["bot_availability_error"] = str(e)

    # Per-request status codes from the bot's availability queries
    results["avail_query_debug"] = getattr(playtomic, "_last_avail_debug", {})
    results["has_tenant_token"] = playtomic.tenant_token is not None
    # Last booking attempt result (statuses + error bodies)
    results["last_booking_debug"] = getattr(playtomic, "_last_booking_debug", {})

    # Check the OTHER tenant ("TO BE DELETED") — the bot's manager role
    # may be attached to it instead of the tenant we use
    ALT_TENANT = "04007e09-6f59-4ed1-bbef-13c80aa0c906"
    try:
        r = await playtomic.client.get(
            f"{api}/v1/tenants/{ALT_TENANT}",
            headers=playtomic._auth_headers(),
        )
        if r.status_code == 200:
            info = r.json()
            alt_resources = info.get("resources", info.get("facilities", []))
            results["alt_tenant_name"] = info.get("tenant_name", info.get("name", "?"))
            results["alt_tenant_resources"] = [
                {"id": x.get("resource_id", x.get("id", "")), "name": x.get("name", ""), "sport": x.get("sport_id", "")}
                for x in (alt_resources[:10] if isinstance(alt_resources, list) else [])
            ]
        else:
            results["alt_tenant_error"] = f"{r.status_code}: {r.text[:150]}"
    except Exception as e:
        results["alt_tenant_error"] = str(e)[:200]
    # Claims of the current tenant token (roles = manager permissions)
    results["tenant_token_claims"] = getattr(playtomic, "_tenant_token_claims", {})
    # Claims of the customer (login) token — do the roles exist at login?
    if playtomic.token:
        c = playtomic._decode_token_claims(playtomic.token)
        results["customer_token_claims"] = {
            "aud": c.get("aud"),
            "scopes": c.get("scopes"),
            "roles": [k for k in c.keys() if k.startswith("role")],
        }

    async with httpx.AsyncClient(timeout=10, http2=True, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://playtomic.io",
        "Referer": "https://playtomic.io/",
    }) as client:
        # 1. Tenant info
        try:
            r = await client.get(f"{api}/v1/tenants/{tenant_id}")
            info = r.json() if r.status_code == 200 else r.text
            # Extract just the keys and resources summary
            if isinstance(info, dict):
                resources = info.get("resources", info.get("facilities", []))
                results["tenant_keys"] = list(info.keys())
                results["resources_count"] = len(resources)
                results["resources"] = [
                    {k: v for k, v in r.items() if k in ("resource_id", "id", "name", "resource_name", "sport_id")}
                    for r in (resources[:10] if isinstance(resources, list) else [])
                ]
            else:
                results["tenant_raw"] = str(info)[:500]
        except Exception as e:
            results["tenant_error"] = str(e)

        # 2. Availability raw (first 2 items)
        try:
            offset_h = abs(int(os.getenv("CLUB_UTC_OFFSET", "-6")))
            from datetime import date as d, timedelta
            target = d.fromisoformat(date)
            next_day = (target + timedelta(days=1)).isoformat()

            r = await client.get(f"{api}/v1/availability", params={
                "user_id": "me", "sport_id": "PADEL", "tenant_id": tenant_id,
                "start_min": f"{date}T{offset_h:02d}:00:00",
                "start_max": f"{next_day}T{offset_h:02d}:00:00",
            })
            if r.status_code == 200:
                avail = r.json()
                results["availability_count"] = len(avail) if isinstance(avail, list) else "not a list"
                if isinstance(avail, list) and avail:
                    results["availability_sample_keys"] = list(avail[0].keys())
                    # Show first item with limited slots
                    sample = dict(avail[0])
                    if "slots" in sample:
                        sample["slots"] = sample["slots"][:3]
                    results["availability_sample"] = sample
            else:
                results["availability_error"] = f"{r.status_code}: {r.text[:300]}"
        except Exception as e:
            results["availability_error"] = str(e)

    # 3. Cached resource names
    results["cached_resource_names"] = playtomic._resource_names

    return results


# ─────────────────────────────────────────────────────
# Health check
# ─────────────────────────────────────────────────────

@app.get("/stats", response_class=HTMLResponse)
async def bot_stats():
    """Bot usage dashboard — visual stats page."""
    # Total unique users
    total_users = execute(
        "SELECT COUNT(DISTINCT wa_phone) AS cnt FROM wa_booking_state",
        fetch_one=True,
    )
    # Active last 7 days
    active_7d = execute(
        "SELECT COUNT(DISTINCT wa_phone) AS cnt FROM wa_booking_state WHERE updated_at >= NOW() - INTERVAL '7 days'",
        fetch_one=True,
    )
    # Active today
    active_today = execute(
        "SELECT COUNT(DISTINCT wa_phone) AS cnt FROM wa_booking_state WHERE updated_at::date = CURRENT_DATE",
        fetch_one=True,
    )
    # Daily activity last 14 days
    daily_activity = execute(
        """SELECT updated_at::date AS dia, COUNT(DISTINCT wa_phone) AS usuarios
           FROM wa_booking_state
           WHERE updated_at >= NOW() - INTERVAL '14 days'
           GROUP BY dia ORDER BY dia""",
        fetch_all=True,
    )
    # Recent users (last 20)
    recent = execute(
        """SELECT wa_phone,
                  data->>'customer_name' AS nombre,
                  state,
                  updated_at
           FROM wa_booking_state
           ORDER BY updated_at DESC
           LIMIT 20""",
        fetch_all=True,
    )

    n_total = total_users["cnt"] if total_users else 0
    n_7d = active_7d["cnt"] if active_7d else 0
    n_today = active_today["cnt"] if active_today else 0

    # Build chart data
    chart_labels = []
    chart_values = []
    for row in (daily_activity or []):
        chart_labels.append(str(row["dia"])[5:])  # MM-DD
        chart_values.append(row["usuarios"])

    # Build table rows
    table_rows = ""
    state_labels = {
        "idle": ("Inactivo", "#6b7280"),
        "choosing_date": ("Eligiendo fecha", "#f59e0b"),
        "choosing_time": ("Eligiendo hora", "#f59e0b"),
        "choosing_court": ("Eligiendo cancha", "#f59e0b"),
        "confirming": ("Confirmando", "#3b82f6"),
        "choosing_payment": ("Pagando", "#8b5cf6"),
    }
    for r in (recent or []):
        phone = r["wa_phone"][-4:]
        name = r.get("nombre") or "—"
        st = r["state"]
        label, color = state_labels.get(st, (st, "#6b7280"))
        ts = str(r["updated_at"])[:16]
        table_rows += f"""
        <tr>
          <td>{name}</td>
          <td>***{phone}</td>
          <td><span class="badge" style="background:{color}">{label}</span></td>
          <td>{ts}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MatchBot - Dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; background:#0f172a; color:#e2e8f0; min-height:100vh; }}
  .header {{ background:linear-gradient(135deg,#1e3a5f,#0f172a); padding:24px 32px; border-bottom:1px solid #1e293b; }}
  .header h1 {{ font-size:24px; color:#fff; }}
  .header p {{ color:#94a3b8; font-size:14px; margin-top:4px; }}
  .container {{ max-width:960px; margin:0 auto; padding:24px 16px; }}
  .cards {{ display:grid; grid-template-columns:repeat(3,1fr); gap:16px; margin-bottom:32px; }}
  .card {{ background:#1e293b; border-radius:12px; padding:24px; text-align:center; border:1px solid #334155; }}
  .card .number {{ font-size:42px; font-weight:700; }}
  .card .label {{ font-size:13px; color:#94a3b8; margin-top:6px; text-transform:uppercase; letter-spacing:1px; }}
  .card.green .number {{ color:#22c55e; }}
  .card.blue .number {{ color:#3b82f6; }}
  .card.yellow .number {{ color:#f59e0b; }}
  .section {{ background:#1e293b; border-radius:12px; padding:24px; margin-bottom:24px; border:1px solid #334155; }}
  .section h2 {{ font-size:16px; color:#fff; margin-bottom:16px; }}
  table {{ width:100%; border-collapse:collapse; }}
  th {{ text-align:left; font-size:12px; color:#64748b; text-transform:uppercase; letter-spacing:1px; padding:8px 12px; border-bottom:1px solid #334155; }}
  td {{ padding:10px 12px; border-bottom:1px solid #1e293b; font-size:14px; }}
  tr:hover {{ background:#334155; }}
  .badge {{ padding:3px 10px; border-radius:12px; font-size:11px; color:#fff; font-weight:500; }}
  canvas {{ max-height:220px; }}
  @media(max-width:600px) {{
    .cards {{ grid-template-columns:1fr; }}
    .card .number {{ font-size:32px; }}
  }}
</style>
</head>
<body>
  <div class="header">
    <h1>MatchBot Dashboard</h1>
    <p>Club de Padel Victoria</p>
  </div>
  <div class="container">
    <div class="cards">
      <div class="card green">
        <div class="number">{n_total}</div>
        <div class="label">Usuarios totales</div>
      </div>
      <div class="card blue">
        <div class="number">{n_7d}</div>
        <div class="label">Activos (7 dias)</div>
      </div>
      <div class="card yellow">
        <div class="number">{n_today}</div>
        <div class="label">Activos hoy</div>
      </div>
    </div>
    <div class="section">
      <h2>Actividad diaria (ultimos 14 dias)</h2>
      <canvas id="chart"></canvas>
    </div>
    <div class="section">
      <h2>Usuarios recientes</h2>
      <table>
        <thead><tr><th>Nombre</th><th>Tel</th><th>Estado</th><th>Ultima actividad</th></tr></thead>
        <tbody>{table_rows}</tbody>
      </table>
    </div>
  </div>
  <script>
    new Chart(document.getElementById('chart'), {{
      type: 'bar',
      data: {{
        labels: {json.dumps(chart_labels)},
        datasets: [{{
          label: 'Usuarios activos',
          data: {json.dumps(chart_values)},
          backgroundColor: 'rgba(34,197,94,0.6)',
          borderColor: '#22c55e',
          borderWidth: 1,
          borderRadius: 6,
        }}]
      }},
      options: {{
        responsive: true,
        plugins: {{ legend: {{ display: false }} }},
        scales: {{
          y: {{ beginAtZero: true, ticks: {{ stepSize: 1, color: '#64748b' }}, grid: {{ color: '#1e293b' }} }},
          x: {{ ticks: {{ color: '#64748b' }}, grid: {{ display: false }} }}
        }}
      }}
    }});
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.get("/health")
async def health():
    return {"status": "ok", "app": "MatchBot", "version": "1.0.0"}
