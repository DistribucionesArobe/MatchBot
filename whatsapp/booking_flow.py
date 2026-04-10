"""
MatchBot - WhatsApp Booking Flow (State Machine)
Adapted from CotizaExpress calculator wizard pattern.

States:
    idle            -> User sends "Reservar" or "Hola"
    choosing_date   -> Show next 7 days as buttons/list
    choosing_time   -> Show available time slots
    choosing_court  -> Show available courts for that time
    confirming      -> Show summary, ask to confirm
    choosing_payment-> Ask payment method
    done            -> Booking created, send confirmation

Data stored in wa_booking_state.data (JSONB):
    {
        "date": "2026-04-10",
        "start_time": "09:00",
        "end_time": "10:30",
        "court_id": 4,
        "court_name": "Cancha Techada 4 - Aceromax",
        "price_cents": 45000,
        "payment_method": "cash",
        "customer_name": "Carlos"
    }
"""
import json
import logging
from datetime import date, datetime, timedelta
from db.database import execute
from api.availability import get_available_slots, get_slots_summary, check_slot_available
from api.bookings import create_booking, cancel_booking, get_customer_bookings, BookingError
from whatsapp.sender import send_text, send_interactive_buttons, send_interactive_list

logger = logging.getLogger("matchbot.flow")

# Keyword triggers
GREETINGS = {"hola", "hi", "hello", "buenas", "buenos dias", "buenas tardes", "que onda"}
BOOK_TRIGGERS = {"reservar", "reserva", "cancha", "jugar", "booking", "book"}
CANCEL_TRIGGERS = {"cancelar", "cancela", "cancel"}
MY_BOOKINGS_TRIGGERS = {"mis reservas", "mis reservaciones", "my bookings", "mis partidos"}
MENU_TRIGGERS = {"menu", "menú", "opciones", "ayuda", "help"}

