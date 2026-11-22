"""
MatchBot — Endpoints para el AGENTE DE VOZ (llamadas por WhatsApp).

Cualquier plataforma de voz-IA (Telnyx, Zernio, Vapi vía SIP, etc.)
puede usar estos endpoints como "herramientas" del agente:

  GET  /api/voice/disponibilidad?date=YYYY-MM-DD   (o "hoy"/"manana")
  POST /api/voice/reservar  {date, time, nombre, telefono}

Seguridad: requieren el header  X-Voice-Key: <VOICE_API_KEY>
(env var en Render). Sin la llave → 403. Respuestas pensadas para
que una voz las lea natural en español.
"""
import os
import logging
from datetime import datetime, timedelta

logger = logging.getLogger("matchbot.voice")

OFFSET = int(os.getenv("CLUB_UTC_OFFSET", "-6"))
DAYS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def _auth_ok(request) -> bool:
    key = os.getenv("VOICE_API_KEY", "")
    if not key:
        return False  # deshabilitado hasta configurar la llave
    return request.headers.get("x-voice-key", "") == key


def _local_now() -> datetime:
    return datetime.utcnow() + timedelta(hours=OFFSET)


def _resolve_date(raw: str) -> str | None:
    """'hoy'/'manana'/'mañana'/'YYYY-MM-DD' → ISO date local."""
    raw = (raw or "").strip().lower()
    today = _local_now().date()
    if raw in ("", "hoy"):
        return today.isoformat()
    if raw in ("manana", "mañana"):
        return (today + timedelta(days=1)).isoformat()
    try:
        return datetime.fromisoformat(raw[:10]).date().isoformat()
    except ValueError:
        return None


def _fmt_time_12h(hhmm: str) -> str:
    """'19:30' → '7:30 de la tarde' (para que la voz lo lea natural)."""
    try:
        h, m = int(hhmm[:2]), int(hhmm[3:5])
    except (ValueError, IndexError):
        return hhmm
    if h < 12:
        part = "de la mañana"
        h12 = h or 12
    elif h < 19:
        part = "de la tarde"
        h12 = h - 12 or 12
    else:
        part = "de la noche"
        h12 = h - 12
    mm = f" {m:02d}" if m else ""
    return f"{h12}{':' + f'{m:02d}' if m else ''} {part}".replace(":00 ", " ")


async def disponibilidad(playtomic, raw_date: str) -> dict:
    """Horarios libres del día, resumidos para leerse por voz."""
    date_str = _resolve_date(raw_date)
    if not date_str:
        return {"ok": False, "mensaje": "No entendí la fecha. Usa hoy, mañana o año-mes-día."}

    availability = await playtomic.get_availability(date_str)
    if not availability:
        return {"ok": True, "date": date_str, "horarios": [],
                "mensaje": f"No hay canchas disponibles para ese día."}

    # Agrupar por hora de inicio
    times = {}
    for court in availability:
        for slot in court["slots"]:
            times.setdefault(slot["time"], []).append(court["name"])

    horarios = []
    for t in sorted(times.keys()):
        horarios.append({
            "hora": t,
            "hora_hablada": _fmt_time_12h(t),
            "canchas_libres": len(times[t]),
        })

    d = datetime.fromisoformat(date_str).date()
    dia_label = f"{DAYS_ES[d.weekday()]} {d.day}"
    return {
        "ok": True,
        "date": date_str,
        "dia": dia_label,
        "horarios": horarios,
        "mensaje": (f"Para el {dia_label} hay {len(horarios)} horarios disponibles, "
                    f"desde las {horarios[0]['hora_hablada']} hasta las {horarios[-1]['hora_hablada']}."
                    if horarios else "No hay horarios disponibles ese día."),
    }


