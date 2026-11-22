"""
MatchBot — Base de datos de clientes.

Registro AUTOMÁTICO: la primera vez que alguien reserva (chat o llamada),
el bot le pregunta su nombre (y cumpleaños opcional) UNA sola vez.
Después lo reconoce por su teléfono: sus reservas salen a su nombre
registrado en Playtomic y el panel muestra su ficha.

Clave canónica: los últimos 10 dígitos del teléfono (phone10), porque
WhatsApp manda 521XXXXXXXXXX y la voz usa 52XXXXXXXXXX.
"""
import logging
from datetime import datetime, timedelta
import os
import re

from db.database import execute

logger = logging.getLogger("matchbot.customers")

OFFSET = int(os.getenv("CLUB_UTC_OFFSET", "-6"))


def phone10(phone: str) -> str:
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def ensure_table():
    execute("""
        CREATE TABLE IF NOT EXISTS customers (
            phone10 TEXT PRIMARY KEY,
            wa_phone TEXT,
            name TEXT,
            birthday TEXT,
            source TEXT DEFAULT 'chat',
            bookings_count INT DEFAULT 0,
            last_booking_at TIMESTAMP,
            last_bday_greet INT,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)


def get(phone: str) -> dict | None:
    key = phone10(phone)
    if not key:
        return None
    row = execute("SELECT * FROM customers WHERE phone10=%s", [key], fetch_one=True)
    return dict(row) if row else None


def registered_name(phone: str) -> str:
    c = get(phone)
    return (c or {}).get("name") or ""


def save_name(phone: str, name: str, source: str = "chat"):
    key = phone10(phone)
    name = (name or "").strip()[:80]
    if not key or not name:
        return
    try:
        execute("""
            INSERT INTO customers (phone10, wa_phone, name, source)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (phone10) DO UPDATE
            SET name=EXCLUDED.name, wa_phone=EXCLUDED.wa_phone, updated_at=NOW()
        """, [key, phone, name, source])
    except Exception as e:
        logger.error(f"customers save_name failed: {e}")


def save_birthday(phone: str, birthday: str):
    """birthday en formato DD/MM."""
    key = phone10(phone)
    if not key:
        return
    try:
        execute("UPDATE customers SET birthday=%s, updated_at=NOW() WHERE phone10=%s",
                [birthday, key])
    except Exception as e:
        logger.error(f"customers save_birthday failed: {e}")


def parse_birthday(text: str) -> str | None:
    """'15/05', '15-05', '15 de mayo' → 'DD/MM' o None."""
    t = (text or "").strip().lower()
    m = re.search(r"(\d{1,2})\s*[/\-\.]\s*(\d{1,2})", t)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        if 1 <= d <= 31 and 1 <= mo <= 12:
            return f"{d:02d}/{mo:02d}"
    meses = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5,
             "junio": 6, "julio": 7, "agosto": 8, "septiembre": 9,
             "octubre": 10, "noviembre": 11, "diciembre": 12}
    m = re.search(r"(\d{1,2})\s*(?:de\s+)?([a-záé]+)", t)
    if m and m.group(2) in meses:
        d = int(m.group(1))
        if 1 <= d <= 31:
            return f"{d:02d}/{meses[m.group(2)]:02d}"
    return None


def touch_booking(phone: str, source: str = "chat"):
    """Cuenta una reserva al cliente (lo crea si no existe, sin nombre aún)."""
    key = phone10(phone)
    if not key:
        return
    try:
        execute("""
            INSERT INTO customers (phone10, wa_phone, source, bookings_count, last_booking_at)
            VALUES (%s, %s, %s, 1, NOW())
            ON CONFLICT (phone10) DO UPDATE
            SET bookings_count = customers.bookings_count + 1,
                last_booking_at = NOW(), updated_at = NOW()
        """, [key, phone, source])
    except Exception as e:
        logger.error(f"customers touch_booking failed: {e}")


def datos_message(phone: str) -> str:
    c = get(phone)
    if not c or not c.get("name"):
        return ("📇 Aún no estás registrado. Al hacer tu próxima reserva "
                "te preguntaré tu nombre para registrarte. 🎾")
    saldo_line = ""
    try:
        from api import wallet
        bal = wallet.balance(phone)
        saldo_line = f"💰 Saldo en monedero: ${bal:.0f} MXN"
    except Exception:
        pass
    lines = [
        "📇 *Tus datos de cliente*",
        "",
        f"👤 Nombre: {c['name']}",
        f"🎂 Cumpleaños: {c.get('birthday') or 'sin registrar'}",
        f"🎾 Reservas con el bot: {c.get('bookings_count') or 0}",
        saldo_line,
        "",
        "Para corregir tu nombre escribe: *soy Tu Nombre*",
        "Para tu cumpleaños escribe: *cumple 15/05*",
    ]
    return "\n".join(lines)


def today_birthdays() -> list[dict]:
    """Clientes que cumplen años HOY (hora local) y no han sido felicitados este año."""
    now = datetime.utcnow() + timedelta(hours=OFFSET)
    today = f"{now.day:02d}/{now.month:02d}"
    try:
        rows = execute("""
            SELECT * FROM customers
            WHERE birthday=%s AND name IS NOT NULL
              AND (last_bday_greet IS NULL OR last_bday_greet < %s)
        """, [today, now.year], fetch_all=True) or []
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"customers today_birthdays failed: {e}")
        return []


def mark_bday_greeted(phone10_key: str):
    now = datetime.utcnow() + timedelta(hours=OFFSET)
    try:
        execute("UPDATE customers SET last_bday_greet=%s WHERE phone10=%s",
                [now.year, phone10_key])
    except Exception as e:
        logger.error(f"customers mark_bday_greeted failed: {e}")