DAY_NAMES = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
MONTH_NAMES = ["", "Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]


async def handle_message(club: dict, wa_phone: str, message: dict):
    """
    Main entry point. Process an incoming WhatsApp message.
    club = {id, name, wa_phone_id, wa_token, ...}
    """
    phone_id = club["wa_phone_id"]
    token = club["wa_token"]
    club_id = club["id"]

    # Extract text / button reply
    text, button_id = _extract_input(message)
    text_lower = text.lower().strip() if text else ""

    # Load current state
    state_row = _get_state(club_id, wa_phone)
    state = state_row["state"] if state_row else "idle"
    data = state_row["data"] if state_row else {}

    # ── MENU / GREETING ──
    if state == "idle" and (text_lower in GREETINGS or text_lower in MENU_TRIGGERS or button_id == "btn_menu"):
        await _send_main_menu(phone_id, token, wa_phone, club["name"])
        return

    # ── START BOOKING ──
    if text_lower in BOOK_TRIGGERS or button_id == "btn_reservar":
        _set_state(club_id, wa_phone, "choosing_date", {})
        await _send_date_picker(phone_id, token, wa_phone)
        return

    # ── MY BOOKINGS ──
    if text_lower in MY_BOOKINGS_TRIGGERS or button_id == "btn_mis_reservas":
        await _send_my_bookings(phone_id, token, wa_phone, club_id)
        return

    # ── CANCEL ──
    if text_lower in CANCEL_TRIGGERS or button_id == "btn_cancelar":
        await _handle_cancel_start(phone_id, token, wa_phone, club_id)
        return

    # ── STATE MACHINE ──
    if state == "choosing_date":
        await _handle_date_chosen(phone_id, token, wa_phone, club_id, text, button_id, data)
    elif state == "choosing_time":
        await _handle_time_chosen(phone_id, token, wa_phone, club_id, text, button_id, data)
    elif state == "choosing_court":
        await _handle_court_chosen(phone_id, token, wa_phone, club_id, text, button_id, data)
    elif state == "confirming":
        await _handle_confirm(phone_id, token, wa_phone, club_id, text, button_id, data)
    elif state == "choosing_payment":
        await _handle_payment(phone_id, token, wa_phone, club_id, text, button_id, data)
    elif state == "cancelling":
        await _handle_cancel_confirm(phone_id, token, wa_phone, club_id, text, button_id, data)
    else:
        # Unknown input in idle state
        await _send_main_menu(phone_id, token, wa_phone, club.get("name", "MatchBot"))


# ─────────────────────────────────────────────────────
# MAIN MENU
# ─────────────────────────────────────────────────────

async def _send_main_menu(phone_id, token, to, club_name):
    await send_interactive_buttons(
        phone_id, token, to,
        body=f"👋 ¡Hola! Bienvenido a *{club_name}*\n\n¿Qué deseas hacer?",
        buttons=[
            {"id": "btn_reservar", "title": "🎾 Reservar cancha"},
            {"id": "btn_mis_reservas", "title": "📋 Mis reservas"},
            {"id": "btn_cancelar", "title": "❌ Cancelar"},
        ],
        header="MatchBot",
        footer="matchbot.live"
    )


# ─────────────────────────────────────────────────────
# DATE PICKER (next 7 days as list)
# ─────────────────────────────────────────────────────

async def _send_date_picker(phone_id, token, to):
    today = date.today()
    rows = []
    for i in range(7):
        d = today + timedelta(days=i)
        day_name = DAY_NAMES[d.weekday()]
        label = "Hoy" if i == 0 else ("Mañana" if i == 1 else f"{day_name} {d.day} {MONTH_NAMES[d.month]}")
        rows.append({
            "id": f"date_{d.isoformat()}",
            "title": label,
            "description": d.strftime("%d/%m/%Y"),
        })

    await send_interactive_list(
        phone_id, token, to,
        body="📅 ¿Para qué día quieres reservar?",
        button_text="Ver días",
        sections=[{"title": "Próximos 7 días", "rows": rows}],
        header="Reservar Cancha",
    )


# ─────────────────────────────────────────────────────
# DATE → show available times
# ─────────────────────────────────────────────────────

async def _handle_date_chosen(phone_id, token, to, club_id, text, button_id, data):
    # Extract date from button_id like "date_2026-04-10"
    date_str = None
    if button_id and button_id.startswith("date_"):
        date_str = button_id.replace("date_", "")
    elif text:
        # Try to parse free-text date
        date_str = _parse_date_text(text)

    if not date_str:
        await send_text(phone_id, token, to, "No entendí la fecha. Por favor selecciona un día de la lista.")
        return

    target = date.fromisoformat(date_str)
    data["date"] = date_str

    # Get available time slots
    summary = get_slots_summary(club_id, target)

    if not summary:
        await send_text(phone_id, token, to, f"😕 No hay horarios disponibles para el {target.strftime('%d/%m/%Y')}. Intenta otro día.")
        return

    # Build list: group by time
    rows = []
    for t in sorted(summary.keys()):
        courts = summary[t]
        n = len(courts)
        min_price = min(c["price_cents"] for c in courts) / 100
        end = courts[0]["end_time"]
        rows.append({
            "id": f"time_{t}",
            "title": f"🕐 {t} - {end}",
            "description": f"{n} cancha{'s' if n > 1 else ''} desde ${min_price:.0f}",
        })

    # WhatsApp list max 10 rows per section
    sections = [{"title": "Horarios disponibles", "rows": rows[:10]}]

    _set_state(club_id, to, "choosing_time", data)

    day_label = DAY_NAMES[target.weekday()]
    await send_interactive_list(
        phone_id, token, to,
        body=f"⏰ Horarios para *{day_label} {target.day}/{target.month}*\nSelecciona un horario:",
        button_text="Ver horarios",
        sections=sections,
        footer=f"{len(rows)} horarios disponibles"
    )


# ─────────────────────────────────────────────────────
# TIME → show available courts for that time
# ─────────────────────────────────────────────────────

async def _handle_time_chosen(phone_id, token, to, club_id, text, button_id, data):
    time_str = None
    if button_id and button_id.startswith("time_"):
        time_str = button_id.replace("time_", "")

    if not time_str:
        await send_text(phone_id, token, to, "Por favor selecciona un horario de la lista.")
        return

    data["start_time"] = time_str
    target = date.fromisoformat(data["date"])

    # Get courts available at this time
    all_slots = get_available_slots(club_id, target)
    matching = [s for s in all_slots if s["start_time"] == time_str]

    if not matching:
        await send_text(phone_id, token, to, "😕 Ese horario ya no está disponible. Intenta otro.")
        _set_state(club_id, to, "choosing_time", data)
        return

    if len(matching) == 1:
        # Only one court → skip court selection
        court = matching[0]
        data["court_id"] = court["court_id"]
        data["court_name"] = court["court_name"]
        data["end_time"] = court["end_time"]
        data["price_cents"] = court["price_cents"]
        data["court_type"] = court["court_type"]
        _set_state(club_id, to, "confirming", data)
        await _send_confirmation(phone_id, token, to, data)
        return

    # Multiple courts → let user choose
    rows = []
    for s in matching:
        price = s["price_cents"] / 100
        tipo = "🏠 Techada" if s["court_type"] == "covered" else "☀️ Abierta"
        rows.append({
            "id": f"court_{s['court_id']}",
            "title": s["court_name"][:24],
            "description": f"{tipo} - ${price:.0f} MXN",
        })

    _set_state(club_id, to, "choosing_court", data)

    await send_interactive_list(
        phone_id, token, to,
        body=f"🎾 Canchas disponibles a las *{time_str}*\n¿Cuál prefieres?",
        button_text="Ver canchas",
        sections=[{"title": "Canchas", "rows": rows}],
    )


# ─────────────────────────────────────────────────────
# COURT → show confirmation
# ─────────────────────────────────────────────────────

async def _handle_court_chosen(phone_id, token, to, club_id, text, button_id, data):
    court_id = None
    if button_id and button_id.startswith("court_"):
        court_id = int(button_id.replace("court_", ""))

    if not court_id:
        await send_text(phone_id, token, to, "Por favor selecciona una cancha de la lista.")
        return

    target = date.fromisoformat(data["date"])
    all_slots = get_available_slots(club_id, target, court_id=court_id)
    slot = next((s for s in all_slots if s["start_time"] == data["start_time"]), None)

    if not slot:
        await send_text(phone_id, token, to, "😕 Esa cancha ya no está disponible. Intenta otra.")
        return

    data["court_id"] = court_id
    data["court_name"] = slot["court_name"]
    data["end_time"] = slot["end_time"]
    data["price_cents"] = slot["price_cents"]
    data["court_type"] = slot["court_type"]

    _set_state(club_id, to, "confirming", data)
    await _send_confirmation(phone_id, token, to, data)


# ─────────────────────────────────────────────────────
# CONFIRMATION
# ─────────────────────────────────────────────────────

async def _send_confirmation(phone_id, token, to, data):
    price = data["price_cents"] / 100
    tipo = "Techada" if data.get("court_type") == "covered" else "Abierta"

    await send_interactive_buttons(
        phone_id, token, to,
        body=(
            f"📋 *Resumen de tu reserva:*\n\n"
            f"📅 Fecha: *{data['date']}*\n"
            f"🕐 Horario: *{data['start_time']} - {data['end_time']}*\n"
            f"🎾 Cancha: *{data['court_name']}*\n"
            f"   ({tipo})\n"
            f"💰 Precio: *${price:.0f} MXN*\n\n"
            f"¿Confirmas la reserva?"
        ),
        buttons=[
            {"id": "btn_confirm_yes", "title": "✅ Confirmar"},
            {"id": "btn_confirm_no", "title": "❌ Cancelar"},
        ],
        header="Confirmar Reserva"
    )


async def _handle_confirm(phone_id, token, to, club_id, text, button_id, data):
    if button_id == "btn_confirm_no" or (text and text.lower() in ("no", "cancelar")):
        _set_state(club_id, to, "idle", {})
        await send_text(phone_id, token, to, "❌ Reserva cancelada. Escribe *Reservar* para intentar de nuevo.")
        return

    if button_id == "btn_confirm_yes" or (text and text.lower() in ("si", "sí", "yes", "confirmar")):
        _set_state(club_id, to, "choosing_payment", data)
        await send_interactive_buttons(
            phone_id, token, to,
            body="💳 ¿Cómo deseas pagar?",
            buttons=[
                {"id": "pay_cash", "title": "💵 Efectivo en club"},
                {"id": "pay_transfer", "title": "🏦 Transferencia"},
                {"id": "pay_card", "title": "💳 Tarjeta"},
            ],
            header="Método de Pago"
        )
        return

    await send_text(phone_id, token, to, "Por favor presiona *Confirmar* o *Cancelar*.")


# ─────────────────────────────────────────────────────
# PAYMENT → Create booking
# ─────────────────────────────────────────────────────

async def _handle_payment(phone_id, token, to, club_id, text, button_id, data):
    payment_map = {
        "pay_cash": "cash",
        "pay_transfer": "transfer",
        "pay_card": "card",
    }
    method = payment_map.get(button_id)
    if not method:
        await send_text(phone_id, token, to, "Por favor selecciona un método de pago.")
        return

    data["payment_method"] = method

    try:
        booking = create_booking(
            club_id=club_id,
            court_id=data["court_id"],
            booking_date=date.fromisoformat(data["date"]),
            start_time=data["start_time"],
            end_time=data["end_time"],
            wa_phone=to,
            booking_type="regular",
            payment_method=method,
            amount_cents=data["price_cents"],
        )
    except BookingError as e:
        await send_text(phone_id, token, to, f"😕 {str(e)}")
        _set_state(club_id, to, "idle", {})
        return

    _set_state(club_id, to, "idle", {})

    price = data["price_cents"] / 100
    method_labels = {"cash": "Efectivo en club", "transfer": "Transferencia", "card": "Tarjeta"}

    confirmation_msg = (
        f"✅ *¡Reserva confirmada!*\n\n"
        f"📋 Reserva #{booking['id']}\n"
        f"📅 {data['date']}\n"
        f"🕐 {data['start_time']} - {data['end_time']}\n"
        f"🎾 {data['court_name']}\n"
        f"💰 ${price:.0f} MXN\n"
        f"💳 {method_labels.get(method, method)}\n\n"
    )

    if method == "cash":
        confirmation_msg += "💵 Paga al llegar al club.\n"
    elif method == "transfer":
        confirmation_msg += (
            "🏦 *Datos para transferencia:*\n"
            "Banco: [CONFIGURAR]\n"
            "CLABE: [CONFIGURAR]\n"
            "Envía tu comprobante por este chat.\n"
        )
    elif method == "card":
        confirmation_msg += "💳 Te enviaremos el link de pago en breve.\n"

    confirmation_msg += "\n¡Nos vemos en la cancha! 🎾"

    await send_text(phone_id, token, to, confirmation_msg)


# ─────────────────────────────────────────────────────
# MY BOOKINGS
# ─────────────────────────────────────────────────────

async def _send_my_bookings(phone_id, token, to, club_id):
    bookings = get_customer_bookings(club_id, to)

    if not bookings:
        await send_text(phone_id, token, to, "📋 No tienes reservas próximas.\n\nEscribe *Reservar* para hacer una.")
        return

    msg = "📋 *Tus próximas reservas:*\n\n"
    for b in bookings:
        status_icon = "✅" if b["status"] == "confirmed" else "⏳"
        price = b["amount_cents"] / 100
        msg += (
            f"{status_icon} *#{b['id']}* - {b['booking_date']} {str(b['start_time'])[:5]}\n"
            f"   {b['court_name']} - ${price:.0f}\n\n"
        )

    await send_text(phone_id, token, to, msg)


# ─────────────────────────────────────────────────────
# CANCEL FLOW
# ─────────────────────────────────────────────────────

async def _handle_cancel_start(phone_id, token, to, club_id):
    bookings = get_customer_bookings(club_id, to)

    if not bookings:
        await send_text(phone_id, token, to, "No tienes reservas que cancelar.")
        return

    rows = []
    for b in bookings:
        rows.append({
            "id": f"cancel_{b['id']}",
            "title": f"#{b['id']} - {str(b['start_time'])[:5]}",
            "description": f"{b['booking_date']} | {b['court_name'][:30]}",
        })

    _set_state(club_id, to, "cancelling", {})

    await send_interactive_list(
        phone_id, token, to,
        body="¿Cuál reserva quieres cancelar?",
        button_text="Ver reservas",
        sections=[{"title": "Tus reservas", "rows": rows[:10]}],
    )


async def _handle_cancel_confirm(phone_id, token, to, club_id, text, button_id, data):
    booking_id = None
    if button_id and button_id.startswith("cancel_"):
        booking_id = int(button_id.replace("cancel_", ""))

    if not booking_id:
        await send_text(phone_id, token, to, "Selecciona la reserva que quieres cancelar.")
        return

    try:
        cancel_booking(booking_id, club_id)
        _set_state(club_id, to, "idle", {})
        await send_text(phone_id, token, to, f"✅ Reserva #{booking_id} cancelada exitosamente.")
    except BookingError as e:
        _set_state(club_id, to, "idle", {})
        await send_text(phone_id, token, to, f"😕 {str(e)}")


# ─────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────

def _extract_input(message: dict) -> tuple[str, str]:
    """Extract text and button_id from incoming WA message."""
    text = ""
    button_id = ""

    msg_type = message.get("type", "")

    if msg_type == "text":
        text = message.get("text", {}).get("body", "")
    elif msg_type == "interactive":
        interactive = message.get("interactive", {})
        itype = interactive.get("type", "")
        if itype == "button_reply":
            button_id = interactive.get("button_reply", {}).get("id", "")
            text = interactive.get("button_reply", {}).get("title", "")
        elif itype == "list_reply":
            button_id = interactive.get("list_reply", {}).get("id", "")
            text = interactive.get("list_reply", {}).get("title", "")

    return text, button_id


def _get_state(club_id: int, wa_phone: str) -> dict | None:
    return execute(
        "SELECT state, data FROM wa_booking_state WHERE club_id = %s AND wa_phone = %s",
        [club_id, wa_phone], fetch_one=True
    )


def _set_state(club_id: int, wa_phone: str, state: str, data: dict):
    execute("""
        INSERT INTO wa_booking_state (club_id, wa_phone, state, data, updated_at)
        VALUES (%s, %s, %s, %s::jsonb, NOW())
        ON CONFLICT (club_id, wa_phone) DO UPDATE
        SET state = EXCLUDED.state, data = EXCLUDED.data, updated_at = NOW()
    """, [club_id, wa_phone, state, json.dumps(data)])


def _parse_date_text(text: str) -> str | None:
    """Try to parse common date expressions."""
    t = text.lower().strip()
    today = date.today()

    if t in ("hoy", "today"):
        return today.isoformat()
    if t in ("mañana", "tomorrow"):
        return (today + timedelta(days=1)).isoformat()

    # Try ISO format
    try:
        return date.fromisoformat(t).isoformat()
    except ValueError:
        pass

    return None
