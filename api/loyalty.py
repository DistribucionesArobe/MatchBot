"""
MatchBot — Tarjeta de cliente frecuente (sellos digitales).

Reglas de negocio:
  - 1 sello por reserva JUGADA (se confirma cuando pasa la hora del
    partido y la reserva no fue cancelada). Reservar y cancelar NO suma.
  - Sello 5  → la SIGUIENTE renta es a mitad de precio.
  - Sello 10 → la SIGUIENTE renta es GRATIS y la tarjeta se reinicia.
  - Los premios se aplican automáticamente en la próxima reserva del
    cliente: el bot lo avisa en WhatsApp y lo anota en las notas
    privadas de Playtomic para que el staff lo vea al cobrar.
"""
import logging
from datetime import datetime

from db.database import execute

logger = logging.getLogger("matchbot.loyalty")

CYCLE = 10        # sellos por tarjeta
HALF_AT = 5       # sello que gana mitad de precio
FREE_AT = 10      # sello que gana cancha gratis


def ensure_tables():
    """Create loyalty tables if they don't exist. Call at startup."""
    execute("""
        CREATE TABLE IF NOT EXISTS loyalty_bookings (
            id SERIAL PRIMARY KEY,
            club_id INT DEFAULT 1,
            wa_phone TEXT NOT NULL,
            match_id TEXT UNIQUE,
            start_utc TIMESTAMP,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    execute("""
        CREATE TABLE IF NOT EXISTS loyalty_rewards (
            id SERIAL PRIMARY KEY,
            club_id INT DEFAULT 1,
            wa_phone TEXT NOT NULL,
            reward_type TEXT NOT NULL,
            earned_at TIMESTAMP DEFAULT NOW(),
            used_at TIMESTAMP,
            used_match_id TEXT
        )
    """)


def record_booking(wa_phone: str, match_id: str, start_utc: str, club_id: int = 1):
    """Register a bot booking as a PENDING stamp (confirmed after play)."""
    try:
        start_dt = datetime.fromisoformat(start_utc.replace("Z", "")[:19])
    except (ValueError, TypeError):
        start_dt = None
    try:
        execute("""
            INSERT INTO loyalty_bookings (club_id, wa_phone, match_id, start_utc, status)
            VALUES (%s, %s, %s, %s, 'pending')
            ON CONFLICT (match_id) DO NOTHING
        """, [club_id, wa_phone, match_id, start_dt])
    except Exception as e:
        logger.error(f"loyalty record_booking failed: {e}")


def mark_cancelled(match_id: str):
    """A cancelled booking never earns its stamp."""
    try:
        execute("UPDATE loyalty_bookings SET status='cancelled' WHERE match_id=%s", [match_id])
    except Exception as e:
        logger.error(f"loyalty mark_cancelled failed: {e}")


def stamps_total(wa_phone: str) -> int:
    row = execute(
        "SELECT COUNT(*) AS n FROM loyalty_bookings WHERE wa_phone=%s AND status='stamped'",
        [wa_phone], fetch_one=True,
    )
    return int(row["n"]) if row else 0


def get_card(wa_phone: str) -> dict:
    """Current card state for a customer."""
    total = stamps_total(wa_phone)
    in_cycle = total % CYCLE
    pending = execute("""
        SELECT id, reward_type FROM loyalty_rewards
        WHERE wa_phone=%s AND used_at IS NULL
        ORDER BY earned_at ASC
    """, [wa_phone], fetch_all=True) or []
    return {
        "total": total,
        "in_cycle": in_cycle,
        "pending_rewards": [dict(r) for r in pending],
    }


def card_message(wa_phone: str, name: str = "") -> str:
    """WhatsApp text for 'mis sellos'."""
    card = get_card(wa_phone)
    n = card["in_cycle"]
    filled = "🎾" * n + "⚪" * (CYCLE - n)
    lines = [
        "🎟️ *Tu tarjeta de cliente frecuente*",
        "",
        f"{filled}  ({n}/{CYCLE})",
        "",
        f"• Sello {HALF_AT}: tu siguiente renta a *mitad de precio*",
        f"• Sello {FREE_AT}: ¡cancha *GRATIS*! 🎉",
        "",
        "El sello se suma cuando juegas tu reserva (cancelar no cuenta).",
    ]
    for r in card["pending_rewards"]:
        premio = "cancha GRATIS" if r["reward_type"] == "free" else "mitad de precio"
        lines.append(f"\n🎁 *Tienes un premio pendiente:* {premio} — se aplica automáticamente en tu próxima reserva.")
    return "\n".join(lines)


def get_unused_reward(wa_phone: str):
    """Oldest unused reward, or None."""
    return execute("""
        SELECT id, reward_type FROM loyalty_rewards
        WHERE wa_phone=%s AND used_at IS NULL
        ORDER BY earned_at ASC LIMIT 1
    """, [wa_phone], fetch_one=True)


def use_reward(reward_id: int, match_id: str):
    try:
        execute(
            "UPDATE loyalty_rewards SET used_at=NOW(), used_match_id=%s WHERE id=%s",
            [match_id, reward_id],
        )
    except Exception as e:
        logger.error(f"loyalty use_reward failed: {e}")


async def process_pending(playtomic) -> list[dict]:
    """Confirm stamps for bookings whose start time has passed.
    Checks Playtomic to make sure the match wasn't cancelled outside
    the bot. Returns notification events:
      [{phone, event: 'stamp'|'half'|'free', stamps: n}]
    """
    events = []
    try:
        rows = execute("""
            SELECT id, wa_phone, match_id FROM loyalty_bookings
            WHERE status='pending' AND start_utc IS NOT NULL AND start_utc < NOW()
            ORDER BY start_utc ASC LIMIT 50
        """, fetch_all=True) or []
    except Exception as e:
        logger.error(f"loyalty process_pending query failed: {e}")
        return events

    for row in rows:
        match_id = row["match_id"]
        phone = row["wa_phone"]
        # Verify against Playtomic: cancelled matches don't stamp
        cancelled = False
        try:
            m = await playtomic.get_match(match_id)
            if m and "CANCEL" in str(m.get("status", "")).upper():
                cancelled = True
        except Exception as e:
            logger.warning(f"loyalty: could not verify match {match_id}: {e}")

        if cancelled:
            execute("UPDATE loyalty_bookings SET status='cancelled' WHERE id=%s", [row["id"]])
            continue

        execute("UPDATE loyalty_bookings SET status='stamped' WHERE id=%s", [row["id"]])
        total = stamps_total(phone)
        in_cycle = total % CYCLE

        if in_cycle == HALF_AT:
            execute(
                "INSERT INTO loyalty_rewards (wa_phone, reward_type) VALUES (%s, 'half')",
                [phone],
            )
            events.append({"phone": phone, "event": "half", "stamps": in_cycle})
        elif in_cycle == 0 and total > 0:
            execute(
                "INSERT INTO loyalty_rewards (wa_phone, reward_type) VALUES (%s, 'free')",
                [phone],
            )
            events.append({"phone": phone, "event": "free", "stamps": CYCLE})
        else:
            events.append({"phone": phone, "event": "stamp", "stamps": in_cycle})

    return events