async def reservar(playtomic, raw_date: str, time_str: str,
                   nombre: str, telefono: str) -> dict:
    """Crea la reserva: elige cancha libre a esa hora, cotiza y reserva.
    Misma lógica que el bot de WhatsApp (sellos y avisos incluidos)."""
    from api import loyalty

    date_str = _resolve_date(raw_date)
    if not date_str:
        return {"ok": False, "mensaje": "No entendí la fecha."}
    time_str = (time_str or "").strip()[:5]
    if len(time_str) == 4:  # "9:30" → "09:30"
        time_str = "0" + time_str

    telefono = "".join(ch for ch in (telefono or "") if ch.isdigit())
    if len(telefono) == 10:
        telefono = "52" + telefono
    if not telefono:
        return {"ok": False, "mensaje": "Necesito el teléfono del cliente para la reserva."}

    availability = await playtomic.get_availability(date_str)
    matching = []
    for court in availability:
        for slot in court["slots"]:
            if slot["time"] == time_str:
                matching.append({"court": court["name"], **slot})
    if not matching:
        return {"ok": False,
                "mensaje": f"Ese horario ya no está disponible. Pregunta por otro horario."}

    chosen = matching[0]
    price = chosen["price"]
    if price <= 0:
        price = await playtomic.calculate_price(
            chosen["resource_id"], chosen["start"], chosen["duration"]) or \
            float(__import__("api.settings", fromlist=["settings"]).get("precio_respaldo") or os.getenv("FALLBACK_PRICE", "300"))

    # Base de clientes: si el teléfono ya está registrado, usar ese nombre
    display_name = (nombre or "Cliente llamada").strip()
    try:
        from api import customers
        reg = customers.registered_name(telefono)
        if reg:
            display_name = reg
    except Exception as e:
        logger.error(f"voice customers lookup failed: {e}")

    # Monedero: ¿el saldo alcanza para pagar en automático?
    wallet_will_pay = False
    try:
        from api import wallet
        wallet_will_pay = price > 0 and wallet.balance(telefono) + 0.01 >= price
    except Exception as e:
        logger.error(f"voice wallet check failed: {e}")

    notes = "📞 Reserva por LLAMADA (agente de voz)"
    if wallet_will_pay:
        notes += f" | 💰 PAGADO CON MONEDERO DEL BOT — NO COBRAR (${price:.0f})"

    result = await playtomic.create_booking(
        resource_id=chosen["resource_id"],
        start_time=chosen["start"],
        duration=chosen["duration"],
        customer_name=display_name,
        customer_phone=telefono,
        slot_price=price,
        private_notes=notes,
        source="llamada",
    )

    if not result.get("success"):
        if result.get("error") == "slot_locked":
            return {"ok": False, "mensaje": "Ese horario se acaba de ocupar. Ofrece otro horario."}
        logger.error(f"voice booking failed: {result.get('error')}")
        return {"ok": False, "mensaje": "Hubo un problema técnico al reservar. Pide que lo intenten por WhatsApp."}

    # Sello de lealtad + aviso al club
    try:
        loyalty.record_booking(telefono, result.get("match_id", ""), chosen["start"])
    except Exception as e:
        logger.error(f"voice loyalty failed: {e}")
    # Base de clientes: contar reserva y registrar nombre si es nuevo
    try:
        from api import customers
        customers.touch_booking(telefono, source="llamada")
        if not customers.registered_name(telefono) and (nombre or "").strip():
            customers.save_name(telefono, (nombre or "").strip().title(), source="llamada")
    except Exception as e:
        logger.error(f"voice customers failed: {e}")
    try:
        from api.promos import send_alert
        phone_id = os.getenv("PHONE_NUMBER_ID_PADEL", "")
        token = os.getenv("WHATSAPP_TOKEN", "")
        notify = os.getenv("CLUB_NOTIFY_PHONE", "528342546466")
        if phone_id and token:
            d = datetime.fromisoformat(date_str).date()
            await send_alert(phone_id, token, notify,
                f"📞 *Nueva reserva por LLAMADA*\n\n"
                f"👤 {display_name}\n📱 +{telefono}\n"
                f"📅 {DAYS_ES[d.weekday()]} {d.day} a las {time_str}\n"
                f"🎾 {chosen['court']}\n💰 ${price:.0f} MXN")
    except Exception as e:
        logger.error(f"voice notify failed: {e}")

    # Cobro automático con monedero
    pago_frase = "se paga al llegar al club"
    if wallet_will_pay:
        try:
            from api import wallet
            remaining = wallet.debit(telefono, price, result.get("match_id", ""))
            pago_frase = (f"ya quedó pagada con tu monedero del club, "
                          f"te quedan {remaining:.0f} pesos de saldo")
        except Exception as e:
            logger.error(f"voice wallet debit failed: {e}")

    d = datetime.fromisoformat(date_str).date()
    return {
        "ok": True,
        "cancha": chosen["court"],
        "precio": price,
        "mensaje": (f"Listo, reserva confirmada: {DAYS_ES[d.weekday()]} {d.day} "
                    f"a las {_fmt_time_12h(time_str)} en la {chosen['court']}, "
                    f"total {price:.0f} pesos, {pago_frase}."),
    }
